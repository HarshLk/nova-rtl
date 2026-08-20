"""Atomic publication helpers for the M0 sign-off evidence packet."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from nova_rtl.contracts.base import AwareDatetime, EntityId, StrictContract, canonical_json_bytes
from nova_rtl.contracts.platform import (
    DoctorReport,
    M0SignoffReport,
    OrganizerDecisions,
    PlatformAnalysisViews,
    PlatformLock,
    PlatformSmokeReport,
    SignoffEvidenceFile,
    SmokeTimingView,
    ToolchainReceipt,
)
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.doctor import run_doctor
from nova_rtl.platform.hydration import (
    load_toolchain_source_manifest,
    manifest_content_identity_hash,
)
from nova_rtl.platform.lock import (
    hash_file,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)
from nova_rtl.platform.smoke import (
    PlatformSmokeRequest,
    _execution_environment_identity_hash,
    _require_output_outside_roots,
    parse_timing_evidence,
    run_platform_smoke,
)

M0_REQUIRED_TOOLS = (
    "yosys",
    "opensta",
    "openroad",
    "eqy",
    "sby",
    "slang",
    "iverilog",
    "verilator",
)


class SignoffError(RuntimeError):
    """Raised when the M0 evidence packet cannot be published safely."""


@dataclass(frozen=True)
class M0SignoffRequest:
    """Committed M0 inputs and the new immutable packet destination."""

    project_root: Path
    toolchain_manifest: Path
    selection_policy: Path
    platform_lock: Path
    analysis_views: Path
    organizer_decisions: Path
    default_policy: Path
    cdc_patterns: Path
    reset_assumptions: Path
    smoke_rtl: Path
    smoke_constraints: Path
    output_directory: Path


@dataclass(frozen=True)
class GitCheckpoint:
    """Clean committed implementation identity used to authorize one sign-off run."""

    commit: str
    tree_hash: str


def capture_git_checkpoint(project_root: Path) -> GitCheckpoint:
    """Return HEAD identities only when the complete repository worktree is clean."""

    root = project_root.resolve(strict=True)
    git = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
    if git is None:
        raise SignoffError("Git is required to identify the M0 implementation checkpoint")
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}

    def query(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                [git, "-C", str(root), *arguments],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SignoffError(f"Git checkpoint query failed: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SignoffError(f"Git checkpoint query failed: {detail or completed.returncode}")
        return completed.stdout.strip()

    if query("status", "--porcelain=v1", "--untracked-files=all"):
        raise SignoffError("repository working tree is not clean at the M0 checkpoint")
    commit = query("rev-parse", "--verify", "HEAD")
    tree_hash = query("rev-parse", "--verify", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(
        r"[0-9a-f]{40}", tree_hash
    ):
        raise SignoffError("Git checkpoint did not produce full SHA-1 identities")
    return GitCheckpoint(commit=commit, tree_hash=tree_hash)


def _signoff_invocation_hash(
    *,
    implementation_commit: str,
    implementation_tree_hash: str,
    source_manifest_hash: str,
    selection_policy_hash: str,
    platform_lock_hash: str,
    analysis_views_hash: str,
    organizer_decisions_hash: str,
    default_policy_hash: str,
    cdc_patterns_hash: str,
    reset_assumptions_hash: str,
    rtl_hash: str,
    constraints_hash: str,
) -> str:
    """Hash the normalized semantic inputs of an M0 sign-off invocation."""

    payload = {
        "implementation_commit": implementation_commit,
        "implementation_tree_hash": implementation_tree_hash,
        "source_manifest_hash": source_manifest_hash,
        "selection_policy_hash": selection_policy_hash,
        "platform_lock_hash": platform_lock_hash,
        "analysis_views_hash": analysis_views_hash,
        "organizer_decisions_hash": organizer_decisions_hash,
        "default_policy_hash": default_policy_hash,
        "cdc_patterns_hash": cdc_patterns_hash,
        "reset_assumptions_hash": reset_assumptions_hash,
        "rtl_hash": rtl_hash,
        "constraints_hash": constraints_hash,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class _M0DefaultPolicy(StrictContract):
    schema_version: Literal[1] = 1
    policy_id: EntityId
    primary_correctness: Literal["STRICT_SEQ_EQUIV"]
    missing_evidence_policy: Literal["FAIL_CLOSED"]
    allow_sdc_edits: Literal[False]
    allow_latency_change: Literal[False]
    allow_protected_structure_edits: Literal[False]
    deterministic_seed: Literal[0]


class _CdcPattern(StrictContract):
    pattern_id: EntityId
    kind: Literal[
        "TWO_FLOP_LEVEL",
        "TOGGLE_PULSE",
        "REQUEST_ACKNOWLEDGE",
        "ASYNC_FIFO_GRAY",
        "GRAY_POINTER",
        "RESET_SYNCHRONIZER",
    ]
    protected: Literal[True]


class _CdcPatternRegistry(StrictContract):
    schema_version: Literal[1] = 1
    registry_id: EntityId
    claim_scope: Literal["STRUCTURAL_CDC_INVARIANT_AUDIT"]
    unsupported_crossing_policy: Literal["FAIL_CLOSED"]
    patterns: tuple[_CdcPattern, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def registry_covers_each_approved_kind_once(self) -> _CdcPatternRegistry:
        expected = {
            "TWO_FLOP_LEVEL",
            "TOGGLE_PULSE",
            "REQUEST_ACKNOWLEDGE",
            "ASYNC_FIFO_GRAY",
            "GRAY_POINTER",
            "RESET_SYNCHRONIZER",
        }
        kinds = [pattern.kind for pattern in self.patterns]
        if set(kinds) != expected or len(kinds) != len(expected):
            raise ValueError("CDC registry must contain each approved pattern kind exactly once")
        if len({pattern.pattern_id for pattern in self.patterns}) != len(self.patterns):
            raise ValueError("CDC pattern IDs must be unique")
        return self


class _ResetAssumptionManifest(StrictContract):
    schema_version: Literal[1] = 1
    manifest_id: EntityId
    master_clock_model: Literal["INDEPENDENT_SHARED_GOLD_GATE_EVENTS"]
    generated_clock_model: Literal["DERIVED_FROM_PROTECTED_DIVIDER_STATE"]
    initial_reset_state: Literal["ASSERTED"]
    reset_assertion: Literal["ASYNCHRONOUS_ALLOWED"]
    reset_release: Literal["SYNCHRONOUS_PER_DOMAIN"]
    minimum_asserted_master_events: int = Field(ge=2)
    unknown_initial_state_policy: Literal["FAIL_CLOSED"]


@dataclass(frozen=True)
class M0Defaults:
    policy: _M0DefaultPolicy
    cdc: _CdcPatternRegistry
    reset: _ResetAssumptionManifest


def load_organizer_decisions(path: Path) -> OrganizerDecisions:
    """Load the organizer decision record under its strict versioned contract."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return OrganizerDecisions.model_validate(payload)
    except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as error:
        raise SignoffError(f"organizer decision record is invalid: {error}") from error


