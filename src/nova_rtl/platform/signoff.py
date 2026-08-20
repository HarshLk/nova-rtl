"""Atomic publication helpers for the M0 sign-off evidence packet."""

from __future__ import annotations

import os
import shutil
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
    ToolchainReceipt,
)
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.doctor import run_doctor
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import (
    hash_file,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)
from nova_rtl.platform.smoke import PlatformSmokeRequest, run_platform_smoke

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


def publish_signoff_packet(
    *,
    destination: Path,
    report: M0SignoffReport,
    source_files: Mapping[str, Path],
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
        verify_m0_signoff_packet(staging)
        os.replace(staging, target)
    except (OSError, RuntimeError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, SignoffError):
            raise
        raise SignoffError(f"sign-off packet could not be published: {error}") from error
    return target / "m0-signoff.json"


def verify_m0_signoff_packet(packet_directory: Path) -> M0SignoffReport:
    """Fail closed if a published M0 packet or any nested smoke artifact changed."""

    root = packet_directory.resolve(strict=True)
    try:
        report = M0SignoffReport.model_validate_json(
            (root / "m0-signoff.json").read_text(encoding="utf-8")
        )
        for evidence in _report_evidence(report):
            candidate = root.joinpath(*PurePosixPath(evidence.relative_path).parts)
            if (
                not candidate.is_file()
                or candidate.stat().st_size != evidence.size_bytes
                or hash_file(candidate) != evidence.sha256
            ):
                raise SignoffError(
                    f"published sign-off evidence identity mismatch: {evidence.relative_path}"
                )
        doctor = DoctorReport.model_validate_json(
            (root / report.doctor_report.relative_path).read_text(encoding="utf-8")
        )
        if doctor.status != "PASS" or doctor.exit_code != 0:
            raise SignoffError("published doctor report is not passing")
        receipt = ToolchainReceipt.model_validate_json(
            (root / report.toolchain_receipt.relative_path).read_text(encoding="utf-8")
        )
        load_organizer_decisions(root / report.organizer_decisions.relative_path)
        load_m0_defaults(
            default_policy=root / report.default_policy.relative_path,
            cdc_patterns=root / report.cdc_patterns.relative_path,
            reset_assumptions=root / report.reset_assumptions.relative_path,
        )
        views = PlatformAnalysisViews.model_validate(
            yaml.safe_load((root / report.analysis_views.relative_path).read_text(encoding="utf-8"))
        )
        lock = load_platform_lock(root / report.platform_lock.relative_path)
        selection_policy = load_platform_selection_policy(
            root / report.selection_policy.relative_path
        )
        policy_hash = selection_policy_content_identity_hash(selection_policy)
        if policy_hash != lock.selection_policy_hash:
            raise SignoffError("published selection policy does not match the platform lock")
        if views.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published analysis views reference different platform lock bytes")
        validate_analysis_views(lock, views)
        smoke_path = root / report.smoke_report.relative_path
        smoke = PlatformSmokeReport.model_validate_json(smoke_path.read_text(encoding="utf-8"))
        if smoke.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published smoke report references different platform lock bytes")
        if smoke.toolchain_receipt_hash != report.toolchain_receipt.sha256:
            raise SignoffError("published smoke report references a different toolchain receipt")
        if smoke.source_manifest_hash != receipt.manifest_hash:
            raise SignoffError("published smoke report references a different source manifest")
        if smoke.selection_policy_hash != policy_hash:
            raise SignoffError("published smoke report references a different selection policy")
        if doctor.platform_lock_hash != report.platform_lock.sha256:
            raise SignoffError("published doctor report references different platform lock bytes")
        receipt_tools = {item.tool_id: item for item in receipt.tool_fingerprints}
        for check in doctor.checks:
            if check.tool_fingerprint is None:
                continue
            expected = receipt_tools.get(check.name)
            if expected is None or expected.model_dump() != check.tool_fingerprint.model_dump():
                raise SignoffError(
                    f"published doctor fingerprint disagrees with toolchain receipt: {check.name}"
                )
        smoke_root = smoke_path.parent
        for evidence in _smoke_evidence(smoke):
            candidate = smoke_root.joinpath(*PurePosixPath(evidence.relative_path).parts)
            if (
                not candidate.is_file()
                or candidate.stat().st_size != evidence.size_bytes
                or hash_file(candidate) != evidence.sha256
            ):
                raise SignoffError(
                    f"published smoke evidence identity mismatch: {evidence.relative_path}"
                )
    except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as error:
        raise SignoffError(f"M0 sign-off packet verification failed: {error}") from error
    return report


def run_m0_signoff(
    request: M0SignoffRequest,
    *,
    generated_at: AwareDatetime | None = None,
) -> tuple[Path, M0SignoffReport]:
    """Execute every M0 exit gate and atomically publish its required evidence packet."""

    destination = request.output_directory.absolute()
    if destination.exists():
        raise SignoffError(f"sign-off destination already exists: {destination}")
    timestamp = generated_at or datetime.now(UTC)
    project_root = request.project_root.resolve(strict=True)
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
    views = PlatformAnalysisViews.model_validate(
        yaml.safe_load(request.analysis_views.read_text(encoding="utf-8"))
    )
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

        doctor_path = scratch / "doctor-report.json"
        doctor_path.write_bytes(canonical_json_bytes(doctor) + b"\n")
        smoke_report_path = smoke_root / "smoke-report.json"
        report = M0SignoffReport(
            milestone="M0",
            status="PASS",
            generated_at=timestamp,
            exit_code=0,
            doctor_report=_evidence_file(doctor_path, "doctor-report.json"),
            toolchain_receipt=_evidence_file(
                verified.receipt_path,
                "toolchain-receipt.json",
            ),
            platform_lock=_evidence_file(request.platform_lock, "platform.lock.yaml"),
            selection_policy=_evidence_file(
                request.selection_policy,
                "platform-selection-policy.yaml",
            ),
            analysis_views=_evidence_file(request.analysis_views, "analysis-views.yaml"),
            organizer_decisions=_evidence_file(
                request.organizer_decisions,
                "organizer-decisions.yaml",
            ),
            default_policy=_evidence_file(request.default_policy, "default-policy.yaml"),
            cdc_patterns=_evidence_file(request.cdc_patterns, "cdc-patterns.yaml"),
            reset_assumptions=_evidence_file(
                request.reset_assumptions,
                "reset-assumptions.yaml",
            ),
            smoke_report=_evidence_file(smoke_report_path, "smoke/smoke-report.json"),
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
        )
    verified_report = verify_m0_signoff_packet(destination)
    return report_path, verified_report


__all__ = [
    "M0SignoffRequest",
    "M0Defaults",
    "SignoffError",
    "load_m0_defaults",
    "load_organizer_decisions",
    "publish_signoff_packet",
    "run_m0_signoff",
    "validate_analysis_views",
    "verify_m0_signoff_packet",
]
