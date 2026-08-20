"""Locked two-view synthesis, STA, and OpenROAD smoke execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from nova_rtl.contracts.base import AwareDatetime, canonical_json_bytes
from nova_rtl.contracts.platform import (
    PlatformLock,
    PlatformSmokeReport,
    SignoffEvidenceFile,
    SmokeTimingView,
    ToolchainReceipt,
    ToolFingerprint,
)
from nova_rtl.platform.activation import VerifiedToolchain, verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import (
    PlatformLockVerification,
    hash_file,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
    verify_platform_lock,
)

DESIGN_NAME = "nova_m0_two_flop_smoke"
FLOW_VARIANT = "m0_smoke"
REQUIRED_SMOKE_TOOLS = ("yosys", "opensta", "openroad")


class SmokeError(RuntimeError):
    """Raised when the locked physical smoke gate cannot complete safely."""


@dataclass(frozen=True)
class ParsedTimingEvidence:
    """Required facts parsed from one OpenSTA smoke report."""

    check: Literal["SETUP", "HOLD"]
    corner_id: str
    path_type: Literal["max", "min"]
    path_group: str
    register_count: int
    path_count: int
    slack_ps: float


@dataclass(frozen=True)
class PlatformSmokeRequest:
    """All immutable inputs and the durable destination for one smoke run."""

    project_root: Path
    toolchain_manifest: Path
    selection_policy: Path
    platform_lock: Path
    rtl: Path
    constraints: Path
    output_directory: Path


@dataclass(frozen=True)
class InputSnapshot:
    """Read-only pre-analysis bytes retained across every EDA consumer."""

    path: Path
    sha256: str
    size_bytes: int
    label: str


def require_orfs_safe_path(path: Path, label: str) -> Path:
    """Fail clearly where ORFS cannot preserve a path through Make/Tcl lists."""

    absolute = path.absolute()
    if re.fullmatch(r"/[A-Za-z0-9._/+\-]+", str(absolute)) is None:
        raise SmokeError(
            f"{label} contains characters outside the portable path allowlist; "
            f"whitespace or Make metacharacters are unsupported by ORFS: {absolute}"
        )
    return absolute


def _snapshot_input(source: Path, destination: Path, label: str) -> InputSnapshot:
    """Copy one stable input once before any EDA process can consume it."""

    resolved = source.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise SmokeError(f"{label} is missing or empty: {source}")
    before_size = resolved.stat().st_size
    before_hash = hash_file(resolved)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolved, destination)
    if (
        resolved.stat().st_size != before_size
        or hash_file(resolved) != before_hash
        or destination.stat().st_size != before_size
        or hash_file(destination) != before_hash
    ):
        raise SmokeError(f"{label} changed while snapshotting: {source}")
    destination.chmod(0o444)
    return InputSnapshot(
        path=destination.resolve(strict=True),
        sha256=before_hash,
        size_bytes=before_size,
        label=label,
    )


def _verify_snapshot(snapshot: InputSnapshot) -> None:
    if (
        snapshot.path.is_symlink()
        or not snapshot.path.is_file()
        or snapshot.path.stat().st_size != snapshot.size_bytes
        or hash_file(snapshot.path) != snapshot.sha256
    ):
        raise SmokeError(f"{snapshot.label} snapshot changed while EDA consumed it")


def _consumer_copy(snapshot: InputSnapshot, destination: Path, label: str) -> InputSnapshot:
    """Create a guarded writable copy where an external flow propagates source mode."""

    _verify_snapshot(snapshot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot.path, destination)
    destination.chmod(0o644)
    consumer = InputSnapshot(
        path=destination.resolve(strict=True),
        sha256=snapshot.sha256,
        size_bytes=snapshot.size_bytes,
        label=label,
    )
    _verify_snapshot(consumer)
    return consumer


def _require_output_outside_roots(output: Path, protected_roots: tuple[Path, ...]) -> Path:
    """Resolve a prospective destination and keep it outside verified input trees."""

    resolved_output = output.resolve(strict=False)
    for root in protected_roots:
        resolved_root = root.resolve(strict=True)
        if resolved_output == resolved_root or resolved_root in resolved_output.parents:
            raise SmokeError(
                "output destination is inside verified tool root "
                f"{resolved_root}: {resolved_output}"
            )
    return resolved_output


def _execution_environment_identity_hash(
    toolchain_environment: Mapping[str, object],
    *,
    make_sha256: str,
    make_version: str,
    python_sha256: str,
    python_version: str,
) -> str:
    """Hash the normalized, non-secret environment and host helpers used by the smoke."""

    payload = {
        "fixed_environment": {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "QT_QPA_PLATFORM": "offscreen",
            "TZ": "UTC",
            "system_path_suffix": "/usr/local/bin:/usr/bin:/bin",
        },
        "make": {"sha256": make_sha256, "version": make_version},
        "python": {"sha256": python_sha256, "version": python_version},
        "toolchain_environment": toolchain_environment,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _required_match(pattern: str, report: str, check: str, label: str) -> re.Match[str]:
    matched = re.search(pattern, report, flags=re.MULTILINE)
    if matched is None:
        raise SmokeError(f"required {check} timing evidence is missing {label}")
    return matched


def parse_timing_evidence(report: str, check: str) -> ParsedTimingEvidence:
    """Parse and validate the constrained two-register smoke path."""

    if check not in {"SETUP", "HOLD"}:
        raise SmokeError(f"unsupported timing check: {check}")
    expected_type = "max" if check == "SETUP" else "min"
    corner = _required_match(r"^NOVA_SMOKE_VIEW\s+(\S+)\s*$", report, check, "view")
    _required_match(rf"^NOVA_SMOKE_CHECK\s+{check}\s*$", report, check, "check marker")
    registers = _required_match(
        r"^NOVA_SMOKE_REGISTER_COUNT\s+(\d+)\s*$", report, check, "register count"
    )
    paths = _required_match(
        r"^NOVA_SMOKE_PATH_COUNT\s+(\d+)\s*$", report, check, "path count"
    )
    _required_match(
        r"^Startpoint:\s+first_stage\$_SDFF_PN0_\s*$", report, check, "startpoint"
    )
    _required_match(
        r"^Endpoint:\s+data_out\$_SDFF_PN0_\s*$", report, check, "endpoint"
    )
    group = _required_match(r"^Path Group:\s+(\S+)\s*$", report, check, "path group")
    path_type = _required_match(r"^Path Type:\s+(max|min)\s*$", report, check, "path type")
    slack = _required_match(
        r"^\s*([-+]?\d+(?:\.\d+)?)\s+slack \(MET\)\s*$", report, check, "MET slack"
    )
    register_count = int(registers.group(1))
    path_count = int(paths.group(1))
    if register_count != 2 or path_count < 1:
        raise SmokeError(f"required {check} timing evidence has no two-register path")
    if group.group(1) != "smoke_clock" or path_type.group(1) != expected_type:
        raise SmokeError(f"required {check} timing evidence uses the wrong constrained path")
    if "Error:" in report:
        raise SmokeError(f"required {check} timing evidence contains an OpenSTA error")
    return ParsedTimingEvidence(
        check=check,
        corner_id=corner.group(1),
        path_type=expected_type,
        path_group=group.group(1),
        register_count=register_count,
        path_count=path_count,
        slack_ps=float(slack.group(1)),
    )


def _validated_smoke_lock(
    verification: PlatformLockVerification,
    *,
    expected_manifest_hash: str,
    expected_policy_hash: str,
    verified_fingerprints: Mapping[str, ToolFingerprint],
) -> PlatformLock:
    if verification.status != "PASS" or verification.lock is None:
        details = "; ".join(
            f"{issue.code}:{issue.subject}: {issue.message}" for issue in verification.issues
        )
        raise SmokeError(f"platform lock byte verification failed: {details or 'no valid lock'}")
    lock = verification.lock
    if lock.source_manifest_hash != expected_manifest_hash:
        raise SmokeError("platform lock does not match the production source manifest")
    if lock.selection_policy_hash != expected_policy_hash:
        raise SmokeError("platform lock does not match the production selection policy")
    locked_fingerprints = {item.tool_id: item for item in lock.tool_fingerprints}
    for tool_id in REQUIRED_SMOKE_TOOLS:
        locked = locked_fingerprints.get(tool_id)
        verified = verified_fingerprints.get(tool_id)
        if locked is None or verified is None:
            raise SmokeError(f"platform lock lacks required tool fingerprint: {tool_id}")
        if locked.model_dump(exclude={"executable"}) != verified.model_dump(
            exclude={"executable"}
        ):
            raise SmokeError(f"platform lock tool fingerprint changed: {tool_id}")
    return lock


def _smoke_environment(verified: VerifiedToolchain, scratch_root: Path) -> dict[str, str]:
    home = scratch_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
    }
    for name, operation in verified.environment_operations.items():
        if operation == "SET_LITERAL":
            environment[name] = verified.literal_environment[name]
        else:
            environment[name] = os.pathsep.join(verified.canonical_environment[name])
    system_path = "/usr/local/bin:/usr/bin:/bin"
    environment["PATH"] = (
        f"{environment['PATH']}:{system_path}" if environment.get("PATH") else system_path
    )
    return environment


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        raise SmokeError(
            f"command timed out after {timeout_seconds}s: {' '.join(command)}; "
            f"partial log: {log_path}\n{output[-12000:]}"
        ) from error
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    completed = subprocess.CompletedProcess(command, process.returncode, output, None)
    if process.returncode != 0:
        raise SmokeError(
            f"command failed with exit {process.returncode}: {' '.join(command)}; "
            f"full log: {log_path}\n{output[-12000:]}"
        )
    return completed


def _run_with_snapshot_guard(
    command: list[str],
    *,
    snapshots: tuple[InputSnapshot, ...],
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Verify input identities immediately before and after one EDA consumer."""

    for snapshot in snapshots:
        _verify_snapshot(snapshot)
    try:
        return _run_checked(
            command,
            cwd=cwd,
            environment=environment,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
        )
    finally:
        for snapshot in snapshots:
            _verify_snapshot(snapshot)