def load_analysis_views(path: Path) -> PlatformAnalysisViews:
    """Load required setup/hold views through one structured failure boundary."""

    try:
        return PlatformAnalysisViews.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as error:
        raise SignoffError(f"analysis views are invalid: {error}") from error


def load_m0_defaults(
    *,
    default_policy: Path,
    cdc_patterns: Path,
    reset_assumptions: Path,
) -> M0Defaults:
    """Load the three independently versioned conservative M0 policy inputs."""

    try:
        return M0Defaults(
            policy=_M0DefaultPolicy.model_validate(
                yaml.safe_load(default_policy.read_text(encoding="utf-8"))
            ),
            cdc=_CdcPatternRegistry.model_validate(
                yaml.safe_load(cdc_patterns.read_text(encoding="utf-8"))
            ),
            reset=_ResetAssumptionManifest.model_validate(
                yaml.safe_load(reset_assumptions.read_text(encoding="utf-8"))
            ),
        )
    except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as error:
        raise SignoffError(f"M0 default configuration is invalid: {error}") from error


def validate_analysis_views(lock: PlatformLock, views: PlatformAnalysisViews) -> None:
    """Correlate every required view identity back to the selected lock."""

    setup, hold = views.views
    if views.platform_id != lock.platform_id:
        raise SignoffError("analysis views and platform lock use different platforms")
    if setup.liberty_corner_id != lock.setup_corner.corner_id:
        raise SignoffError("analysis setup corner does not match the platform lock")
    if hold.liberty_corner_id != lock.hold_corner.corner_id:
        raise SignoffError("analysis hold corner does not match the platform lock")
    if setup.liberty_artifact_hashes != tuple(
        artifact.sha256 for artifact in lock.setup_corner.liberty_files
    ):
        raise SignoffError("analysis setup Liberty hashes do not match the platform lock")
    if hold.liberty_artifact_hashes != tuple(
        artifact.sha256 for artifact in lock.hold_corner.liberty_files
    ):
        raise SignoffError("analysis hold Liberty hashes do not match the platform lock")
    if (
        setup.rc_artifact_hash != lock.rc_rules.sha256
        or hold.rc_artifact_hash != lock.rc_rules.sha256
    ):
        raise SignoffError("analysis RC hashes do not match the platform lock")
    if setup.operating_conditions != lock.setup_corner.operating_conditions:
        raise SignoffError("analysis setup operating conditions do not match the platform lock")
    if hold.operating_conditions != lock.hold_corner.operating_conditions:
        raise SignoffError("analysis hold operating conditions do not match the platform lock")
    required_stages = {"OPENSTA_FULL", "OPENROAD_PHYSICAL"}
    if any(set(view.required_stages) != required_stages for view in views.views):
        raise SignoffError("analysis views do not require both STA and physical evidence")


