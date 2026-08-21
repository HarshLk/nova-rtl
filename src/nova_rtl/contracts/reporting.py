"""Experiment, Pareto, bounded-search, and report-bundle contracts."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    EvidenceRef,
    FiniteFloat,
    HashRef,
    MetricSet,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract, JsonScalar
from nova_rtl.contracts.optimization import Fingerprint, SelectionClass
from nova_rtl.contracts.planning import PlannerMode

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$"),
]
StableLowerName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$"),
]
ProofOutcome = Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _validate_metric_map(metrics: dict[str, MetricSet], label: str) -> None:
    for view_id, values in metrics.items():
        if values.analysis_view_id != view_id:
            raise ValueError(f"{label} metric view key must match MetricSet analysis_view_id")


class HumanReviewRecord(StrictContract):
    status: Literal["APPROVED", "REJECTED"]
    reviewer_id: EntityId
    decision_at: UtcDatetime
    note_hash: HashRef


class ExperimentRecord(StrictContract):
    """Append-only search ledger unit for one evaluated candidate."""

    schema_version: Literal[1] = 1
    experiment_record_id: EntityId
    run_id: EntityId
    planner_result_id: EntityId | None
    council_result_id: EntityId | None
    opportunity_id: EntityId
    cone_fingerprint: Fingerprint
    proposal_id: EntityId
    operation: StableUpperString
    parameters: dict[str, JsonScalar]
    parent_candidate_id: EntityId
    candidate_id: EntityId
    source_hash: HashRef
    patch_hash: HashRef
    transform_fingerprint: Fingerprint
    stage_result_ids: tuple[EntityId, ...]
    comparison_identity_hashes: dict[StableLowerName, HashRef] = Field(min_length=1)
    proof_result_id: EntityId | None
    proof_outcome: ProofOutcome | None
    counterexample_artifact_id: EntityId | None
    before_metrics: dict[EntityId, MetricSet]
    after_metrics: dict[EntityId, MetricSet]
    failure_event_id: EntityId | None
    repair_directive_id: EntityId | None
    candidate_failure_fingerprint_id: EntityId | None
    recovery_decision_id: EntityId | None
    descendant_outcome: StableUpperString | None
    role_ids: tuple[EntityId, ...]
    model_configuration_hashes: tuple[HashRef, ...]
    prompt_hashes: tuple[HashRef, ...]
    context_pack_hashes: tuple[HashRef, ...]
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    planner_latency_ms: NonNegativeInt
    eda_runtime_ms: NonNegativeInt
    terminal_disposition: StableUpperString
    human_review: HumanReviewRecord | None
    created_at: UtcDatetime

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for name, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"experiment parameter {name} must be finite")
        return value

    @model_validator(mode="after")
    def experiment_is_comparable_and_semantically_closed(self) -> Self:
        if self.parent_candidate_id == self.candidate_id:
            raise ValueError("candidate cannot be its own parent")
        for values, label in (
            (self.stage_result_ids, "stage result IDs"),
            (self.role_ids, "role IDs"),
            (self.model_configuration_hashes, "model configuration hashes"),
            (self.prompt_hashes, "prompt hashes"),
            (self.context_pack_hashes, "context pack hashes"),
        ):
            _require_unique(values, label)
        _validate_metric_map(self.before_metrics, "before")
        _validate_metric_map(self.after_metrics, "after")
        if set(self.before_metrics) != set(self.after_metrics):
            raise ValueError("before and after metrics must cover the same analysis views")
        if (self.proof_result_id is None) != (self.proof_outcome is None):
            raise ValueError("proof result ID and outcome must be present together")
        if self.proof_outcome == "PASS" and self.counterexample_artifact_id is not None:
            raise ValueError("PASS proof cannot reference a counterexample")
        if self.failure_event_id is None and any(
            item is not None
            for item in (
                self.repair_directive_id,
                self.candidate_failure_fingerprint_id,
                self.recovery_decision_id,
                self.descendant_outcome,
            )
        ):
            raise ValueError("recovery lineage requires a failure_event_id")
        return self


class ParetoRecord(StrictContract):
    schema_version: Literal[1] = 1
    pareto_record_id: EntityId
    candidate_id: EntityId
    correctness_contract: CorrectnessContract
    selection_class: SelectionClass
    feasibility_predicate_version: NonEmptyString
    required_view_metric_ids: dict[EntityId, EntityId] = Field(min_length=1)
    metric_vector: dict[StableLowerName, FiniteFloat] = Field(min_length=1)
    dominated_candidate_ids: tuple[EntityId, ...]
    objective_policy_rank: int = Field(strict=True, gt=0)
    binding_hash: HashRef
    clock_hash: HashRef
    cdc_hash: HashRef
    formal_hash: HashRef
    physical_stage: Literal[
        "OPENROAD_PHYSICAL", "OPENROAD_PLACED_CTS", "OPENROAD_ROUTED"
    ]
    comparison_identity_hashes: dict[StableLowerName, HashRef] = Field(min_length=1)
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def correctness_partition_and_dominance_are_coherent(self) -> Self:
        expected = {
            "PRIMARY_STRICT": "STRICT_SEQ_EQUIV",
            "SECONDARY_RETIMED": "RETIMING_EQUIV",
            "EXPLORATORY_LATENCY_AWARE": "LATENCY_AWARE",
        }[self.selection_class]
        if self.correctness_contract != expected:
            raise ValueError(f"{self.selection_class} requires {expected}")
        _require_unique(tuple(self.required_view_metric_ids.values()), "view metric IDs")
        _require_unique(self.dominated_candidate_ids, "dominated candidate IDs")
        if self.candidate_id in self.dominated_candidate_ids:
            raise ValueError("candidate cannot dominate itself")
        return self


class SearchRequest(StrictContract):
    schema_version: Literal[1] = 1
    search_request_id: EntityId
    run_id: EntityId
    design_contract_hash: HashRef
    policy_hash: HashRef
    transform_registry_hash: HashRef
    ordered_opportunity_ids: tuple[EntityId, ...] = Field(min_length=1)
    planner_mode: PlannerMode
    required_correctness_class: SelectionClass
    candidate_budget: int = Field(strict=True, gt=0)
    formal_budget: int = Field(strict=True, gt=0)
    physical_budget: int = Field(strict=True, gt=0)
    token_budget: int = Field(strict=True, gt=0)
    latency_budget_ms: int = Field(strict=True, gt=0)
    deterministic_seed: NonNegativeInt
    stop_policy_hash: HashRef
    created_at: UtcDatetime

    @model_validator(mode="after")
    def cascade_budgets_are_bounded(self) -> Self:
        _require_unique(self.ordered_opportunity_ids, "ordered opportunity IDs")
        if self.formal_budget > self.candidate_budget:
            raise ValueError("formal_budget cannot exceed candidate_budget")
        if self.physical_budget > self.formal_budget:
            raise ValueError("physical_budget cannot exceed formal_budget")
        return self


class ConsumedSearchBudgets(StrictContract):
    candidates: NonNegativeInt
    formal_jobs: NonNegativeInt
    physical_jobs: NonNegativeInt
    tokens: NonNegativeInt
    latency_ms: NonNegativeInt

    @model_validator(mode="after")
    def cascade_counts_are_coherent(self) -> Self:
        if self.physical_jobs > self.formal_jobs or self.formal_jobs > self.candidates:
            raise ValueError("consumed physical/formal/candidate counts violate cascade order")
        return self


class SearchResult(StrictContract):
    schema_version: Literal[1] = 1
    search_result_id: EntityId
    search_request_id: EntityId
    status: Literal[
        "COMPLETED",
        "BUDGET_EXHAUSTED",
        "NO_FEASIBLE_CANDIDATE",
        "STOPPED_BY_POLICY",
        "INFRASTRUCTURE_BLOCKED",
    ]
    ordered_candidate_ids: tuple[EntityId, ...]
    feasible_candidate_ids: tuple[EntityId, ...]
    pareto_candidate_ids: tuple[EntityId, ...]
    selected_candidate_id: EntityId | None
    stop_reason_code: StableUpperString
    consumed_budgets: ConsumedSearchBudgets
    planner_result_ids: tuple[EntityId, ...]
    council_result_ids: tuple[EntityId, ...]
    recovery_decision_ids: tuple[EntityId, ...]
    event_sequence_range: tuple[NonNegativeInt, NonNegativeInt]
    artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def terminal_sets_are_nested_and_coherent(self) -> Self:
        for values, label in (
            (self.ordered_candidate_ids, "ordered candidate IDs"),
            (self.feasible_candidate_ids, "feasible candidate IDs"),
            (self.pareto_candidate_ids, "Pareto candidate IDs"),
            (self.planner_result_ids, "planner result IDs"),
            (self.council_result_ids, "council result IDs"),
            (self.recovery_decision_ids, "recovery decision IDs"),
        ):
            _require_unique(values, label)
        ordered = set(self.ordered_candidate_ids)
        feasible = set(self.feasible_candidate_ids)
        pareto = set(self.pareto_candidate_ids)
        if not feasible.issubset(ordered):
            raise ValueError("feasible candidates must be evaluated candidates")
        if not pareto.issubset(feasible):
            raise ValueError("Pareto candidates must be feasible candidates")
        if self.selected_candidate_id is not None and self.selected_candidate_id not in pareto:
            raise ValueError("selected candidate must be on the Pareto frontier")
        if self.status == "COMPLETED" and self.selected_candidate_id is None:
            raise ValueError("COMPLETED search requires a selected candidate")
        if self.status == "NO_FEASIBLE_CANDIDATE" and (
            feasible or pareto or self.selected_candidate_id is not None
        ):
            raise ValueError("NO_FEASIBLE_CANDIDATE cannot contain feasible results")
        if self.event_sequence_range[1] < self.event_sequence_range[0]:
            raise ValueError("event sequence range must increase")
        _require_unique(
            tuple(item.artifact_id for item in self.artifact_refs), "search artifact IDs"
        )
        return self


class ClaimEvidence(StrictContract):
    artifact_ids: tuple[EntityId, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    comparison_identity_hashes: dict[StableLowerName, HashRef] = Field(min_length=1)

    @model_validator(mode="after")
    def claim_references_are_unique(self) -> Self:
        _require_unique(self.artifact_ids, "claim artifact IDs")
        _require_unique(
            tuple(item.evidence_id for item in self.evidence_refs), "claim evidence IDs"
        )
        if not {item.artifact_id for item in self.evidence_refs}.issubset(self.artifact_ids):
            raise ValueError("claim evidence artifacts must appear in claim artifact IDs")
        return self


class ReportBundle(StrictContract):
    """Hash-linked claim index and sealed replay/report identity."""

    schema_version: Literal[1] = 1
    report_bundle_id: EntityId
    run_id: EntityId
    selected_candidate_id: EntityId
    claim_to_evidence_index: dict[StableLowerName, ClaimEvidence] = Field(min_length=1)
    baseline_final_comparison_identities: dict[StableLowerName, HashRef] = Field(min_length=1)
    timing_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    ppa_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    formal_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    binding_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    clock_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    cdc_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    experiment_ledger_sequence_range: tuple[NonNegativeInt, NonNegativeInt]
    experiment_ledger_hash: HashRef
    replay_manifest_artifact: ArtifactRef
    replay_manifest_hash: HashRef
    ui_snapshot_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    bundle_hash: HashRef
    created_at: UtcDatetime

    @model_validator(mode="after")
    def every_claim_resolves_and_bundle_is_self_hashed(self) -> Self:
        if self.experiment_ledger_sequence_range[1] < self.experiment_ledger_sequence_range[0]:
            raise ValueError("experiment ledger sequence range must increase")
        if self.replay_manifest_hash != self.replay_manifest_artifact.sha256:
            raise ValueError("replay manifest hash must match its artifact")

        artifacts = (
            self.timing_artifact_refs
            + self.ppa_artifact_refs
            + self.formal_artifact_refs
            + self.binding_artifact_refs
            + self.clock_artifact_refs
            + self.cdc_artifact_refs
            + (self.replay_manifest_artifact,)
            + self.ui_snapshot_refs
        )
        artifacts_by_id: dict[str, ArtifactRef] = {}
        for artifact in artifacts:
            existing = artifacts_by_id.get(artifact.artifact_id)
            if existing is not None and existing != artifact:
                raise ValueError("duplicate report artifact ID has conflicting identity")
            artifacts_by_id[artifact.artifact_id] = artifact

        for claim in self.claim_to_evidence_index.values():
            if not set(claim.artifact_ids).issubset(artifacts_by_id):
                raise ValueError("claim artifact ID does not resolve in report bundle")
            for name, identity_hash in claim.comparison_identity_hashes.items():
                if self.baseline_final_comparison_identities.get(name) != identity_hash:
                    raise ValueError("claim comparison identity does not match bundle identity")

        expected = canonical_sha256(self, exclude=frozenset({"bundle_hash"}))
        if self.bundle_hash != expected:
            raise ValueError("bundle_hash does not match canonical report bundle")
        return self


__all__ = [
    "ExperimentRecord",
    "ParetoRecord",
    "ReportBundle",
    "SearchRequest",
    "SearchResult",
]
