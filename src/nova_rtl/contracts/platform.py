"""Platform-readiness contracts used by the bootstrap doctor."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit

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
    "ORFS_TREE_IDENTITY_MISMATCH",
    "ORFS_TREE_IO_ERROR",
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


class ArchiveMetadata(StrictContract):
    """Exact archive identity and layout required for safe acquisition."""

    byte_size: int = Field(gt=0)
    archive_format: Literal["TAR_GZ", "TAR_XZ", "DEB"]
    strip_components: int = Field(ge=0, le=32)
    max_decompressed_bytes: int = Field(gt=0)
    max_regular_file_bytes: int = Field(gt=0)
    max_entries: int = Field(gt=0)


class RuntimeEnvironmentEntry(StrictContract):
    """A rooted runtime value contributed by one portable component."""

    name: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    operation: Literal["PREPEND_PATH", "SET", "SET_LITERAL"] = "PREPEND_PATH"
    relative_paths: tuple[str, ...] = ()
    literal_value: str | None = None

    @model_validator(mode="before")
    @classmethod
    def path_operations_require_relative_paths_in_input(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and value.get("operation", "PREPEND_PATH") != "SET_LITERAL"
            and "relative_paths" not in value
        ):
            raise ValueError("path-based runtime environment entry requires relative_paths")
        return value

    @field_validator("relative_paths")
    @classmethod
    def runtime_paths_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("runtime paths must be unique within an entry")
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def operation_value_is_unambiguous(self) -> Self:
        if self.literal_value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in self.literal_value
        ):
            raise ValueError("literal_value must not contain control characters")
        if self.operation == "SET_LITERAL":
            if self.relative_paths:
                raise ValueError(
                    "SET_LITERAL runtime environment entry cannot declare relative_paths"
                )
            if not self.literal_value:
                raise ValueError(
                    "SET_LITERAL runtime environment entry requires a nonempty literal_value"
                )
            return self
        if not self.relative_paths:
            raise ValueError(
                "runtime environment entry relative_paths must contain at least 1 item"
            )
        if self.literal_value is not None:
            raise ValueError("path-based runtime environment entry cannot declare literal_value")
        if self.operation == "SET" and len(self.relative_paths) != 1:
            raise ValueError("SET runtime environment entry requires exactly one path")
        return self


def _validate_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("source_url must be an absolute HTTPS URL without credentials or fragment")
    return value


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
    archive: ArchiveMetadata | None = None
    runtime_environment: tuple[RuntimeEnvironmentEntry, ...] = ()
    allowed_redirect_hosts: tuple[str, ...]

    @field_validator("source_url")
    @classmethod
    def source_url_is_safe_https(cls, value: str) -> str:
        return _validate_https_url(value)

    @model_validator(mode="after")
    def source_is_immutable(self) -> Self:
        normalized_url = self.source_url.lower().rstrip("/")
        if "/latest/" in normalized_url or normalized_url.endswith("/latest"):
            raise ValueError("moving latest URL is not permitted")
        if self.source_kind == "ARCHIVE":
            if self.archive_sha256 is None or self.git_commit is not None or self.archive is None:
                raise ValueError(
                    "ARCHIVE source requires archive_sha256, archive metadata, and no git_commit"
                )
        elif (
            self.archive_sha256 is not None
            or self.archive is not None
            or not re.fullmatch(r"[0-9a-f]{40}", self.git_commit or "")
        ):
            raise ValueError(
                "GIT source requires a full 40-character Git commit and no archive metadata"
            )
        tool_ids = [item.tool_id for item in self.executables]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("duplicate tool_id within component")
        runtime_names = [item.name for item in self.runtime_environment]
        if len(runtime_names) != len(set(runtime_names)):
            raise ValueError("duplicate runtime environment variable within component")
        if len(self.allowed_redirect_hosts) != len(set(self.allowed_redirect_hosts)):
            raise ValueError("duplicate redirect host within component")
        for host in self.allowed_redirect_hosts:
            if host != host.lower() or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                host,
            ):
                raise ValueError("redirect host must be a normalized DNS hostname")
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
        if validated != ".nova-tools":
            raise ValueError("tool_root_name must be exactly .nova-tools")
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
        operations: dict[str, str] = {}
        set_contributions: set[str] = set()
        for component in self.components:
            for entry in component.runtime_environment:
                prior = operations.setdefault(entry.name, entry.operation)
                if prior != entry.operation:
                    raise ValueError(
                        f"runtime variable {entry.name} mixes SET and PREPEND_PATH operations"
                    )
                if entry.operation in {"SET", "SET_LITERAL"}:
                    if entry.name in set_contributions:
                        raise ValueError(
                            f"runtime variable {entry.name} has more than one SET contribution"
                        )
                    set_contributions.add(entry.name)
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
    operating_condition_mode: Literal["PER_LIBRARY_NOMINAL"]
    operating_conditions: tuple[str, ...] = Field(min_length=1)
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
        if len(self.operating_conditions) != len(self.liberty_files):
            raise ValueError("each Liberty artifact requires one operating condition")
        if any(not item.strip() for item in self.operating_conditions):
            raise ValueError("operating conditions must be nonempty")
        return self


class TimingCornerSelection(StrictContract):
    """Reviewed relative Liberty selection for one required timing role."""

    corner_id: EntityId
    role: Literal["SETUP", "HOLD", "REFERENCE"]
    liberty_files: tuple[str, ...] = Field(min_length=1)

    @field_validator("liberty_files")
    @classmethod
    def liberty_paths_are_safe_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Liberty paths must be unique within a corner")
        return tuple(_validate_relative_path(item) for item in value)


class PlatformSelectionPolicy(StrictContract):
    """Human-reviewed ASAP7 file selection independent of a local checkout path."""

    schema_version: Literal[1] = 1
    platform_id: Literal["asap7"]
    orfs_component_id: Literal["orfs"]
    orfs_commit: GitCommit
    platform_root: str
    library_model: Literal["NLDM"]
    setup_corner: TimingCornerSelection
    hold_corner: TimingCornerSelection
    reference_corner: TimingCornerSelection | None
    tech_lef: str
    cell_lefs: tuple[str, ...] = Field(min_length=1)
    rc_config: str
    flow_config: str
    license_artifacts: tuple[str, ...] = Field(min_length=1)
    redistribution_status: Literal["PERMITTED", "REVIEW_REQUIRED", "RESTRICTED"]
    license_notes: tuple[str, ...] = Field(min_length=1)
    deterministic_seed: int = Field(ge=0, le=2**31 - 1)

    @field_validator(
        "platform_root",
        "tech_lef",
        "rc_config",
        "flow_config",
    )
    @classmethod
    def selected_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("cell_lefs", "license_artifacts")
    @classmethod
    def selected_paths_are_safe_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("selected paths must be unique")
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def selection_is_coherent(self) -> Self:
        if not re.fullmatch(r"[0-9a-f]{40}", self.orfs_commit):
            raise ValueError("orfs_commit must be a full 40-character Git commit")
        if self.setup_corner.role != "SETUP":
            raise ValueError("setup_corner selection must have role SETUP")
        if self.hold_corner.role != "HOLD":
            raise ValueError("hold_corner selection must have role HOLD")
        if self.reference_corner is not None and self.reference_corner.role != "REFERENCE":
            raise ValueError("reference_corner selection must have role REFERENCE")
        corner_ids = (
            self.setup_corner.corner_id,
            self.hold_corner.corner_id,
            *((self.reference_corner.corner_id,) if self.reference_corner else ()),
        )
        if len(corner_ids) != len(set(corner_ids)):
            raise ValueError("corner IDs must be distinct")
        platform_prefix = PurePosixPath(self.platform_root)
        platform_paths = (
            *self.setup_corner.liberty_files,
            *self.hold_corner.liberty_files,
            *((self.reference_corner.liberty_files) if self.reference_corner else ()),
            self.tech_lef,
            *self.cell_lefs,
            self.rc_config,
            self.flow_config,
        )
        if any(platform_prefix not in PurePosixPath(item).parents for item in platform_paths):
            raise ValueError("platform artifact path must be below platform_root")
        if len(platform_paths) != len(set(platform_paths)):
            raise ValueError("platform artifact paths must be unique")
        if any(not note.strip() for note in self.license_notes):
            raise ValueError("license notes must be nonempty")
        return self


class PlatformLockRequest(StrictContract):
    """One verified local realization requested from a reviewed selection policy."""

    orfs_root: Path
    policy: PlatformSelectionPolicy
    source_manifest_hash: HashRef
    verified_orfs_tree_identity: HashRef
    host: HostPlatform
    tool_fingerprints: tuple[ToolFingerprint, ...] = Field(min_length=1)
    generated_at: AwareDatetime

    @field_validator("orfs_root")
    @classmethod
    def orfs_root_is_normalized_absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or value != value.resolve():
            raise ValueError("orfs_root must be absolute, normalized, and symlink-resolved")
        return value


class PlatformAnalysisView(StrictContract):
    """Platform-only portion of one required M0 timing-analysis view."""

    analysis_view_id: EntityId
    check: Literal["SETUP", "HOLD"]
    required: Literal[True] = True
    liberty_corner_id: EntityId
    liberty_artifact_hashes: tuple[HashRef, ...] = Field(min_length=1)
    rc_corner_id: EntityId
    rc_artifact_hash: HashRef
    operating_condition_mode: Literal["PER_LIBRARY_NOMINAL"]
    operating_conditions: tuple[str, ...] = Field(min_length=1)
    required_stages: tuple[Literal["OPENSTA_FULL", "OPENROAD_PHYSICAL"], ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def parallel_fields_are_coherent(self) -> Self:
        if len(self.liberty_artifact_hashes) != len(self.operating_conditions):
            raise ValueError("each Liberty hash requires one operating condition")
        if len(self.required_stages) != len(set(self.required_stages)):
            raise ValueError("required stages must be unique")
        return self


class PlatformAnalysisViews(StrictContract):
    """Two required platform views bound to exact platform-lock bytes."""

    schema_version: Literal[1] = 1
    platform_id: EntityId
    platform_lock_hash: HashRef
    views: tuple[PlatformAnalysisView, PlatformAnalysisView]

    @model_validator(mode="after")
    def contains_one_setup_and_one_hold_view(self) -> Self:
        if tuple(view.check for view in self.views) != ("SETUP", "HOLD"):
            raise ValueError("platform views must contain ordered SETUP and HOLD checks")
        view_ids = tuple(view.analysis_view_id for view in self.views)
        corner_ids = tuple(view.liberty_corner_id for view in self.views)
        if len(set(view_ids)) != 2 or len(set(corner_ids)) != 2:
            raise ValueError("view and Liberty corner IDs must be distinct")
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


class ComponentInventoryEntry(StrictContract):
    """One strict, content-addressed entry in a hydrated component tree."""

    path: str
    type: Literal["directory", "file", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    sha256: HashRef | None = None
    target: str | None = None

    @field_validator("path")
    @classmethod
    def inventory_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @model_validator(mode="after")
    def fields_match_entry_type(self) -> Self:
        if self.type == "file" and (self.sha256 is None or self.target is not None):
            raise ValueError("file inventory entry requires only sha256")
        if self.type == "directory" and (self.sha256 is not None or self.target is not None):
            raise ValueError("directory inventory entry cannot contain sha256 or target")
        if self.type == "symlink":
            if self.sha256 is not None or not self.target:
                raise ValueError("symlink inventory entry requires only target")
            target = PurePosixPath(self.target)
            contains_control = any(
                ord(character) < 32 or ord(character) == 127 for character in self.target
            )
            if "\\" in self.target or contains_control or target.is_absolute():
                raise ValueError("symlink inventory target must be a safe relative path")
        return self


class HydrationComponentReceipt(StrictContract):
    """Versioned source-to-tree provenance for one hydrated component."""

    schema_version: Literal[2] = 2
    component_id: EntityId
    manifest_hash: HashRef
    source: ToolSource
    inventory: tuple[ComponentInventoryEntry, ...]
    tree_identity: HashRef

    @model_validator(mode="after")
    def source_owns_receipt_component(self) -> Self:
        if self.source.component_id != self.component_id:
            raise ValueError("component receipt source does not match component_id")
        return self


class InstalledComponentReceipt(StrictContract):
    """Strict installed identity for one reviewed toolchain source."""

    component_id: EntityId
    source: ToolSource
    inventory: tuple[ComponentInventoryEntry, ...]
    tree_identity: HashRef


class CanonicalEnvironmentEntry(StrictContract):
    """One deterministic, rooted activation variable in a toolchain receipt."""

    operation: Literal["PREPEND_PATH", "SET", "SET_LITERAL"]
    paths: tuple[str, ...] = ()
    literal_value: str | None = None

    @field_validator("paths")
    @classmethod
    def paths_are_absolute_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("canonical environment paths must be unique")
        for item in value:
            path = Path(item)
            if not path.is_absolute() or path != path.absolute():
                raise ValueError("canonical environment path must be absolute and normalized")
        return value

    @model_validator(mode="after")
    def operation_value_is_unambiguous(self) -> Self:
        if self.literal_value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in self.literal_value
        ):
            raise ValueError("literal_value must not contain control characters")
        if self.operation == "SET_LITERAL":
            if self.paths:
                raise ValueError("SET_LITERAL canonical environment entry cannot declare paths")
            if not self.literal_value:
                raise ValueError(
                    "SET_LITERAL canonical environment entry requires a nonempty literal_value"
                )
            return self
        if not self.paths:
            raise ValueError("path-based canonical environment entry requires at least 1 path")
        if self.literal_value is not None:
            raise ValueError("path-based canonical environment entry cannot declare literal_value")
        if self.operation == "SET" and len(self.paths) != 1:
            raise ValueError("SET canonical environment entry requires exactly one path")
        return self


class ToolchainReceipt(StrictContract):
    """Versioned global identity of one verified portable toolchain install."""

    schema_version: Literal[1]
    manifest_hash: HashRef
    manifest: ToolchainSourceManifest
    components: tuple[InstalledComponentReceipt, ...] = Field(min_length=1)
    environment: dict[str, CanonicalEnvironmentEntry]
    tool_fingerprints: tuple[ToolFingerprint, ...]

    @field_validator("environment")
    @classmethod
    def environment_names_are_safe(
        cls, value: dict[str, CanonicalEnvironmentEntry]
    ) -> dict[str, CanonicalEnvironmentEntry]:
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) for name in value):
            raise ValueError("canonical environment variable name is invalid")
        return value

    @model_validator(mode="after")
    def identities_are_unique_and_manifest_owned(self) -> Self:
        component_ids = tuple(item.component_id for item in self.components)
        expected_components = tuple(item.component_id for item in self.manifest.components)
        if component_ids != expected_components:
            raise ValueError("receipt component ordering does not match manifest")
        for installed, source in zip(self.components, self.manifest.components, strict=True):
            if installed.source != source:
                raise ValueError("receipt component source does not match manifest")
        tool_ids = tuple(item.tool_id for item in self.tool_fingerprints)
        expected_tools = tuple(
            executable.tool_id
            for component in self.manifest.components
            for executable in component.executables
        )
        if tool_ids != expected_tools:
            raise ValueError("receipt fingerprint ordering does not match manifest")
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
    selection_policy_hash: HashRef
    orfs_commit: GitCommit
    orfs_tree_identity: HashRef
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
    redistribution_status: Literal["PERMITTED", "REVIEW_REQUIRED", "RESTRICTED"]
    license_notes: tuple[str, ...] = Field(min_length=1)
    deterministic_seed: int = Field(ge=0, le=2**31 - 1)
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
        corner_ids = (
            self.setup_corner.corner_id,
            self.hold_corner.corner_id,
            *((self.reference_corner.corner_id,) if self.reference_corner else ()),
        )
        if len(corner_ids) != len(set(corner_ids)):
            raise ValueError("corner IDs must be distinct")
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
        if any(not note.strip() for note in self.license_notes):
            raise ValueError("license notes must be nonempty")
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
        resolved_paths = [item.resolved_path for item in all_artifacts]
        if len(resolved_paths) != len(set(resolved_paths)):
            raise ValueError("platform artifacts must resolve to distinct files")
        return self


class GeneratedClockInterpretation(StrictContract):
    """Versioned interpretation of the organizer's generated-clock requirement."""

    effective_value: Literal["21_PER_MASTER"]
    masters: int = Field(gt=0)
    total_generated_clocks: int = Field(gt=0)
    source: Literal["CONSERVATIVE_INTERNAL_DEFAULT", "ORGANIZER_RESPONSE"]

    @model_validator(mode="after")
    def total_matches_effective_value(self) -> Self:
        if self.masters * 21 != self.total_generated_clocks:
            raise ValueError("total_generated_clocks must equal masters multiplied by 21")
        return self