def _report_evidence(report: M0SignoffReport) -> tuple[SignoffEvidenceFile, ...]:
    return (
        report.doctor_report,
        report.toolchain_receipt,
        report.platform_lock,
        report.selection_policy,
        report.analysis_views,
        report.organizer_decisions,
        report.default_policy,
        report.cdc_patterns,
        report.reset_assumptions,
        report.smoke_report,
    )


def _evidence_file(path: Path, relative_path: str) -> SignoffEvidenceFile:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size <= 0:
        raise SignoffError(f"required sign-off evidence is missing or empty: {path}")
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_file(resolved),
        size_bytes=resolved.stat().st_size,
    )


def _smoke_evidence(report: PlatformSmokeReport) -> tuple[SignoffEvidenceFile, ...]:
    return (
        report.rtl,
        report.constraints,
        report.smoke_config,
        report.openroad_log,
        report.mapped_netlist,
        report.synthesis_database,
        report.cts_database,
        report.setup_view.report,
        report.hold_view.report,
    )


def _packet_file(root: Path, relative_path: str) -> Path:
    """Resolve one packet-local regular file without traversing any symlink."""

    relative = PurePosixPath(relative_path)
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise SignoffError(f"published sign-off evidence traverses a symlink: {relative_path}")
    if not candidate.is_file():
        raise SignoffError(f"published sign-off evidence is not a regular file: {relative_path}")
    return candidate


def _require_exact_packet_entries(root: Path, expected_files: set[str]) -> None:
    actual_files: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            candidate = directory_path / name
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                raise SignoffError(f"published sign-off packet contains symlink: {relative}")
        for name in file_names:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise SignoffError(f"published sign-off packet contains symlink: {relative}")
            actual_files.add(relative)
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise SignoffError(
            f"unexpected packet entries; unexpected={unexpected}, missing={missing}"
        )


def _timing_summary_matches_raw(
    *,
    smoke_root: Path,
    view: SmokeTimingView,
) -> None:
    report_path = _packet_file(smoke_root, view.report.relative_path)
    parsed = parse_timing_evidence(report_path.read_text(encoding="utf-8"), view.check)
    if (
        parsed.corner_id != view.corner_id
        or parsed.path_type != view.path_type
        or parsed.path_group != view.path_group
        or parsed.register_count != view.register_count
        or parsed.path_count != view.path_count
        or parsed.slack_ps != view.slack_ps
    ):
        raise SignoffError(f"{view.check} timing summary disagrees with its raw report")