def _discover_gnu_make(environment: Mapping[str, str], scratch_root: Path) -> tuple[Path, str]:
    located = shutil.which("make", path="/usr/local/bin:/usr/bin:/bin")
    if located is None:
        raise SmokeError("GNU Make was not found in the supported system paths")
    executable = Path(located).resolve(strict=True)
    completed = _run_checked(
        [str(executable), "--version"],
        cwd=scratch_root,
        environment=environment,
        log_path=scratch_root / "make-version.log",
        timeout_seconds=10,
    )
    first_line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
    if not first_line.startswith("GNU Make "):
        raise SmokeError(f"unsupported Make implementation: {first_line or 'no version output'}")
    return executable, first_line


def _orfs_config() -> str:
    return f"""\
export PLATFORM = asap7
export DESIGN_NAME = {DESIGN_NAME}
export DESIGN_NICKNAME = {DESIGN_NAME}
export VERILOG_FILES := $(value NOVA_SMOKE_RTL)
export SDC_FILE := $(value NOVA_SMOKE_SDC)
export WORK_HOME := $(value NOVA_SMOKE_WORK_HOME)
export FLOW_VARIANT = {FLOW_VARIANT}
export ASAP7_USE_VT = RVT
export CORNER = BC
export CORE_UTILIZATION = 10
export PLACE_DENSITY = 0.20
export SYNTH_MINIMUM_KEEP_SIZE = 0
export REMOVE_ABC_BUFFERS = 1
export SKIP_INCREMENTAL_REPAIR = 1
export GPL_TIMING_DRIVEN = 0
export GPL_ROUTING_DRIVEN = 0
export SKIP_CTS_REPAIR_TIMING = 1
"""


