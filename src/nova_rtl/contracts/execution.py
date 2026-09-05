"""Typed deterministic tool-execution boundaries and stage results."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    Diagnostic,
    EntityId,
    HashRef,
    MetricSet,
    NonNegativeInt,
    StageInputHashes,
    StrictContract,
    UtcDatetime,
    canonical_sha256,
)
from nova_rtl.contracts.platform import ToolFingerprint

Stage = Literal[
    "RTL_PARSE",
    "YOSYS_SYNTH",
    "CONSTRAINT_BINDING",
    "CLOCK_INVENTORY",
    "CDC_INVARIANT",
    "FORMAL_MODEL_PREFLIGHT",
    "EQY_SMOKE",
    "FAST_SYNTH",
    "OPENSTA_FULL",
    "OPENROAD_PHYSICAL",
    "OPENROAD_PLACED_CTS",
    "OPENROAD_ROUTED",
    "FORMAL_EQUIVALENCE",
    "SIMULATION",
    "POWER_ANALYSIS",
    "EVIDENCE_GRAPH",
    "OPPORTUNITY_FORMATION",
]
StageStatus = Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]
ArtifactNamespace = Annotated[
    str,
    StringConstraints(pattern=r"^artifact://[a-zA-Z0-9._/-]+/$"),
]
EnvironmentName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]{0,63}$"),
]

VIEW_STAGES = frozenset(
    {
        "OPENSTA_FULL",
        "OPENROAD_PHYSICAL",
        "OPENROAD_PLACED_CTS",
        "OPENROAD_ROUTED",
        "POWER_ANALYSIS",
    }
)
FORMAL_STAGES = frozenset({"FORMAL_MODEL_PREFLIGHT", "EQY_SMOKE", "FORMAL_EQUIVALENCE"})
TIMING_STAGES = frozenset(
    {"OPENSTA_FULL", "OPENROAD_PHYSICAL", "OPENROAD_PLACED_CTS", "OPENROAD_ROUTED"}
)
M3_STAGE_EXTENSION_IDENTITIES = {
    "EVIDENCE_GRAPH": frozenset(
        {
            "analysis_view_set",
            "cdc_inventory",
            "clock_inventory",
            "critical_path_records",
            "protection_policy",
            "synthesis_structure",
        }
    ),
    "OPPORTUNITY_FORMATION": frozenset(
        {"evidence_graph", "protection_policy", "transform_registry"}
    ),
}
M3_EXTENSION_IDENTITIES = frozenset().union(*M3_STAGE_EXTENSION_IDENTITIES.values())
SECRET_ENVIRONMENT_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")


class ResourceLimits(StrictContract):
    cpu_cores: int = Field(strict=True, gt=0)
    memory_bytes: int = Field(strict=True, gt=0)
    wall_time_ms: int = Field(strict=True, gt=0)
    max_output_bytes: int = Field(strict=True, gt=0)


class ResourceUsage(StrictContract):
    cpu_time_ms: NonNegativeInt
    wall_time_ms: NonNegativeInt
    peak_rss_bytes: NonNegativeInt


def _validate_stage_identity(
    *,
    stage: str,
    analysis_view_id: str | None,
    input_hashes: StageInputHashes,
    design_contract_hash: str | None = None,
) -> None:
    if stage in VIEW_STAGES:
        if analysis_view_id is None:
            raise ValueError("view-specific stage requires analysis_view_id")
    elif analysis_view_id is not None:
        raise ValueError("view-independent stage cannot declare analysis_view_id")

    required = {"rtl_snapshot", "design_contract", "platform_lock", "tool_recipe"}
    if stage in VIEW_STAGES:
        required.update({"constraints", "constraint_binding", "analysis_view"})
    if stage == "POWER_ANALYSIS":
        required.add("power_activity")
    if stage in FORMAL_STAGES:
        required.add("formal_model")
    if stage in M3_STAGE_EXTENSION_IDENTITIES:
        required.update({"constraints", "constraint_binding"})
    if stage == "OPPORTUNITY_FORMATION":
        required.add("parent_stage_result")
    missing = sorted(name for name in required if getattr(input_hashes, name) is None)
    if missing:
        raise ValueError(f"stage input identity is missing: {', '.join(missing)}")

    extension_names = set(input_hashes.extensions)
    if stage in M3_STAGE_EXTENSION_IDENTITIES:
        expected_extensions = M3_STAGE_EXTENSION_IDENTITIES[stage]
        missing_extensions = sorted(expected_extensions - extension_names)
        unexpected_extensions = sorted(extension_names - expected_extensions)
        if missing_extensions:
            raise ValueError(
                "stage input extension identity is missing: "
                f"{', '.join(missing_extensions)}"
            )
        if unexpected_extensions:
            raise ValueError(
                f"stage input extension identity is not valid for {stage}: "
                f"{', '.join(unexpected_extensions)}"
            )
    else:
        invalid_extensions = sorted(extension_names & M3_EXTENSION_IDENTITIES)
        if invalid_extensions:
            raise ValueError(
                f"stage input extension identity is not valid for {stage}: "
                f"{', '.join(invalid_extensions)}"
            )
    if design_contract_hash is not None and input_hashes.design_contract != design_contract_hash:
        raise ValueError("design_contract_hash must match input_hashes.design_contract")


def _unique_artifact_ids(artifacts: tuple[ArtifactRef, ...], label: str) -> None:
    artifact_ids = tuple(item.artifact_id for item in artifacts)
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError(f"{label} artifact IDs must be unique")


class ToolJob(StrictContract):
    """Persisted request for one deterministic tool execution."""

    schema_version: Literal[1] = 1
    tool_job_id: EntityId
    stage_result_id: EntityId
    run_id: EntityId
    candidate_id: EntityId
    stage: Stage
    analysis_view_id: EntityId | None
    design_contract_hash: HashRef
    input_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    input_hashes: StageInputHashes
    resource_limits: ResourceLimits
    deadline: UtcDatetime
    artifact_namespace: ArtifactNamespace
    requested_at: UtcDatetime

    @model_validator(mode="after")
    def execution_request_is_coherent(self) -> Self:
        if self.deadline <= self.requested_at:
            raise ValueError("deadline must be later than requested_at")
        _unique_artifact_ids(self.input_artifact_refs, "input")
        _validate_stage_identity(
            stage=self.stage,
            analysis_view_id=self.analysis_view_id,
            input_hashes=self.input_hashes,
            design_contract_hash=self.design_contract_hash,
        )
        return self


class PreparedCommand(StrictContract):
    """Shell-free, isolated and self-hashed command prepared by a tool adapter."""

    schema_version: Literal[1] = 1
    tool_job_id: EntityId
    stage_result_id: EntityId
    tool_fingerprint: ToolFingerprint
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: Path
    environment: dict[EnvironmentName, str]
    resource_limits: ResourceLimits
    deadline: UtcDatetime
    staged_input_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    recipe_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    preparation_hash: HashRef

    @field_validator("argv")
    @classmethod
    def argv_entries_are_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be nonempty and NUL-free")
        return value

    @field_validator("working_directory")
    @classmethod
    def working_directory_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or value != value.resolve():
            raise ValueError("working_directory must be absolute, normalized and isolated")
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_sanitized(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if any(marker in name for marker in SECRET_ENVIRONMENT_MARKERS):
                raise ValueError(f"secret-bearing environment variable is forbidden: {name}")
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError(f"environment value contains control characters: {name}")
        return value

    @model_validator(mode="after")
    def prepared_identity_is_coherent(self) -> Self:
        if self.argv[0] != self.tool_fingerprint.executable:
            raise ValueError("argv[0] must equal the fingerprinted executable")
        _unique_artifact_ids(self.staged_input_artifact_refs, "staged input")
        _unique_artifact_ids(self.recipe_artifact_refs, "recipe")
        if any(
            item.producer_stage_result_id != self.stage_result_id
            for item in self.recipe_artifact_refs
        ):
            raise ValueError("recipe artifacts must name the preallocated stage result producer")
        expected_hash = canonical_sha256(self, exclude=frozenset({"preparation_hash"}))
        if self.preparation_hash != expected_hash:
            raise ValueError("preparation_hash does not match the canonical command identity")
        return self


class RawToolResult(StrictContract):
    """Process outcome and raw evidence, intentionally without analysis status."""

    schema_version: Literal[1] = 1
    tool_job_id: EntityId
    stage_result_id: EntityId
    tool_fingerprint: ToolFingerprint
    prepared_command_artifact: ArtifactRef
    prepared_command_hash: HashRef
    exit_code: int = Field(strict=True)
    signal: int | None = Field(default=None, strict=True, ge=1)
    timed_out: bool
    started_at: UtcDatetime
    ended_at: UtcDatetime
    resource_usage: ResourceUsage
    raw_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def execution_evidence_is_coherent(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot be earlier than started_at")
        if self.prepared_command_hash != self.prepared_command_artifact.sha256:
            raise ValueError("prepared_command_hash must match its artifact")
        if self.prepared_command_artifact.producer_stage_result_id != self.stage_result_id:
            raise ValueError("prepared command artifact has the wrong producer")
        _unique_artifact_ids(self.raw_artifacts, "raw")
        if any(
            item.producer_stage_result_id != self.stage_result_id for item in self.raw_artifacts
        ):
            raise ValueError("raw artifact producer must match stage_result_id")
        artifact_labels = tuple(
            f"{item.artifact_id} {item.uri}".lower() for item in self.raw_artifacts
        )
        has_stdout = any("stdout" in label for label in artifact_labels)
        has_stderr = any("stderr" in label for label in artifact_labels)
        has_invocation = any(
            marker in label
            for label in artifact_labels
            for marker in ("argv", "recipe", "invocation")
        )
        if not (has_stdout and has_stderr and has_invocation):
            raise ValueError("raw artifacts must preserve stdout, stderr and invocation evidence")
        return self


class StageResult(StrictContract):
    """Canonical validated outcome of a deterministic gate or tool stage."""

    schema_version: Literal[2] = 2
    stage_result_id: EntityId
    run_id: EntityId
    candidate_id: EntityId
    stage: Stage
    analysis_view_id: EntityId | None
    status: StageStatus
    tool_fingerprint: ToolFingerprint
    input_hashes: StageInputHashes
    metrics: MetricSet
    diagnostics: tuple[Diagnostic, ...]
    raw_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    started_at: UtcDatetime
    ended_at: UtcDatetime

    @model_validator(mode="after")
    def validated_result_is_coherent(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot be earlier than started_at")
        _validate_stage_identity(
            stage=self.stage,
            analysis_view_id=self.analysis_view_id,
            input_hashes=self.input_hashes,
        )
        if self.metrics.analysis_view_id != self.analysis_view_id:
            raise ValueError("metrics analysis_view_id must match StageResult analysis_view_id")
        if self.status != "PASS" and not self.diagnostics:
            raise ValueError("a non-pass StageResult requires at least one diagnostic")
        _unique_artifact_ids(self.raw_artifacts, "raw")
        if any(
            item.producer_stage_result_id != self.stage_result_id for item in self.raw_artifacts
        ):
            raise ValueError("raw artifact producer must match StageResult identity")

        if self.status == "PASS" and self.stage in TIMING_STAGES:
            setup_complete = (
                self.metrics.setup_wns_ns is not None and self.metrics.setup_tns_ns is not None
            )
            hold_complete = (
                self.metrics.hold_wns_ns is not None and self.metrics.hold_tns_ns is not None
            )
            if not setup_complete and not hold_complete:
                raise ValueError("completed timing stage requires setup or hold WNS/TNS metrics")
        if self.status == "PASS" and self.stage in {
            "YOSYS_SYNTH",
            "FAST_SYNTH",
        } and (self.metrics.cell_count is None or self.metrics.mapped_area_um2 is None):
            raise ValueError("completed synthesis stage requires cell count and mapped area")
        if (
            self.status == "PASS"
            and self.stage == "POWER_ANALYSIS"
            and (self.metrics.power_total_uw is None or self.input_hashes.power_activity is None)
        ):
            raise ValueError(
                "completed power stage requires power metric and activity identity"
            )
        return self


__all__ = [
    "PreparedCommand",
    "RawToolResult",
    "ResourceLimits",
    "ResourceUsage",
    "Stage",
    "StageResult",
    "StageStatus",
    "ToolJob",
]