def publish_signoff_packet(
    *,
    destination: Path,
    report: M0SignoffReport,
    source_files: Mapping[str, Path],
    project_root: Path | None = None,
) -> Path:
    """Copy verified evidence and atomically publish a new immutable packet."""

    target = destination.absolute()
    if target.exists():
        raise SignoffError(f"sign-off destination already exists: {target}")
    evidence_by_path = {item.relative_path: item for item in _report_evidence(report)}
    smoke_source = source_files.get(report.smoke_report.relative_path)
    if smoke_source is None:
        raise SignoffError("sign-off source set does not match the report evidence index")
    try:
        smoke_report = PlatformSmokeReport.model_validate_json(
            smoke_source.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as error:
        raise SignoffError(f"smoke evidence index is invalid: {error}") from error
    nested_paths = {
        f"smoke/{item.relative_path}": item for item in _smoke_evidence(smoke_report)
    }
    expected_paths = set(evidence_by_path) | set(nested_paths)
    if set(source_files) != expected_paths:
        raise SignoffError("sign-off source set does not match the report evidence index")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".m0-signoff-", dir=target.parent))
    try:
        for relative_path, source in source_files.items():
            evidence = evidence_by_path.get(relative_path) or nested_paths.get(relative_path)
            relative = PurePosixPath(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise SignoffError(f"unsafe sign-off evidence path: {relative_path}")
            if source.is_symlink():
                raise SignoffError(f"sign-off evidence may not be a symlink: {source}")
            resolved_source = source.resolve(strict=True)
            if not resolved_source.is_file():
                raise SignoffError(f"sign-off evidence is not a regular file: {source}")
            if evidence is not None and (
                resolved_source.stat().st_size != evidence.size_bytes
                or hash_file(resolved_source) != evidence.sha256
            ):
                raise SignoffError(f"sign-off evidence identity mismatch: {source}")
            staged_file = staging.joinpath(*relative.parts)
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved_source, staged_file)
            if evidence is not None and (
                staged_file.stat().st_size != evidence.size_bytes
                or hash_file(staged_file) != evidence.sha256
                or hash_file(resolved_source) != evidence.sha256
            ):
                raise SignoffError(f"sign-off evidence changed while copying: {source}")
        (staging / "m0-signoff.json").write_bytes(canonical_json_bytes(report) + b"\n")
        verify_m0_signoff_packet(staging, project_root=project_root)
        os.replace(staging, target)
    except (OSError, RuntimeError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, SignoffError):
            raise
        raise SignoffError(f"sign-off packet could not be published: {error}") from error
    return target / "m0-signoff.json"