class PsmInterpretation(StrictContract):
    """Versioned interpretation of the challenge's PSM terminology."""

    effective_value: Literal["FSM_STATE_MACHINE_OPTIMIZATION"]
    source: Literal["CONSERVATIVE_INTERNAL_DEFAULT", "ORGANIZER_RESPONSE"]


class OrganizerResponse(StrictContract):
    """Optional immutable reference to a later organizer clarification."""

    status: Literal["NOT_RECEIVED", "RECEIVED"]
    evidence_artifact_id: EntityId | None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> Self:
        if (self.status == "RECEIVED") != (self.evidence_artifact_id is not None):
            raise ValueError("organizer response evidence must match response status")
        return self


class OrganizerDecisions(StrictContract):
    """Strict M0 record of conservative defaults and organizer clarifications."""

    schema_version: Literal[1] = 1
    generated_clock_interpretation: GeneratedClockInterpretation
    psm_interpretation: PsmInterpretation
    organizer_response: OrganizerResponse
    override_rule: Literal["CONFIG_ONLY_WITH_NEW_RUN_ID"]


class SignoffEvidenceFile(StrictContract):
    """One immutable file included in a milestone sign-off packet."""

    relative_path: str
    sha256: HashRef
    size_bytes: int = Field(gt=0)

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        validated = _validate_relative_path(value)
        if validated == ".":
            raise ValueError("evidence path must name a file")
        return validated


