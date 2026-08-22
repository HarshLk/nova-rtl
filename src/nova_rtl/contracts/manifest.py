"""Human-authored project manifest and its integrated policy contracts."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeFloat,
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
    canonical_sha256,
)

HdlIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*$"),
]
Define = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*(?:=.*)?$"),
]
StageName = Literal[
    "OPENSTA_FULL",
    "OPENROAD_PHYSICAL",
    "OPENROAD_PLACED_CTS",
    "OPENROAD_ROUTED",
]
CorrectnessContract = Literal["STRICT_SEQ_EQUIV", "RETIMING_EQUIV", "LATENCY_AWARE"]
JsonScalar = str | int | float | bool | None


def _normalized_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    has_control = any(ord(character) < 32 or ord(character) == 127 for character in value)
    if (
        not value
        or "\\" in value
        or has_control
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("path must be a normalized relative path under the project root")
    return value


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


def _define_name(value: str) -> str:
    return value.split("=", 1)[0]


class RtlManifest(StrictContract):
    files: tuple[str, ...] = Field(min_length=1)
    include_dirs: tuple[str, ...]
    defines: tuple[Define, ...]

    @field_validator("files", "include_dirs")
    @classmethod
    def paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized_relative_path(item) for item in value)
        return _unique(normalized, "RTL paths")

    @field_validator("defines")
    @classmethod
    def defines_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(_define_name(item) for item in value)
        _unique(names, "RTL define names")
        return value


class FunctionalCompilationProfile(StrictContract):
    defines: tuple[Define, ...]
    parameters: dict[HdlIdentifier, JsonScalar]

    @field_validator("defines")
    @classmethod
    def defines_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(tuple(_define_name(item) for item in value), "functional define names")
        return value

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for parameter, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"parameter {parameter} must be finite")
        return value


class FormalCompilationProfile(StrictContract):
    property_files: tuple[str, ...] = Field(min_length=1)
    harness_manifest: str
    property_only_define: HdlIdentifier
    disallow_design_behavior_defines: Literal[True]
    require_functional_logic_hash_match: Literal[True]

    @field_validator("property_files")
    @classmethod
    def property_paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(tuple(_normalized_relative_path(item) for item in value), "property paths")

    @field_validator("harness_manifest")
    @classmethod
    def harness_path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class CompilationProfiles(StrictContract):
    functional: FunctionalCompilationProfile
    formal: FormalCompilationProfile


class ConstraintPolicy(StrictContract):
    sdc: str
    immutable: bool
    expected_master_clocks: Literal[5]
    expected_generated_clocks_per_master: int = Field(strict=True, gt=0)
    require_zero_unconstrained_endpoints: Literal[True]
    require_constraint_binding_equivalence: Literal[True]
    require_exception_coverage_equivalence: Literal[True]

    @field_validator("sdc")
    @classmethod
    def sdc_path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)

    @field_validator("immutable")
    @classmethod
    def constraints_are_immutable(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("constraints must remain immutable")
        return value


class TechnologyPolicy(StrictContract):
    platform_id: EntityId
    platform_lock: str
    platform_lock_hash: HashRef

    @field_validator("platform_lock")
    @classmethod
    def platform_lock_path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class AnalysisViewManifest(StrictContract):
    id: EntityId
    mode: Literal["FUNCTIONAL"]
    check: Literal["SETUP", "HOLD", "POWER"]
    liberty_corner: EntityId
    rc_corner: EntityId
    operating_condition: EntityId
    derate_policy: str
    clock_uncertainty_policy: str
    hard_limits: dict[str, FiniteFloat]
    sdc: str
    required_stages: tuple[StageName, ...] = Field(min_length=1)
    required: bool

    @field_validator("derate_policy", "clock_uncertainty_policy", "sdc")
    @classmethod
    def policy_paths_are_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)

    @field_validator("required_stages")
    @classmethod
    def stages_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "required stages")

    @model_validator(mode="after")
    def hard_limit_matches_check(self) -> Self:
        required_limit = {
            "SETUP": "setup_wns_ns",
            "HOLD": "hold_wns_ns",
            "POWER": "power_total_uw",
        }[self.check]
        if required_limit not in self.hard_limits:
            raise ValueError(f"{self.check} view requires hard limit {required_limit}")
        return self


class PowerActivityManifest(StrictContract):
    format: Literal["VCD", "SAIF", "VECTORLESS"]
    source: str | None
    scope: NonEmptyString
    time_window_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    require_same_activity_hash: Literal[True]
    vectorless_fallback: Literal["ESTIMATED_ONLY_NOT_COMPARABLE"]

    @field_validator("source")
    @classmethod
    def source_path_is_normalized(cls, value: str | None) -> str | None:
        return None if value is None else _normalized_relative_path(value)

    @model_validator(mode="after")
    def source_and_window_match_format(self) -> Self:
        start, end = self.time_window_ns
        if end <= start:
            raise ValueError("power activity time window end must be greater than start")
        if self.format in {"VCD", "SAIF"} and self.source is None:
            raise ValueError(f"{self.format} power activity requires a source")
        if self.format == "VECTORLESS" and self.source is not None:
            raise ValueError("VECTORLESS power activity cannot declare a source")
        return self


class CdcPolicy(StrictContract):
    checker_mode: Literal["STRUCTURAL_INVARIANT_AUDIT"]
    approved_pattern_registry: str
    require_zero_unapproved_crossings: Literal[True]
    require_candidate_inventory_equivalence: Literal[True]
    protocol_property_manifest: str
    external_cdc_tool: EntityId | None

    @field_validator("approved_pattern_registry", "protocol_property_manifest")
    @classmethod
    def policy_paths_are_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class FormalPolicy(StrictContract):
    primary_competition_contract: Literal["STRICT_SEQ_EQUIV"]
    require_primary_latency_preserving_candidate: Literal[True]
    proof_scope_policy: Literal["WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED"]
    asynchronous_master_clocks: Literal[5]
    master_clock_model: Literal["INDEPENDENT_SHARED_GOLD_GATE_EVENTS"]
    generated_clock_model: Literal["DERIVED_FROM_PROTECTED_DIVIDER_STATE"]
    sby_multiclock: Literal[True]
    reset_assumption_manifest: str
    prohibit_assumption_only_equivalence: Literal[True]

    @field_validator("reset_assumption_manifest")
    @classmethod
    def reset_manifest_path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class ProtectionPolicy(StrictContract):
    modules: tuple[HdlIdentifier, ...] = Field(min_length=1)
    path_patterns: tuple[str, ...] = Field(min_length=1)

    @field_validator("modules")
    @classmethod
    def modules_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "protected modules")

    @field_validator("path_patterns")
    @classmethod
    def paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(tuple(_normalized_relative_path(item) for item in value), "protected paths")


class OptimizationPolicy(StrictContract):
    objective_policy: Literal["BALANCED_PPA"]
    max_area_growth_percent: NonNegativeFloat
    max_candidates: int = Field(strict=True, gt=0)
    openroad_finalists: int = Field(strict=True, gt=0)
    allowed_contracts: tuple[CorrectnessContract, ...] = Field(min_length=1)
    editable_path_patterns: tuple[str, ...]
    deterministic_seed: NonNegativeInt

    @field_validator("allowed_contracts")
    @classmethod
    def contracts_are_unique_and_include_primary(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        _unique(value, "allowed correctness contracts")
        if "STRICT_SEQ_EQUIV" not in value:
            raise ValueError("allowed contracts must include STRICT_SEQ_EQUIV")
        return value

    @field_validator("editable_path_patterns")
    @classmethod
    def paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(tuple(_normalized_relative_path(item) for item in value), "editable paths")


class PlannerPolicy(StrictContract):
    mode: Literal["HEURISTIC", "SINGLE_AGENT", "AGENT_COUNCIL"]
    runtime: Literal["DETERMINISTIC", "DIRECT", "LANGGRAPH"]
    fallback_order: tuple[Literal["SINGLE_AGENT", "HEURISTIC"], ...]
    max_proposals: int = Field(strict=True, gt=0)
    max_parallel_specialists: int = Field(strict=True, gt=0)
    max_revision_rounds: NonNegativeInt
    deadline_seconds: int = Field(strict=True, gt=0)
    aggregate_token_budget: NonNegativeInt

    @field_validator("fallback_order")
    @classmethod
    def fallbacks_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "planner fallback modes")


class ContextIsolationPolicy(StrictContract):
    mode: Literal["ROLE_SCOPED_EVIDENCE_PACKS"]
    shared_envelope_max_tokens: int = Field(strict=True, gt=0)
    private_pack_max_tokens: int = Field(strict=True, gt=0)
    chair_pack_max_tokens: int = Field(strict=True, gt=0)
    allow_read_only_retrieval: Literal[True]
    require_snapshot_hash_match: Literal[True]
    blind_independent_round: Literal[True]
    blind_parallel_critics: Literal[True]
    randomize_neutral_proposal_order: Literal[True]


class RecoveryPolicy(StrictContract):
    enabled: bool
    policy_version: NonEmptyString
    diagnostic_rule_registry: str
    fingerprint_schema: NonEmptyString
    similarity_threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    no_progress_wns_epsilon_ns: NonNegativeFloat
    no_progress_area_epsilon_percent: NonNegativeFloat
    repeated_failure_count: int = Field(strict=True, gt=0)
    max_recovery_depth_per_lineage: NonNegativeInt
    allow_targeted_recovery_council: bool
    force_fresh_sessions_after_stagnation: Literal[True]
    prohibit_same_transform_family_after_stagnation: Literal[True]

    @field_validator("diagnostic_rule_registry")
    @classmethod
    def registry_path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class PhysicalPolicy(StrictContract):
    placement_finalists: int = Field(strict=True, gt=0)
    routed_finalists: int = Field(strict=True, gt=0)
    repeat_final_seeds: tuple[NonNegativeInt, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def finalist_counts_and_seeds_are_valid(self) -> Self:
        if self.routed_finalists > self.placement_finalists:
            raise ValueError("routed finalists cannot exceed placement finalists")
        if len(self.repeat_final_seeds) != len(set(self.repeat_final_seeds)):
            raise ValueError("physical seeds must be unique")
        return self


class ProjectManifest(StrictContract):
    """Single strict human-authored entry point for one immutable run."""

    schema_version: Literal[2] = 2
    project: EntityId
    top: HdlIdentifier
    rtl: RtlManifest
    compilation_profiles: CompilationProfiles
    constraints: ConstraintPolicy
    technology: TechnologyPolicy
    analysis_views: tuple[AnalysisViewManifest, ...] = Field(min_length=2)
    power_activity: PowerActivityManifest
    cdc: CdcPolicy
    formal: FormalPolicy
    protection: ProtectionPolicy
    optimization: OptimizationPolicy
    planner: PlannerPolicy
    context_isolation: ContextIsolationPolicy
    recovery: RecoveryPolicy
    physical: PhysicalPolicy

    @model_validator(mode="after")
    def global_invariants_are_consistent(self) -> Self:
        view_ids = tuple(view.id for view in self.analysis_views)
        _unique(view_ids, "analysis view IDs")
        required_setup = {
            view.id for view in self.analysis_views if view.required and view.check == "SETUP"
        }
        required_hold = {
            view.id for view in self.analysis_views if view.required and view.check == "HOLD"
        }
        if not required_setup or not required_hold or required_setup & required_hold:
            raise ValueError("manifest requires distinct required setup and hold views")
        if any(view.sdc != self.constraints.sdc for view in self.analysis_views):
            raise ValueError("every analysis view must use the immutable manifest SDC")

        property_define = self.compilation_profiles.formal.property_only_define
        functional_defines = {
            _define_name(item)
            for item in (*self.rtl.defines, *self.compilation_profiles.functional.defines)
        }
        if property_define in functional_defines:
            raise ValueError("formal property-only define cannot appear in functional RTL")

        protected = set(self.protection.path_patterns)
        editable = set(self.optimization.editable_path_patterns)
        if protected & editable:
            raise ValueError("a protected path cannot appear in the editable allowlist")

        required_finalists = max(
            self.optimization.openroad_finalists,
            self.physical.placement_finalists,
            self.physical.routed_finalists,
        )
        if self.optimization.max_candidates < required_finalists:
            raise ValueError("max_candidates must cover every physical finalist")
        return self


class DesignContract(StrictContract):
    """Machine-generated immutable input consumed by all downstream jobs."""

    schema_version: Literal[1] = 1
    design_contract_id: EntityId
    run_id: EntityId
    project_manifest_hash: HashRef
    functional_source_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    top: HdlIdentifier
    parameters: dict[HdlIdentifier, JsonScalar]
    defines: tuple[Define, ...]
    functional_compilation_profile_hash: HashRef
    formal_compilation_profile_hash: HashRef
    constraint_snapshot_artifact: ArtifactRef
    constraint_snapshot_hash: HashRef
    analysis_view_ids: tuple[EntityId, ...] = Field(min_length=2)
    analysis_view_set_hash: HashRef
    power_activity_contract_id: EntityId | None
    power_activity_contract_hash: HashRef | None
    platform_lock_hash: HashRef
    cdc_policy_id: EntityId
    cdc_policy_hash: HashRef
    protection_policy_id: EntityId
    protection_policy_hash: HashRef
    formal_policy_id: EntityId
    formal_policy_hash: HashRef
    effective_policy_id: EntityId
    effective_policy_hash: HashRef
    organizer_decision_hash: HashRef
    created_at: UtcDatetime
    contract_hash: HashRef

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for parameter, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"parameter {parameter} must be finite")
        return value

    @field_validator("defines")
    @classmethod
    def defines_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(tuple(_define_name(item) for item in value), "design define names")
        return value

    @model_validator(mode="after")
    def sealed_identity_is_consistent(self) -> Self:
        source_order = tuple(item.uri for item in self.functional_source_artifacts)
        if source_order != tuple(sorted(source_order)):
            raise ValueError("functional source artifacts must be sorted by URI")
        if len(source_order) != len(set(source_order)):
            raise ValueError("functional source artifact URIs must be unique")
        if any(
            item.producer_stage_result_id is not None
            for item in self.functional_source_artifacts
        ):
            raise ValueError("functional source artifacts must be snapshotted inputs")
        if self.constraint_snapshot_artifact.producer_stage_result_id is not None:
            raise ValueError("constraint snapshot must be an input artifact")
        if self.constraint_snapshot_hash != self.constraint_snapshot_artifact.sha256:
            raise ValueError("constraint_snapshot_hash must match the constraint artifact")

        if self.analysis_view_ids != tuple(sorted(self.analysis_view_ids)):
            raise ValueError("analysis_view_ids must be sorted")
        if len(self.analysis_view_ids) != len(set(self.analysis_view_ids)):
            raise ValueError("analysis_view_ids must be unique")

        power_identity = (
            self.power_activity_contract_id is not None,
            self.power_activity_contract_hash is not None,
        )
        if power_identity[0] != power_identity[1]:
            raise ValueError("power activity contract ID and hash must be present together")

        expected_hash = canonical_sha256(self, exclude=frozenset({"contract_hash"}))
        if self.contract_hash != expected_hash:
            raise ValueError("contract_hash does not match canonical design identity")
        return self


__all__ = ["DesignContract", "ProjectManifest"]