def verify_m0_signoff_packet(
    packet_directory: Path,
    *,
    project_root: Path | None = None,
) -> M0SignoffReport:
    """Fail closed if a published M0 packet or any nested smoke artifact changed."""

    root = packet_directory.resolve(strict=True)
    try:
        report_path = _packet_file(root, "m0-signoff.json")
        report = M0SignoffReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        for evidence in _report_evidence(report):
            candidate = _packet_file(root, evidence.relative_path)
            if (
                candidate.stat().st_size != evidence.size_bytes
                or hash_file(candidate) != evidence.sha256
            ):
                raise SignoffError(
                    f"published sign-off evidence identity mismatch: {evidence.relative_path}"
                )
        doctor = DoctorReport.model_validate_json(
            _packet_file(root, report.doctor_report.relative_path).read_text(encoding="utf-8")
        )
        if doctor.status != "PASS" or doctor.exit_code != 0:
            raise SignoffError("published doctor report is not passing")
        receipt = ToolchainReceipt.model_validate_json(
            _packet_file(root, report.toolchain_receipt.relative_path).read_text(encoding="utf-8")
        )
        load_organizer_decisions(_packet_file(root, report.organizer_decisions.relative_path))
        load_m0_defaults(
            default_policy=_packet_file(root, report.default_policy.relative_path),
            cdc_patterns=_packet_file(root, report.cdc_patterns.relative_path),
            reset_assumptions=_packet_file(root, report.reset_assumptions.relative_path),
        )
        views = load_analysis_views(_packet_file(root, report.analysis_views.relative_path))
        lock = load_platform_lock(_packet_file(root, report.platform_lock.relative_path))
        selection_policy = load_platform_selection_policy(
            _packet_file(root, report.selection_policy.relative_path)
        )
        policy_hash = selection_policy_content_identity_hash(selection_policy)
        if policy_hash != lock.selection_policy_hash:
            raise SignoffError("published selection policy does not match the platform lock")
        if views.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published analysis views reference different platform lock bytes")
        validate_analysis_views(lock, views)
        smoke_path = _packet_file(root, report.smoke_report.relative_path)
        smoke = PlatformSmokeReport.model_validate_json(smoke_path.read_text(encoding="utf-8"))
        expected_files = {
            "m0-signoff.json",
            *(item.relative_path for item in _report_evidence(report)),
            *(
                (PurePosixPath(report.smoke_report.relative_path).parent / item.relative_path)
                .as_posix()
                for item in _smoke_evidence(smoke)
            ),
        }
        _require_exact_packet_entries(root, expected_files)
        if smoke.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published smoke report references different platform lock bytes")
        if smoke.toolchain_receipt_hash != report.toolchain_receipt.sha256:
            raise SignoffError("published smoke report references a different toolchain receipt")
        receipt_manifest_hash = manifest_content_identity_hash(receipt.manifest)
        if (
            receipt.manifest_hash != receipt_manifest_hash
            or lock.source_manifest_hash != receipt_manifest_hash
            or smoke.source_manifest_hash != receipt_manifest_hash
        ):
            raise SignoffError("published smoke report references a different source manifest")
        if smoke.selection_policy_hash != policy_hash:
            raise SignoffError("published smoke report references a different selection policy")
        if doctor.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published doctor report references different platform lock bytes")
        required_check_names = (*M0_REQUIRED_TOOLS, "platform_lock")
        if tuple(check.name for check in doctor.checks) != required_check_names:
            raise SignoffError("published doctor report does not contain the exact M0 check set")
        receipt_tools = {item.tool_id: item for item in receipt.tool_fingerprints}
        for check in doctor.checks:
            if check.tool_fingerprint is None:
                continue
            expected = receipt_tools.get(check.name)
            if expected is None or expected.model_dump() != check.tool_fingerprint.model_dump():
                raise SignoffError(
                    f"published doctor fingerprint disagrees with toolchain receipt: {check.name}"
                )
        platform_check = doctor.checks[-1]
        if (
            platform_check.tool_fingerprint is not None
            or platform_check.artifact_hash != report.platform_lock.sha256
        ):
            raise SignoffError("published doctor platform-lock check has the wrong identity")
        lock_tools = {item.tool_id: item for item in lock.tool_fingerprints}
        if set(lock_tools) != set(receipt_tools) or any(
            lock_tools[tool_id].model_dump() != receipt_tools[tool_id].model_dump()
            for tool_id in lock_tools
        ):
            raise SignoffError("published platform lock disagrees with toolchain receipt")
        expected_environment_hash = _execution_environment_identity_hash(
            receipt.model_dump(mode="json")["environment"],
            make_sha256=smoke.make_executable_sha256,
            make_version=smoke.make_version,
            python_sha256=smoke.python_executable_sha256,
            python_version=smoke.python_version,
        )
        if (
            smoke.execution_environment_hash != expected_environment_hash
            or report.execution_environment_hash != expected_environment_hash
        ):
            raise SignoffError("published execution environment identity is incoherent")
        smoke_root = smoke_path.parent
        for evidence in _smoke_evidence(smoke):
            candidate = _packet_file(smoke_root, evidence.relative_path)
            if (
                candidate.stat().st_size != evidence.size_bytes
                or hash_file(candidate) != evidence.sha256
            ):
                raise SignoffError(
                    f"published smoke evidence identity mismatch: {evidence.relative_path}"
                )
        if (
            smoke.setup_view.corner_id != lock.setup_corner.corner_id
            or smoke.hold_view.corner_id != lock.hold_corner.corner_id
        ):
            raise SignoffError("published smoke timing corners disagree with the platform lock")
        _timing_summary_matches_raw(smoke_root=smoke_root, view=smoke.setup_view)
        _timing_summary_matches_raw(smoke_root=smoke_root, view=smoke.hold_view)
        expected_invocation_hash = _signoff_invocation_hash(
            implementation_commit=report.implementation_commit,
            implementation_tree_hash=report.implementation_tree_hash,
            source_manifest_hash=receipt.manifest_hash,
            selection_policy_hash=policy_hash,
            platform_lock_hash=report.platform_lock.sha256,
            analysis_views_hash=report.analysis_views.sha256,
            organizer_decisions_hash=report.organizer_decisions.sha256,
            default_policy_hash=report.default_policy.sha256,
            cdc_patterns_hash=report.cdc_patterns.sha256,
            reset_assumptions_hash=report.reset_assumptions.sha256,
            rtl_hash=smoke.rtl.sha256,
            constraints_hash=smoke.constraints.sha256,
        )
        if report.signoff_invocation_hash != expected_invocation_hash:
            raise SignoffError("published sign-off invocation identity is incoherent")
        if project_root is not None:
            checkpoint = capture_git_checkpoint(project_root)
            if (
                checkpoint.commit != report.implementation_commit
                or checkpoint.tree_hash != report.implementation_tree_hash
            ):
                raise SignoffError("published packet does not match the current Git checkpoint")
    except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as error:
        raise SignoffError(f"M0 sign-off packet verification failed: {error}") from error
    return report