class SmokeTimingView(StrictContract):
    """One constrained register-to-register timing result from the smoke design."""

    check: Literal["SETUP", "HOLD"]
    corner_id: EntityId
    path_type: Literal["max", "min"]
    path_group: Literal["smoke_clock"]
    register_count: Literal[2]
    path_count: int = Field(ge=1)
    slack_ps: float = Field(ge=0, allow_inf_nan=False)
    report: SignoffEvidenceFile

    @model_validator(mode="after")
    def path_type_matches_check(self) -> Self:
        expected = "max" if self.check == "SETUP" else "min"
        if self.path_type != expected:
            raise ValueError(f"{self.check} smoke view requires {expected} path type")
        return self


class PlatformSmokeReport(StrictContract):
    """Durable evidence index for the locked two-view physical smoke flow."""

    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    platform_id: EntityId
    platform_lock_hash: HashRef
    toolchain_receipt_hash: HashRef
    source_manifest_hash: HashRef
    selection_policy_hash: HashRef
    make_executable: str = Field(min_length=1)
    make_executable_sha256: HashRef
    make_version: str = Field(min_length=1)
    rtl: SignoffEvidenceFile
    constraints: SignoffEvidenceFile
    smoke_config: SignoffEvidenceFile
    openroad_log: SignoffEvidenceFile
    mapped_netlist: SignoffEvidenceFile
    synthesis_database: SignoffEvidenceFile
    cts_database: SignoffEvidenceFile
    setup_view: SmokeTimingView
    hold_view: SmokeTimingView
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def views_and_artifacts_are_coherent(self) -> Self:
        if self.setup_view.check != "SETUP" or self.hold_view.check != "HOLD":
            raise ValueError("smoke report requires ordered setup and hold views")
        if self.setup_view.corner_id == self.hold_view.corner_id:
            raise ValueError("smoke setup and hold corners must be distinct")
        evidence = (
            self.rtl,
            self.constraints,
            self.smoke_config,
            self.openroad_log,
            self.mapped_netlist,
            self.synthesis_database,
            self.cts_database,
            self.setup_view.report,
            self.hold_view.report,
        )
        paths = [item.relative_path for item in evidence]
        if len(paths) != len(set(paths)):
            raise ValueError("smoke evidence paths must be unique")
        return self