def _sta_environment(
    lock: PlatformLock,
    check: str,
    *,
    artifact_root: Path,
    netlist: Path,
    constraints: Path,
) -> dict[str, str]:
    corner = lock.setup_corner if check == "SETUP" else lock.hold_corner
    return {
        **{
            f"NOVA_SMOKE_LIB_{index:02d}": str(artifact_root / artifact.logical_path)
            for index, artifact in enumerate(corner.liberty_files, start=1)
        },
        "NOVA_SMOKE_NETLIST": str(netlist),
        "NOVA_SMOKE_SDC": str(constraints),
    }


def _sta_script(lock: PlatformLock, check: str) -> str:
    corner = lock.setup_corner if check == "SETUP" else lock.hold_corner
    path_delay = "max" if check == "SETUP" else "min"
    liberty_commands = "\n".join(
        f"read_liberty $::env(NOVA_SMOKE_LIB_{index:02d})"
        for index, _ in enumerate(corner.liberty_files, start=1)
    )
    return f"""\
{liberty_commands}
read_verilog $::env(NOVA_SMOKE_NETLIST)
link_design {DESIGN_NAME}
read_sdc $::env(NOVA_SMOKE_SDC)
set registers [all_registers]
set register_count [llength $registers]
set paths [find_timing_paths \\
  -from $registers -to $registers \\
  -path_delay {path_delay} -group_count 1]
set path_count [llength $paths]
puts "NOVA_SMOKE_VIEW {corner.corner_id}"
puts "NOVA_SMOKE_CHECK {check}"
puts "NOVA_SMOKE_REGISTER_COUNT $register_count"
puts "NOVA_SMOKE_PATH_COUNT $path_count"
if {{ $register_count != 2 || $path_count < 1 }} {{
  puts stderr "required {check} view has no timed register path"
  exit 3
}}
report_checks \\
  -from $registers -to $registers \\
  -path_delay {path_delay} -group_count 1 \\
  -fields {{slew cap input_pin net fanout}}
exit
"""