def run_m0_signoff(
    request: M0SignoffRequest,
    *,
    generated_at: AwareDatetime | None = None,
) -> tuple[Path, M0SignoffReport]:
    """Execute every M0 exit gate and atomically publish its required evidence packet."""

    destination = request.output_directory.resolve(strict=False)
    if destination.exists():
        raise SignoffError(f"sign-off destination already exists: {destination}")
    timestamp = generated_at or datetime.now(UTC)
    project_root = request.project_root.resolve(strict=True)
    checkpoint = capture_git_checkpoint(project_root)
    load_organizer_decisions(request.organizer_decisions)
    load_m0_defaults(
        default_policy=request.default_policy,
        cdc_patterns=request.cdc_patterns,
        reset_assumptions=request.reset_assumptions,
    )
    loaded = load_toolchain_source_manifest(request.toolchain_manifest)
    verified = verify_toolchain(
        loaded.manifest,
        project_root / loaded.manifest.tool_root_name,
    )
    destination = _require_output_outside_roots(destination, (verified.root,))
    orfs_root = verified.root / "components/orfs"
    doctor = run_doctor(
        required_tools=M0_REQUIRED_TOOLS,
        platform_lock=request.platform_lock,
        hydrated_tools=verified.tool_paths,
        probe_environment=verified.execution_environment(),
        platform_artifact_root=orfs_root,
    )
    if doctor.status != "PASS" or doctor.exit_code != 0:
        details = "; ".join(
            f"{check.name}:{check.message}" for check in doctor.checks if check.status == "FAIL"
        )
        raise SignoffError(f"M0 doctor gate failed: {details}")
    views = load_analysis_views(request.analysis_views)
    lock = load_platform_lock(request.platform_lock)
    if views.platform_lock_hash != hash_file(request.platform_lock):
        raise SignoffError("analysis views do not reference the selected platform lock bytes")
    validate_analysis_views(lock, views)

    with tempfile.TemporaryDirectory(prefix="nova-m0-signoff-") as scratch_name:
        scratch = Path(scratch_name)
        smoke_root = scratch / "smoke"
        smoke = run_platform_smoke(
            PlatformSmokeRequest(
                project_root=project_root,
                toolchain_manifest=request.toolchain_manifest,
                selection_policy=request.selection_policy,
                platform_lock=request.platform_lock,
                rtl=request.smoke_rtl,
                constraints=request.smoke_constraints,
                output_directory=smoke_root,
            ),
            generated_at=timestamp,
        )
        if views.platform_id != smoke.platform_id:
            raise SignoffError("analysis views and smoke report use different platforms")
        final_checkpoint = capture_git_checkpoint(project_root)
        if final_checkpoint != checkpoint:
            raise SignoffError("Git implementation checkpoint changed during M0 sign-off")

        doctor_path = scratch / "doctor-report.json"
        doctor_path.write_bytes(canonical_json_bytes(doctor) + b"\n")
        smoke_report_path = smoke_root / "smoke-report.json"
        doctor_evidence = _evidence_file(doctor_path, "doctor-report.json")
        receipt_evidence = _evidence_file(verified.receipt_path, "toolchain-receipt.json")
        lock_evidence = _evidence_file(request.platform_lock, "platform.lock.yaml")
        selection_evidence = _evidence_file(
            request.selection_policy,
            "platform-selection-policy.yaml",
        )
        views_evidence = _evidence_file(request.analysis_views, "analysis-views.yaml")
        organizer_evidence = _evidence_file(
            request.organizer_decisions,
            "organizer-decisions.yaml",
        )
        default_evidence = _evidence_file(request.default_policy, "default-policy.yaml")
        cdc_evidence = _evidence_file(request.cdc_patterns, "cdc-patterns.yaml")
        reset_evidence = _evidence_file(
            request.reset_assumptions,
            "reset-assumptions.yaml",
        )
        smoke_evidence = _evidence_file(smoke_report_path, "smoke/smoke-report.json")
        report = M0SignoffReport(
            milestone="M0",
            status="PASS",
            generated_at=timestamp,
            exit_code=0,
            implementation_commit=checkpoint.commit,
            implementation_tree_hash=checkpoint.tree_hash,
            signoff_invocation_hash=_signoff_invocation_hash(
                implementation_commit=checkpoint.commit,
                implementation_tree_hash=checkpoint.tree_hash,
                source_manifest_hash=smoke.source_manifest_hash,
                selection_policy_hash=smoke.selection_policy_hash,
                platform_lock_hash=lock_evidence.sha256,
                analysis_views_hash=views_evidence.sha256,
                organizer_decisions_hash=organizer_evidence.sha256,
                default_policy_hash=default_evidence.sha256,
                cdc_patterns_hash=cdc_evidence.sha256,
                reset_assumptions_hash=reset_evidence.sha256,
                rtl_hash=smoke.rtl.sha256,
                constraints_hash=smoke.constraints.sha256,
            ),
            execution_environment_hash=smoke.execution_environment_hash,
            doctor_report=doctor_evidence,
            toolchain_receipt=receipt_evidence,
            platform_lock=lock_evidence,
            selection_policy=selection_evidence,
            analysis_views=views_evidence,
            organizer_decisions=organizer_evidence,
            default_policy=default_evidence,
            cdc_patterns=cdc_evidence,
            reset_assumptions=reset_evidence,
            smoke_report=smoke_evidence,
        )
        source_files = {
            evidence.relative_path: source
            for evidence, source in (
                (report.doctor_report, doctor_path),
                (report.toolchain_receipt, verified.receipt_path),
                (report.platform_lock, request.platform_lock),
                (report.selection_policy, request.selection_policy),
                (report.analysis_views, request.analysis_views),
                (report.organizer_decisions, request.organizer_decisions),
                (report.default_policy, request.default_policy),
                (report.cdc_patterns, request.cdc_patterns),
                (report.reset_assumptions, request.reset_assumptions),
                (report.smoke_report, smoke_report_path),
            )
        }
        source_files.update(
            {
                f"smoke/{path.relative_to(smoke_root).as_posix()}": path
                for path in smoke_root.rglob("*")
                if path.is_file() and path != smoke_report_path
            }
        )
        report_path = publish_signoff_packet(
            destination=destination,
            report=report,
            source_files=source_files,
            project_root=project_root,
        )
    verified_report = verify_m0_signoff_packet(destination, project_root=project_root)
    return report_path, verified_report


__all__ = [
    "M0SignoffRequest",
    "M0Defaults",
    "SignoffError",
    "capture_git_checkpoint",
    "load_analysis_views",
    "load_m0_defaults",
    "load_organizer_decisions",
    "publish_signoff_packet",
    "run_m0_signoff",
    "validate_analysis_views",
    "verify_m0_signoff_packet",
]
