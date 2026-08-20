from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import pytest

from nova_rtl.contracts.platform import PlatformLock, ToolFingerprint
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import (
    PlatformLockVerification,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
    verify_platform_lock,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = PROJECT_ROOT / "config/platform/toolchain-sources.json"
POLICY_PATH = PROJECT_ROOT / "config/platform/platform-selection-policy.yaml"
SMOKE_RTL = Path(__file__).resolve().parent / "rtl/two_flop_smoke.sv"
SMOKE_SDC = Path(__file__).resolve().parent / "constraints/two_flop_smoke.sdc"
DESIGN_NAME = "nova_m0_two_flop_smoke"
REQUIRED_SMOKE_TOOLS = ("yosys", "opensta", "openroad")


class SmokeCommandError(RuntimeError):
    pass


def _smoke_environment(
    *,
    canonical_environment: Mapping[str, tuple[str, ...]],
    environment_operations: Mapping[str, str],
    literal_environment: Mapping[str, str],
    scratch_root: Path,
) -> dict[str, str]:
    home = scratch_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
    }
    for name, operation in environment_operations.items():
        if operation == "SET_LITERAL":
            environment[name] = literal_environment[name]
        else:
            environment[name] = os.pathsep.join(canonical_environment[name])
    system_path = "/usr/bin:/bin"
    environment["PATH"] = (
        f"{environment['PATH']}:{system_path}" if environment.get("PATH") else system_path
    )
    return environment


def _validated_smoke_lock(
    verification: PlatformLockVerification,
    *,
    expected_manifest_hash: str,
    expected_policy_hash: str,
    verified_fingerprints: Mapping[str, ToolFingerprint],
) -> PlatformLock:
    if verification.status != "PASS" or verification.lock is None:
        raise RuntimeError("platform lock byte verification failed")
    lock = verification.lock
    if lock.source_manifest_hash != expected_manifest_hash:
        raise RuntimeError("platform lock does not match the production source manifest")
    if lock.selection_policy_hash != expected_policy_hash:
        raise RuntimeError("platform lock does not match the production selection policy")
    locked_fingerprints = {item.tool_id: item for item in lock.tool_fingerprints}
    for tool_id in REQUIRED_SMOKE_TOOLS:
        locked = locked_fingerprints.get(tool_id)
        verified = verified_fingerprints.get(tool_id)
        if locked is None or verified is None:
            raise RuntimeError(f"platform lock lacks required tool fingerprint: {tool_id}")
        if locked.model_dump(exclude={"executable"}) != verified.model_dump(
            exclude={"executable"}
        ):
            raise RuntimeError(f"platform lock tool fingerprint changed: {tool_id}")
    return lock


def _passing_verification(lock) -> PlatformLockVerification:
    return PlatformLockVerification(
        status="PASS",
        issues=(),
        lock_hash=None,
        lock=lock,
    )