class M0SignoffReport(StrictContract):
    """Complete machine-readable evidence index for the M0 exit gate."""

    schema_version: Literal[1] = 1
    milestone: Literal["M0"]
    status: Literal["PASS"]
    generated_at: AwareDatetime
    exit_code: Literal[0]
    doctor_report: SignoffEvidenceFile
    toolchain_receipt: SignoffEvidenceFile
    platform_lock: SignoffEvidenceFile
    selection_policy: SignoffEvidenceFile
    analysis_views: SignoffEvidenceFile
    organizer_decisions: SignoffEvidenceFile
    default_policy: SignoffEvidenceFile
    cdc_patterns: SignoffEvidenceFile
    reset_assumptions: SignoffEvidenceFile
    smoke_report: SignoffEvidenceFile

    @model_validator(mode="after")
    def evidence_paths_are_unique(self) -> Self:
        evidence = (
            self.doctor_report,
            self.toolchain_receipt,
            self.platform_lock,
            self.selection_policy,
            self.analysis_views,
            self.organizer_decisions,
            self.default_policy,
            self.cdc_patterns,
            self.reset_assumptions,
            self.smoke_report,
        )
        paths = [item.relative_path for item in evidence]
        if len(paths) != len(set(paths)):
            raise ValueError("sign-off evidence paths must be unique")
        return self