def _copy_evidence(source: Path, destination: Path, relative_path: str) -> SignoffEvidenceFile:
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise SmokeError(f"required smoke artifact is missing or empty: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_file(destination),
        size_bytes=destination.stat().st_size,
    )


def _existing_evidence(path: Path, relative_path: str) -> SignoffEvidenceFile:
    if not path.is_file() or path.stat().st_size <= 0:
        raise SmokeError(f"required smoke report is missing or empty: {path}")
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_file(path),
        size_bytes=path.stat().st_size,
    )


def run_platform_smoke(
    request: PlatformSmokeRequest,
    *,
    generated_at: AwareDatetime | None = None,
) -> PlatformSmokeReport:
    """Run and persist the real locked ASAP7 synthesis, two-view STA, and CTS gate."""

    output = request.output_directory.resolve(strict=False)
    if output.exists():
        raise SmokeError(f"smoke output directory already exists: {output}")
    rtl = request.rtl.resolve(strict=True)
    constraints = request.constraints.resolve(strict=True)
    project_root = request.project_root.resolve(strict=True)
    loaded_manifest = load_toolchain_source_manifest(request.toolchain_manifest)
    policy = load_platform_selection_policy(request.selection_policy)
    verified = verify_toolchain(
        loaded_manifest.manifest,
        project_root / loaded_manifest.manifest.tool_root_name,
    )
    receipt = ToolchainReceipt.model_validate_json(
        verified.receipt_path.read_text(encoding="utf-8")
    )
    output = _require_output_outside_roots(output, (verified.root,))
    orfs_root = verified.root / "components" / policy.orfs_component_id
    lock_verification = verify_platform_lock(
        request.platform_lock,
        artifact_root=orfs_root,
        tool_paths=verified.tool_paths,
    )
    lock = _validated_smoke_lock(
        lock_verification,
        expected_manifest_hash=loaded_manifest.content_identity_hash,
        expected_policy_hash=selection_policy_content_identity_hash(policy),
        verified_fingerprints=verified.tool_fingerprints,
    )
    if lock.platform_id != "asap7":
        raise SmokeError(f"M0 smoke requires locked ASAP7, got {lock.platform_id}")

    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="nova-m0-smoke-") as scratch_name:
        scratch = Path(scratch_name)
        flow_home = require_orfs_safe_path(orfs_root / "flow", "ORFS flow root")
        rtl_snapshot = _snapshot_input(
            rtl,
            scratch / "inputs/two_flop_smoke.sv",
            "smoke RTL",
        )
        constraints_snapshot = _snapshot_input(
            constraints,
            scratch / "inputs/two_flop_smoke.sdc",
            "smoke constraints",
        )
        orfs_constraints = _consumer_copy(
            constraints_snapshot,
            scratch / "orfs-inputs/two_flop_smoke.sdc",
            "ORFS constraints consumer copy",
        )
        (scratch / "inputs").chmod(0o555)
        (scratch / "orfs-inputs").chmod(0o555)
        rtl = require_orfs_safe_path(
            rtl_snapshot.path,
            "smoke RTL snapshot",
        )
        constraints = require_orfs_safe_path(
            constraints_snapshot.path,
            "smoke constraints snapshot",
        )
        orfs_constraints_path = require_orfs_safe_path(
            orfs_constraints.path,
            "ORFS constraints consumer copy",
        )
        work_home = require_orfs_safe_path(scratch / "orfs-work", "ORFS work directory")
        config_path = require_orfs_safe_path(scratch / "config.mk", "ORFS smoke config")
        config_path.write_text(_orfs_config(), encoding="utf-8")
        environment = _smoke_environment(verified, scratch)
        make_executable, make_version = _discover_gnu_make(environment, scratch)
        python_executable = Path(sys.executable).resolve(strict=True)
        python_sha256 = hash_file(python_executable)
        python_version = sys.version.splitlines()[0]
        environment.update(
            {
                "OPENROAD_EXE": str(verified.tool_paths["openroad"]),
                "OPENSTA_EXE": str(verified.tool_paths["opensta"]),
                "YOSYS_EXE": str(verified.tool_paths["yosys"]),
                "PYTHON_EXE": str(python_executable),
                "QT_QPA_PLATFORM": "offscreen",
                "NOVA_SMOKE_RTL": str(rtl),
                "NOVA_SMOKE_SDC": str(orfs_constraints_path),
                "NOVA_SMOKE_WORK_HOME": str(work_home),
            }
        )
        openroad_log = output / "openroad-cts.log"
        _run_with_snapshot_guard(
            [
                str(make_executable),
                "--directory",
                str(flow_home),
                f"DESIGN_CONFIG={config_path}",
                f"WORK_HOME={work_home}",
                f"FLOW_VARIANT={FLOW_VARIANT}",
                f"OPENROAD_EXE={verified.tool_paths['openroad']}",
                f"OPENSTA_EXE={verified.tool_paths['opensta']}",
                f"YOSYS_EXE={verified.tool_paths['yosys']}",
                "NUM_CORES=2",
                "cts",
            ],
            cwd=project_root,
            snapshots=(rtl_snapshot, constraints_snapshot, orfs_constraints),
            environment=environment,
            log_path=openroad_log,
            timeout_seconds=900,
        )

        results = work_home / "results/asap7" / DESIGN_NAME / FLOW_VARIANT
        netlist = results / "1_2_yosys.v"
        synth_database = results / "1_synth.odb"
        cts_database = results / "4_cts.odb"
        view_results: dict[str, SmokeTimingView] = {}
        for check, corner in (("SETUP", lock.setup_corner), ("HOLD", lock.hold_corner)):
            lower = check.lower()
            script_path = scratch / f"{lower}.tcl"
            report_path = output / f"{lower}.rpt"
            script_path.write_text(_sta_script(lock, check), encoding="utf-8")
            sta_environment = {
                **environment,
                **_sta_environment(
                    lock,
                    check,
                    artifact_root=orfs_root,
                    netlist=netlist,
                    constraints=constraints,
                ),
            }
            completed = _run_with_snapshot_guard(
                [str(verified.tool_paths["opensta"]), str(script_path)],
                cwd=project_root,
                snapshots=(rtl_snapshot, constraints_snapshot),
                environment=sta_environment,
                log_path=report_path,
                timeout_seconds=180,
            )
            parsed = parse_timing_evidence(completed.stdout, check)
            if parsed.corner_id != corner.corner_id:
                raise SmokeError(
                    f"{check} report used {parsed.corner_id}, expected {corner.corner_id}"
                )
            view_results[check] = SmokeTimingView(
                check=check,
                corner_id=parsed.corner_id,
                path_type=parsed.path_type,
                path_group="smoke_clock",
                register_count=2,
                path_count=parsed.path_count,
                slack_ps=parsed.slack_ps,
                report=_existing_evidence(report_path, f"{lower}.rpt"),
            )

        _verify_snapshot(rtl_snapshot)
        _verify_snapshot(constraints_snapshot)
        _verify_snapshot(orfs_constraints)
        report = PlatformSmokeReport(
            status="PASS",
            platform_id=lock.platform_id,
            platform_lock_hash=hash_file(request.platform_lock),
            toolchain_receipt_hash=hash_file(verified.receipt_path),
            source_manifest_hash=loaded_manifest.content_identity_hash,
            selection_policy_hash=selection_policy_content_identity_hash(policy),
            execution_environment_hash=_execution_environment_identity_hash(
                receipt.model_dump(mode="json")["environment"],
                make_sha256=hash_file(make_executable),
                make_version=make_version,
                python_sha256=python_sha256,
                python_version=python_version,
            ),
            make_executable=str(make_executable),
            make_executable_sha256=hash_file(make_executable),
            make_version=make_version,
            python_executable=str(python_executable),
            python_executable_sha256=python_sha256,
            python_version=python_version,
            rtl=_copy_evidence(
                rtl_snapshot.path,
                output / "two_flop_smoke.sv",
                "two_flop_smoke.sv",
            ),
            constraints=_copy_evidence(
                constraints_snapshot.path,
                output / "two_flop_smoke.sdc",
                "two_flop_smoke.sdc",
            ),
            smoke_config=_copy_evidence(config_path, output / "config.mk", "config.mk"),
            openroad_log=_existing_evidence(openroad_log, "openroad-cts.log"),
            mapped_netlist=_copy_evidence(
                netlist,
                output / "mapped-netlist.v",
                "mapped-netlist.v",
            ),
            synthesis_database=_copy_evidence(
                synth_database,
                output / "1_synth.odb",
                "1_synth.odb",
            ),
            cts_database=_copy_evidence(
                cts_database,
                output / "4_cts.odb",
                "4_cts.odb",
            ),
            setup_view=view_results["SETUP"],
            hold_view=view_results["HOLD"],
            generated_at=generated_at or datetime.now(UTC),
        )
        (output / "smoke-report.json").write_bytes(canonical_json_bytes(report) + b"\n")
        return report


__all__ = [
    "ParsedTimingEvidence",
    "PlatformSmokeRequest",
    "SmokeError",
    "parse_timing_evidence",
    "require_orfs_safe_path",
    "run_platform_smoke",
]