def test_smoke_preflight_rejects_lock_from_another_manifest() -> None:
    lock = load_platform_lock(PROJECT_ROOT / "config/platform/platform.lock.yaml")
    changed = lock.model_copy(update={"source_manifest_hash": "sha256:" + "0" * 64})
    policy = load_platform_selection_policy(POLICY_PATH)

    with pytest.raises(RuntimeError, match="source manifest"):
        _validated_smoke_lock(
            _passing_verification(changed),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=selection_policy_content_identity_hash(policy),
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_preflight_rejects_lock_from_another_selection_policy() -> None:
    lock = load_platform_lock(PROJECT_ROOT / "config/platform/platform.lock.yaml")
    changed = lock.model_copy(update={"selection_policy_hash": "sha256:" + "0" * 64})

    with pytest.raises(RuntimeError, match="selection policy"):
        _validated_smoke_lock(
            _passing_verification(changed),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=lock.selection_policy_hash,
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_preflight_requires_yosys_opensta_and_openroad_fingerprints() -> None:
    lock = load_platform_lock(PROJECT_ROOT / "config/platform/platform.lock.yaml")
    missing_openroad = lock.model_copy(
        update={
            "tool_fingerprints": tuple(
                item for item in lock.tool_fingerprints if item.tool_id != "openroad"
            )
        }
    )
    policy = load_platform_selection_policy(POLICY_PATH)

    with pytest.raises(RuntimeError, match="required tool fingerprint: openroad"):
        _validated_smoke_lock(
            _passing_verification(missing_openroad),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=selection_policy_content_identity_hash(policy),
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_environment_does_not_inherit_make_or_orfs_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAKEFLAGS", "-e")
    monkeypatch.setenv("CORNER", "WC")
    monkeypatch.setenv("LIB_FILES", "/unreviewed.lib")
    monkeypatch.setenv("TMPDIR", "/unreviewed/tmp")

    environment = _smoke_environment(
        canonical_environment={
            "PATH": ("/verified/tools/bin",),
            "LD_LIBRARY_PATH": ("/verified/tools/lib",),
        },
        environment_operations={
            "PATH": "PREPEND_PATH",
            "LD_LIBRARY_PATH": "PREPEND_PATH",
            "PYTHONDONTWRITEBYTECODE": "SET_LITERAL",
        },
        literal_environment={"PYTHONDONTWRITEBYTECODE": "1"},
        scratch_root=tmp_path,
    )

    assert environment["PATH"] == "/verified/tools/bin:/usr/bin:/bin"
    assert environment["LD_LIBRARY_PATH"] == "/verified/tools/lib"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "MAKEFLAGS" not in environment
    assert "CORNER" not in environment
    assert "LIB_FILES" not in environment
    assert "TMPDIR" not in environment


def test_run_checked_persists_partial_log_and_kills_process_group_on_timeout(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "print('partial smoke output', flush=True); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )
    log_path = tmp_path / "timeout.log"

    with pytest.raises(RuntimeError, match="timed out"):
        _run_checked(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            environment=dict(os.environ),
            log_path=log_path,
            timeout_seconds=0.5,
        )

    assert "partial smoke output" in log_path.read_text(encoding="utf-8")
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()


def test_sta_script_rebinds_libraries_to_verified_orfs_root(tmp_path: Path) -> None:
    lock = load_platform_lock(PROJECT_ROOT / "config/platform/platform.lock.yaml")
    old_root = Path("/old/unverified/orfs")
    moved_setup = lock.setup_corner.model_copy(
        update={
            "liberty_files": tuple(
                artifact.model_copy(
                    update={"resolved_path": old_root / artifact.logical_path}
                )
                for artifact in lock.setup_corner.liberty_files
            )
        }
    )
    moved_lock = lock.model_copy(update={"setup_corner": moved_setup})
    verified_root = tmp_path / "verified-orfs"

    sta_environment = _sta_environment(
        moved_lock,
        "SETUP",
        artifact_root=verified_root,
        netlist=tmp_path / "netlist.v",
        sdc=tmp_path / "smoke.sdc",
    )

    assert all(str(old_root) not in value for value in sta_environment.values())
    assert sta_environment["NOVA_SMOKE_LIB_01"] == str(
        verified_root / lock.setup_corner.liberty_files[0].logical_path
    )


def test_sta_paths_are_passed_as_tcl_environment_values(tmp_path: Path) -> None:
    lock = load_platform_lock(PROJECT_ROOT / "config/platform/platform.lock.yaml")
    tricky_root = tmp_path / "root with $dollar {brace} #hash"
    netlist = tmp_path / "mapped $netlist.v"
    sdc = tmp_path / "smoke {constraints}.sdc"

    sta_environment = _sta_environment(
        lock,
        "HOLD",
        artifact_root=tricky_root,
        netlist=netlist,
        sdc=sdc,
    )
    script = _sta_script(lock, "HOLD")

    assert str(tricky_root) not in script
    assert str(netlist) not in script
    assert str(sdc) not in script
    assert sta_environment["NOVA_SMOKE_NETLIST"] == str(netlist)
    assert sta_environment["NOVA_SMOKE_SDC"] == str(sdc)
    assert "read_liberty $::env(NOVA_SMOKE_LIB_01)" in script


def test_orfs_config_does_not_interpolate_runtime_paths() -> None:
    config = _orfs_config()

    assert str(PROJECT_ROOT) not in config
    assert "$(value NOVA_SMOKE_RTL)" in config
    assert "$(value NOVA_SMOKE_SDC)" in config
    assert "$(value NOVA_SMOKE_WORK_HOME)" in config


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
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
        log_path.write_text(output, encoding="utf-8")
        raise SmokeCommandError(
            f"command timed out after {timeout_seconds}s: {' '.join(command)}\n"
            f"partial log: {log_path}\n{output[-12000:]}"
        ) from error
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    log_path.write_text(output, encoding="utf-8")
    completed = subprocess.CompletedProcess(command, process.returncode, output, None)
    if process.returncode != 0:
        raise SmokeCommandError(
            f"command failed with exit {process.returncode}: {' '.join(command)}\n"
            f"full log: {log_path}\n{output[-12000:]}"
        )
    return completed


def _orfs_config() -> str:
    return f"""\
export PLATFORM = asap7
export DESIGN_NAME = {DESIGN_NAME}
export DESIGN_NICKNAME = {DESIGN_NAME}
export VERILOG_FILES := $(value NOVA_SMOKE_RTL)
export SDC_FILE := $(value NOVA_SMOKE_SDC)
export WORK_HOME := $(value NOVA_SMOKE_WORK_HOME)
export FLOW_VARIANT = m0_smoke
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
    sdc: Path,
) -> dict[str, str]:
    corner = lock.setup_corner if check == "SETUP" else lock.hold_corner
    return {
        **{
            f"NOVA_SMOKE_LIB_{index:02d}": str(artifact_root / artifact.logical_path)
            for index, artifact in enumerate(corner.liberty_files, start=1)
        },
        "NOVA_SMOKE_NETLIST": str(netlist),
        "NOVA_SMOKE_SDC": str(sdc),
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
set paths [find_timing_paths \
  -from $registers -to $registers \
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
report_checks \
  -from $registers -to $registers \
  -path_delay {path_delay} -group_count 1 \
  -fields {{slew cap input_pin net fanout}}
exit
"""


@pytest.mark.integration
def test_locked_asap7_two_view_smoke_reaches_openroad_cts(
    platform_lock_path: Path,
    tmp_path: Path,
) -> None:
    assert SMOKE_RTL.is_file(), f"missing committed smoke RTL: {SMOKE_RTL}"
    assert SMOKE_SDC.is_file(), f"missing committed smoke SDC: {SMOKE_SDC}"

    loaded_manifest = load_toolchain_source_manifest(MANIFEST_PATH)
    selection_policy = load_platform_selection_policy(POLICY_PATH)
    verified = verify_toolchain(loaded_manifest.manifest, PROJECT_ROOT / ".nova-tools")
    orfs_root = verified.root / "components/orfs"
    lock_verification = verify_platform_lock(
        platform_lock_path,
        artifact_root=orfs_root,
        tool_paths=verified.tool_paths,
    )
    lock = _validated_smoke_lock(
        lock_verification,
        expected_manifest_hash=loaded_manifest.content_identity_hash,
        expected_policy_hash=selection_policy_content_identity_hash(selection_policy),
        verified_fingerprints=verified.tool_fingerprints,
    )
    assert lock.platform_id == "asap7"

    flow_home = orfs_root / "flow"
    work_home = tmp_path / "orfs-work"
    config_path = tmp_path / "config.mk"
    config_path.write_text(_orfs_config(), encoding="utf-8")
    environment = _smoke_environment(
        canonical_environment=verified.canonical_environment,
        environment_operations=verified.environment_operations,
        literal_environment=verified.literal_environment,
        scratch_root=tmp_path,
    )
    environment.update(
        {
            "OPENROAD_EXE": str(verified.tool_paths["openroad"]),
            "OPENSTA_EXE": str(verified.tool_paths["opensta"]),
            "YOSYS_EXE": str(verified.tool_paths["yosys"]),
            "PYTHON_EXE": sys.executable,
            "QT_QPA_PLATFORM": "offscreen",
            "NOVA_SMOKE_RTL": str(SMOKE_RTL),
            "NOVA_SMOKE_SDC": str(SMOKE_SDC),
            "NOVA_SMOKE_WORK_HOME": str(work_home),
        }
    )
    make_log = tmp_path / "openroad-cts.log"
    _run_checked(
        [
            "/usr/bin/make",
            "--directory",
            str(flow_home),
            f"DESIGN_CONFIG={config_path}",
            f"WORK_HOME={work_home}",
            "FLOW_VARIANT=m0_smoke",
            f"OPENROAD_EXE={verified.tool_paths['openroad']}",
            f"OPENSTA_EXE={verified.tool_paths['opensta']}",
            f"YOSYS_EXE={verified.tool_paths['yosys']}",
            "NUM_CORES=2",
            "cts",
        ],
        cwd=PROJECT_ROOT,
        environment=environment,
        log_path=make_log,
        timeout_seconds=900,
    )

    result_directory = work_home / "results/asap7" / DESIGN_NAME / "m0_smoke"
    netlist = result_directory / "1_2_yosys.v"
    synth_database = result_directory / "1_synth.odb"
    cts_database = result_directory / "4_cts.odb"
    assert netlist.stat().st_size > 0
    assert synth_database.stat().st_size > 0
    assert cts_database.stat().st_size > 0

    view_results: list[dict[str, object]] = []
    for check, corner in (("SETUP", lock.setup_corner), ("HOLD", lock.hold_corner)):
        script_path = tmp_path / f"{check.lower()}.tcl"
        report_path = tmp_path / f"{check.lower()}.rpt"
        script_path.write_text(_sta_script(lock, check), encoding="utf-8")
        sta_environment = {
            **environment,
            **_sta_environment(
                lock,
                check,
                artifact_root=orfs_root,
                netlist=netlist,
                sdc=SMOKE_SDC,
            ),
        }
        completed = _run_checked(
            [str(verified.tool_paths["opensta"]), str(script_path)],
            cwd=PROJECT_ROOT,
            environment=sta_environment,
            log_path=report_path,
            timeout_seconds=180,
        )
        assert f"NOVA_SMOKE_VIEW {corner.corner_id}" in completed.stdout
        assert f"NOVA_SMOKE_CHECK {check}" in completed.stdout
        assert "NOVA_SMOKE_REGISTER_COUNT 2" in completed.stdout
        assert "NOVA_SMOKE_PATH_COUNT 1" in completed.stdout
        assert "Startpoint: first_stage$_SDFF_PN0_" in completed.stdout
        assert "Endpoint: data_out$_SDFF_PN0_" in completed.stdout
        assert "Path Group: smoke_clock" in completed.stdout
        assert f"Path Type: {'max' if check == 'SETUP' else 'min'}" in completed.stdout
        assert "Error:" not in completed.stdout
        view_results.append(
            {
                "check": check,
                "corner_id": corner.corner_id,
                "report": str(report_path),
            }
        )

    summary = {
        "status": "PASS",
        "platform_lock": str(platform_lock_path),
        "synth_netlist": str(netlist),
        "cts_database": str(cts_database),
        "views": view_results,
    }
    (tmp_path / "smoke-report.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
