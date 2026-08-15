"""Platform-readiness contracts used by the bootstrap doctor."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import AwareDatetime, EntityId, HashRef, StrictContract

GitCommit = str
PROBE_VERSION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "yosys": ("-V",),
    "opensta": ("-version",),
    "openroad": ("-version",),
    "eqy": ("--version",),
    "sby": ("--version",),
    "slang": ("--version",),
    "iverilog": ("-V",),
    "verilator": ("--version",),
}
DoctorIssueCode = Literal[
    "ARTIFACT_CHANGED_DURING_VERIFY",
    "ARTIFACT_HASH_MISMATCH",
    "ARTIFACT_IO_ERROR",
    "ARTIFACT_MISSING",
    "ARTIFACT_SIZE_MISMATCH",
    "CONTENT_IDENTITY_MISMATCH",
    "INVALID_PLATFORM_LOCK",
    "PLATFORM_LOCK_IO_ERROR",
    "PLATFORM_LOCK_MISSING",
    "TOOL_EXECUTABLE_HASH_MISMATCH",
    "TOOL_HASH_MISMATCH",
    "TOOL_IO_ERROR",
    "TOOL_LOCK_MISMATCH",
    "TOOL_MISSING",
    "TOOL_PROBE_FAILED",
    "TOOL_VERSION_MISMATCH",
]


def _expected_version_args(tool_id: str) -> tuple[str, ...]:
    return PROBE_VERSION_ARGUMENTS.get(tool_id, ("--version",))


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    contains_control = any(ord(character) < 32 or ord(character) == 127 for character in value)
    if (
        not value
        or "\\" in value
        or contains_control
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("path must be a normalized relative path")
    return value


class HostPlatform(StrictContract):
    """Supported host identity for one portable toolchain realization."""

    os: Literal["linux"]
    architecture: Literal["x86_64"]


class ToolExecutableSource(StrictContract):
    """One executable expected from a pinned toolchain component."""

    tool_id: EntityId
    relative_path: str
    version_args: tuple[str, ...]

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @model_validator(mode="after")
    def probe_recipe_is_registered(self) -> Self:
        if self.version_args != _expected_version_args(self.tool_id):
            raise ValueError("version_args do not match the registered probe recipe")
        return self


class ToolSource(StrictContract):
    """Immutable acquisition source for one portable toolchain component."""

    component_id: EntityId
    source_kind: Literal["ARCHIVE", "GIT"]
    version: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    archive_sha256: HashRef | None
    git_commit: GitCommit | None
    license: str = Field(min_length=1)
    executables: tuple[ToolExecutableSource, ...]

    @model_validator(mode="after")
    def source_is_immutable(self) -> Self:
        normalized_url = self.source_url.lower().rstrip("/")
        if "/latest/" in normalized_url or normalized_url.endswith("/latest"):
            raise ValueError("moving latest URL is not permitted")
        if self.source_kind == "ARCHIVE":
            if self.archive_sha256 is None or self.git_commit is not None:
                raise ValueError("ARCHIVE source requires archive_sha256 and no git_commit")
        elif self.archive_sha256 is not None or not re.fullmatch(
            r"[0-9a-f]{40}", self.git_commit or ""
        ):
            raise ValueError("GIT source requires a full 40-character Git commit")
        tool_ids = [item.tool_id for item in self.executables]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("duplicate tool_id within component")
        return self


class ToolchainSourceManifest(StrictContract):
    """Portable, human-reviewed acquisition manifest for M0 tools."""

    schema_version: Literal[1] = 1
    host: HostPlatform
    tool_root_name: str
    components: tuple[ToolSource, ...] = Field(min_length=1)

    @field_validator("tool_root_name")
    @classmethod
    def tool_root_is_one_relative_directory(cls, value: str) -> str:
        validated = _validate_relative_path(value)
        if len(PurePosixPath(validated).parts) != 1:
            raise ValueError("tool_root_name must name one relative directory")
        return validated

    @model_validator(mode="after")
    def component_and_tool_ownership_is_unique(self) -> Self:
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("duplicate component_id")
        tool_ids = [
            executable.tool_id
            for component in self.components
            for executable in component.executables
        ]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("tool_id is owned by more than one component")
        return self


class PlatformArtifact(StrictContract):
    """Content-addressed platform input with portable and resolved identities."""

    artifact_id: EntityId
    kind: Literal[
        "LIBERTY",
        "TECH_LEF",
        "CELL_LEF",
        "GDS",
        "RC_RULES",
        "FLOW_CONFIG",
        "SCRIPT",
        "MAPPING",
        "LICENSE",
    ]
    logical_path: str
    resolved_path: Path
    sha256: HashRef
    size_bytes: int = Field(ge=0)

    @field_validator("logical_path")
    @classmethod
    def logical_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("resolved_path")
    @classmethod
    def resolved_path_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("resolved_path must be absolute")
        try:
            resolved = value.resolve()
        except (OSError, RuntimeError) as error:
            raise ValueError(f"resolved_path cannot be resolved: {error}") from error
        if value != resolved:
            raise ValueError("resolved_path must be normalized and symlink-resolved")
        return value


class TimingCorner(StrictContract):
    """One immutable ASAP7 timing-corner identity."""

    corner_id: EntityId
    role: Literal["SETUP", "HOLD", "REFERENCE"]
    library_model: Literal["NLDM", "CCS"]
    liberty_files: tuple[PlatformArtifact, ...] = Field(min_length=1)
    voltage_v: float = Field(gt=0, allow_inf_nan=False)
    temperature_c: float = Field(allow_inf_nan=False)
    native_time_unit: Literal["PS", "NS"]

    @model_validator(mode="after")
    def contains_only_unique_liberty_artifacts(self) -> Self:
        if any(item.kind != "LIBERTY" for item in self.liberty_files):
            raise ValueError("timing corner may contain only LIBERTY artifacts")
        artifact_ids = [item.artifact_id for item in self.liberty_files]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate Liberty artifact_id")
        return self


class ToolFingerprint(StrictContract):
    """Identity of the executable inspected by the doctor."""

    schema_version: Literal[1] = 1
    tool_id: EntityId
    executable: str = Field(min_length=1)
    version: str = Field(min_length=1)
    version_args: tuple[str, ...] = Field(min_length=1)
    executable_sha256: HashRef
    build_hash: HashRef
    adapter_version: Literal["bootstrap-doctor-v1"]
    container_digest: HashRef | None = None

    @field_validator("executable")
    @classmethod
    def executable_is_absolute(cls, value: str) -> str:
        executable = Path(value)
        if not executable.is_absolute():
            raise ValueError("executable must be an absolute path")
        try:
            resolved = executable.resolve()
        except (OSError, RuntimeError) as error:
            raise ValueError(f"executable cannot be resolved: {error}") from error
        if executable != resolved:
            raise ValueError("executable must be normalized and symlink-resolved")
        return value

    @model_validator(mode="after")
    def probe_recipe_is_registered(self) -> Self:
        if self.version_args != _expected_version_args(self.tool_id):
            raise ValueError("version_args do not match the registered probe recipe")
        return self


class DoctorIssue(StrictContract):
    """Stable machine-readable reason for one failed doctor check."""

    code: DoctorIssueCode
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)


class DoctorCheck(StrictContract):
    """One executable or platform-file readiness check."""

    name: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    resolved_path: str | None
    tool_fingerprint: ToolFingerprint | None
    artifact_hash: HashRef | None
    issues: tuple[DoctorIssue, ...]
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def passing_check_has_one_identity(self) -> Self:
        identities = sum(
            identity is not None for identity in (self.tool_fingerprint, self.artifact_hash)
        )
        if self.status == "PASS" and identities != 1:
            raise ValueError("a passing doctor check requires exactly one identity")
        if self.status == "PASS" and self.issues:
            raise ValueError("a passing doctor check cannot contain issues")
        if self.status == "FAIL" and not self.issues:
            raise ValueError("a failing doctor check requires at least one issue")
        return self


class DoctorReport(StrictContract):
    """Complete tool and optional platform-lock readiness report."""

    schema_version: Literal[1] = 1
    status: Literal["PASS", "FAIL"]
    checks: tuple[DoctorCheck, ...] = Field(min_length=1)
    platform_lock_hash: HashRef | None
    generated_at: AwareDatetime
    exit_code: Literal[0, 2]

    @model_validator(mode="after")
    def status_matches_checks_and_exit_code(self) -> Self:
        expected_status = "PASS" if all(check.status == "PASS" for check in self.checks) else "FAIL"
        expected_exit_code = 0 if expected_status == "PASS" else 2
        if self.status != expected_status or self.exit_code != expected_exit_code:
            raise ValueError("doctor status and exit code must match all checks")
        return self


class PlatformLock(StrictContract):
    """Pinned tool and ASAP7 platform realization consumed by later stages."""

    schema_version: Literal[1] = 1
    platform_id: EntityId
    source_manifest_hash: HashRef
    orfs_commit: GitCommit
    host: HostPlatform
    tool_fingerprints: tuple[ToolFingerprint, ...] = Field(min_length=1)
    setup_corner: TimingCorner
    hold_corner: TimingCorner
    reference_corner: TimingCorner | None
    tech_lef: PlatformArtifact
    cell_lefs: tuple[PlatformArtifact, ...] = Field(min_length=1)
    rc_rules: PlatformArtifact
    flow_config: PlatformArtifact
    license_artifacts: tuple[PlatformArtifact, ...] = Field(min_length=1)
    content_identity_hash: HashRef
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def platform_contract_is_coherent(self) -> Self:
        if not re.fullmatch(r"[0-9a-f]{40}", self.orfs_commit):
            raise ValueError("orfs_commit must be a full 40-character Git commit")
        if self.setup_corner.role != "SETUP":
            raise ValueError("setup_corner must have role SETUP")
        if self.hold_corner.role != "HOLD":
            raise ValueError("hold_corner must have role HOLD")
        if self.reference_corner is not None and self.reference_corner.role != "REFERENCE":
            raise ValueError("reference_corner must have role REFERENCE")
        setup_hashes = {item.sha256 for item in self.setup_corner.liberty_files}
        hold_hashes = {item.sha256 for item in self.hold_corner.liberty_files}
        if setup_hashes & hold_hashes:
            raise ValueError("setup and hold corners require distinct Liberty content")
        tool_ids = [item.tool_id for item in self.tool_fingerprints]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("duplicate tool_id in platform lock")
        if self.tech_lef.kind != "TECH_LEF":
            raise ValueError("tech_lef must have kind TECH_LEF")
        if any(item.kind != "CELL_LEF" for item in self.cell_lefs):
            raise ValueError("cell_lefs may contain only CELL_LEF artifacts")
        if self.rc_rules.kind != "RC_RULES":
            raise ValueError("rc_rules must have kind RC_RULES")
        if self.flow_config.kind != "FLOW_CONFIG":
            raise ValueError("flow_config must have kind FLOW_CONFIG")
        if any(item.kind != "LICENSE" for item in self.license_artifacts):
            raise ValueError("license_artifacts may contain only LICENSE artifacts")
        all_artifacts = (
            *self.setup_corner.liberty_files,
            *self.hold_corner.liberty_files,
            *((self.reference_corner.liberty_files) if self.reference_corner else ()),
            self.tech_lef,
            *self.cell_lefs,
            self.rc_rules,
            self.flow_config,
            *self.license_artifacts,
        )
        artifact_ids = [item.artifact_id for item in all_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate platform artifact_id")
        return self
