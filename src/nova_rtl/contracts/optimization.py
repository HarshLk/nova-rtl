"""Optimization opportunity, proposal, and immutable candidate contracts."""

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

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$"),
]
Fingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*:v[0-9]+:[A-Za-z0-9._-]+$"),
]
Confidence = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
Percentage = Annotated[float, Field(strict=True, ge=0.0, le=100.0, allow_inf_nan=False)]
GitCommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
TransformFamily = StableUpperString
TransformOperation = StableUpperString
SelectionClass = Literal[
    "PRIMARY_STRICT",
    "SECONDARY_RETIMED",
    "EXPLORATORY_LATENCY_AWARE",
]
CandidateClassification = Literal[
    "REJECTED_SAFETY",
    "REJECTED_CORRECTNESS",
    "REJECTED_POLICY",
    "INCONCLUSIVE",
    "INFRASTRUCTURE_ERROR",
    "VALID_NEGATIVE_RESULT",
    "FEASIBLE_DOMINATED",
    "FEASIBLE_PARETO",
    "SELECTED",
]

FEASIBLE_CLASSIFICATIONS = frozenset({"FEASIBLE_DOMINATED", "FEASIBLE_PARETO", "SELECTED"})


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _validate_evidence_snapshot(refs: tuple[EvidenceRef, ...], label: str) -> None:
    evidence_ids = tuple(item.evidence_id for item in refs)
    _require_unique(evidence_ids, f"{label} evidence IDs")
    if len({item.snapshot_hash for item in refs}) != 1:
        raise ValueError(f"{label} evidence must resolve in one snapshot")


class RootCause(StrictContract):
    category: StableUpperString
    confidence: Confidence


class OpportunitySeverity(StrictContract):
    worst_view_id: EntityId
    worst_slack_ns: FiniteFloat
    affected_endpoints: NonNegativeInt
    tns_share_percent: Percentage


class OptimizationOpportunity(StrictContract):
    """Evidence-grounded editable path or cone opportunity."""

    schema_version: Literal[1] = 1
    opportunity_id: EntityId
    parent_candidate_id: EntityId
    target_domain: EntityId
    target_analysis_view_id: EntityId
    affected_analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    root_causes: tuple[RootCause, ...] = Field(min_length=1)
    severity: OpportunitySeverity
    editability: Literal[
        "RTL_EDITABLE",
        "PROTECTED_OR_UNSAFE",
        "NO_RTL_ACTION",
        "INSUFFICIENT_EVIDENCE",
    ]
    source_spans: tuple[EntityId, ...]
    protected_neighbors: tuple[EntityId, ...]
    eligible_transform_families: tuple[TransformFamily, ...]
    proof_contracts: tuple[CorrectnessContract, ...]
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def opportunity_is_actionable_and_snapshot_bound(self) -> Self:
        _require_unique(self.affected_analysis_view_ids, "affected analysis views")
        _require_unique(self.source_spans, "source spans")
        _require_unique(self.protected_neighbors, "protected neighbors")
        _require_unique(self.eligible_transform_families, "eligible transform families")
        _require_unique(self.proof_contracts, "proof contracts")
        if self.target_analysis_view_id not in self.affected_analysis_view_ids:
            raise ValueError("target analysis view must be included in affected views")
        if self.severity.worst_view_id not in self.affected_analysis_view_ids:
            raise ValueError("severity worst view must be included in affected views")
        if set(self.source_spans) & set(self.protected_neighbors):
            raise ValueError("editable source spans cannot also be protected neighbors")
        _validate_evidence_snapshot(self.evidence_refs, "opportunity")

        if self.editability == "RTL_EDITABLE":
            if not (
                self.source_spans and self.eligible_transform_families and self.proof_contracts
            ):
                raise ValueError(
                    "editable opportunity requires source spans, transform families, "
                    "and proof contracts"
                )
            evidence_ids = {item.evidence_id for item in self.evidence_refs}
            if not set(self.source_spans).issubset(evidence_ids):
                raise ValueError("editable opportunity source spans require evidence refs")
        elif self.eligible_transform_families:
            raise ValueError("non-editable opportunity cannot authorize transform families")
        return self


class ProposalTarget(StrictContract):
    hierarchy: NonEmptyString
    source_span_id: EntityId
    cone_fingerprint: Fingerprint


class Transformation(StrictContract):
    operation: TransformOperation
    family: TransformFamily
    parameters: dict[str, JsonScalar] = Field(min_length=1)

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for name, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"transformation parameter {name} must be finite")
        return value


class ProposalCorrectness(StrictContract):
    contract: CorrectnessContract
    proof_scope: NonEmptyString
    reset_model: StableUpperString


class ProposalPrediction(StrictContract):
    timing_direction: Literal["IMPROVE", "NEUTRAL", "REGRESS", "UNKNOWN"]
    area_direction: Literal[
        "DECREASE",
        "NEUTRAL",
        "SMALL_INCREASE",
        "INCREASE",
        "UNKNOWN",
    ]
    confidence: Confidence


class OptimizationProposal(StrictContract):
    """Typed advisory transformation proposal with no authoritative metrics."""

    schema_version: Literal[2] = 2
    proposal_id: EntityId
    parent_candidate_id: EntityId
    opportunity_id: EntityId
    diagnosis_refs: tuple[EntityId, ...] = Field(min_length=1)
    target: ProposalTarget
    transformation: Transformation
    preconditions: tuple[StableUpperString, ...] = Field(min_length=1)
    correctness: ProposalCorrectness
    prediction: ProposalPrediction
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    abort_conditions: tuple[StableUpperString, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def proposal_is_safe_and_snapshot_bound(self) -> Self:
        _require_unique(self.diagnosis_refs, "diagnosis refs")
        _require_unique(self.preconditions, "preconditions")
        _require_unique(self.abort_conditions, "abort conditions")
        if "NO_PROTECTED_NODE_IN_EDIT_SET" not in self.preconditions:
            raise ValueError("proposal requires NO_PROTECTED_NODE_IN_EDIT_SET")
        _validate_evidence_snapshot(self.evidence_refs, "proposal")
        evidence_ids = {item.evidence_id for item in self.evidence_refs}
        if self.target.source_span_id not in evidence_ids:
            raise ValueError("proposal target source span must resolve in evidence refs")
        return self


class CandidateRecord(StrictContract):
    """Immutable candidate lineage and deterministic hard-gate state."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    run_id: EntityId
    parent_candidate_id: EntityId
    lineage_depth: int = Field(strict=True, gt=0)
    proposal_id: EntityId
    opportunity_id: EntityId
    rtl_snapshot_artifact: ArtifactRef
    patch_artifact: ArtifactRef
    source_hash: HashRef
    changed_spans: tuple[EntityId, ...] = Field(min_length=1)
    transform_fingerprint: Fingerprint
    required_correctness_contract: CorrectnessContract
    stage_result_ids: tuple[EntityId, ...]
    per_view_metrics: dict[EntityId, MetricSet]
    proof_result_id: EntityId | None
    binding_manifest_id: EntityId | None
    clock_inventory_id: EntityId | None
    cdc_inventory_id: EntityId | None
    hard_gate_summary: _CandidateHardGateSummary | None
    classification: CandidateClassification
    selection_class: SelectionClass
    terminal_disposition: StableUpperString
    created_at: UtcDatetime
    recovery_parent_failure_id: EntityId | None

    @model_validator(mode="after")
    def lineage_and_feasibility_are_coherent(self) -> Self:
        if self.parent_candidate_id == self.candidate_id:
            raise ValueError("candidate cannot be its own parent")
        if self.rtl_snapshot_artifact.artifact_id == self.patch_artifact.artifact_id:
            raise ValueError("candidate RTL snapshot and patch artifacts must be distinct")
        if self.source_hash != self.rtl_snapshot_artifact.sha256:
            raise ValueError("candidate source hash must bind the RTL snapshot artifact")
        _require_unique(self.changed_spans, "changed spans")
        _require_unique(self.stage_result_ids, "stage result IDs")
        for view_id, metrics in self.per_view_metrics.items():
            if metrics.analysis_view_id != view_id:
                raise ValueError("metric view key must match MetricSet analysis_view_id")

        contract_by_class = {
            "PRIMARY_STRICT": "STRICT_SEQ_EQUIV",
            "SECONDARY_RETIMED": "RETIMING_EQUIV",
            "EXPLORATORY_LATENCY_AWARE": "LATENCY_AWARE",
        }
        expected_contract = contract_by_class[self.selection_class]
        if self.required_correctness_contract != expected_contract:
            raise ValueError(
                f"{self.selection_class} requires correctness contract {expected_contract}"
            )

        if self.classification in FEASIBLE_CLASSIFICATIONS:
            hard_gate_refs = (
                self.proof_result_id,
                self.binding_manifest_id,
                self.clock_inventory_id,
                self.cdc_inventory_id,
            )
            if any(item is None for item in hard_gate_refs):
                raise ValueError("feasible candidate requires all hard-gate references")
            if not self.stage_result_ids or not self.per_view_metrics:
                raise ValueError("feasible candidate requires stage results and view metrics")
            if self.hard_gate_summary is None:
                raise ValueError("feasible candidate requires a hard-gate summary")
            gate = self.hard_gate_summary
            if gate.candidate_id != self.candidate_id or gate.source_hash != self.source_hash:
                raise ValueError("hard-gate source hash must bind the candidate RTL snapshot")
            if set(gate.required_analysis_view_ids) != set(self.per_view_metrics):
                raise ValueError("hard-gate required-view metrics must be complete")
            if set(gate.passed_stage_result_ids) != set(self.stage_result_ids):
                raise ValueError("hard-gate stage results must match candidate stage results")
            if (
                gate.proof_result_id != self.proof_result_id
                or gate.binding_manifest_id != self.binding_manifest_id
                or gate.clock_inventory_id != self.clock_inventory_id
                or gate.cdc_inventory_id != self.cdc_inventory_id
            ):
                raise ValueError("hard-gate summary references must match candidate references")
            if gate.proof_contract != self.required_correctness_contract:
                raise ValueError("hard-gate proof contract must match candidate contract")
            if (
                gate.proof_outcome != "PASS"
                or gate.binding_status not in {"EQUIVALENT", "APPROVED_SEMANTIC_REMAP"}
                or gate.clock_inventory_status != "COMPLETE"
                or gate.cdc_inventory_status != "UNCHANGED"
            ):
                raise ValueError("feasible candidate requires passing hard-gate outcomes")
        if self.classification == "SELECTED" and self.terminal_disposition != "SELECTED":
            raise ValueError("selected candidate requires SELECTED terminal disposition")
        return self


class _CandidateHardGateSummary(StrictContract):
    """Hashed resolver output proving every feasibility gate for one candidate."""

    candidate_id: EntityId
    source_hash: HashRef
    required_analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    passed_stage_result_ids: tuple[EntityId, ...] = Field(min_length=1)
    proof_result_id: EntityId
    proof_outcome: Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]
    proof_contract: CorrectnessContract
    binding_manifest_id: EntityId
    binding_status: Literal["EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"]
    clock_inventory_id: EntityId
    clock_inventory_status: Literal["COMPLETE", "INCOMPLETE", "MISMATCH"]
    cdc_inventory_id: EntityId
    cdc_inventory_status: Literal["UNCHANGED", "CHANGED", "AMBIGUOUS"]
    summary_hash: HashRef

    @model_validator(mode="after")
    def identities_are_unique_and_self_hashed(self) -> Self:
        _require_unique(self.required_analysis_view_ids, "required analysis view IDs")
        _require_unique(self.passed_stage_result_ids, "passed stage result IDs")
        expected = canonical_sha256(self, exclude=frozenset({"summary_hash"}))
        if self.summary_hash != expected:
            raise ValueError("summary_hash does not match canonical hard-gate summary")
        return self


CandidateRecord.model_rebuild()


class M4ViewComparison(StrictContract):
    """Exact baseline/candidate metrics and result hashes for one required view."""

    analysis_view_id: EntityId
    baseline_stage_result_hash: HashRef
    candidate_stage_result_hash: HashRef
    baseline_metrics: MetricSet
    candidate_metrics: MetricSet

    @model_validator(mode="after")
    def metrics_match_view(self) -> Self:
        if self.baseline_metrics.analysis_view_id != self.analysis_view_id:
            raise ValueError("baseline metrics do not match comparison view")
        if self.candidate_metrics.analysis_view_id != self.analysis_view_id:
            raise ValueError("candidate metrics do not match comparison view")
        return self


class M4SignoffReport(StrictContract):
    """Commit-bound proof that one M4 candidate passed the complete strict cascade."""

    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    commit_sha: GitCommitSha
    implementation_tree_hash: GitCommitSha
    m3_commit_sha: GitCommitSha
    m3_packet_hash: HashRef
    m3_report_hash: HashRef
    parent_run_id: EntityId
    parent_run_index_hash: HashRef
    candidate_id: EntityId
    candidate_run_id: EntityId
    candidate_run_index_hash: HashRef
    candidate_bundle_hash: HashRef
    candidate_source_hash: HashRef
    patch_hash: HashRef
    transform_fingerprint: Fingerprint
    evaluation_hash: HashRef
    gate_statuses: dict[str, Literal["PASS"]]
    prephysical_proof_hash: HashRef
    final_proof_hash: HashRef
    required_view_comparisons: dict[EntityId, M4ViewComparison]
    baseline_replay_digest: HashRef
    candidate_replay_digest: HashRef
    baseline_ledger_hash: HashRef
    candidate_ledger_hash: HashRef
    toolchain_receipt_hash: HashRef
    platform_lock_hash: HashRef
    input_set_hash: HashRef
    report_hash: HashRef

    @model_validator(mode="after")
    def identities_and_hashes_are_canonical(self) -> Self:
        expected_gates = ("0", "0.5", "1", "2", "3", "4", "5", "6", "7")
        if tuple(self.gate_statuses) != expected_gates:
            raise ValueError("M4 gate inventory must contain the complete canonical cascade")
        if set(self.required_view_comparisons) != {"asap7_setup", "asap7_hold"}:
            raise ValueError("M4 requires comparable ASAP7 setup and hold views")
        input_payload = {
            "commit_sha": self.commit_sha,
            "implementation_tree_hash": self.implementation_tree_hash,
            "m3_commit_sha": self.m3_commit_sha,
            "m3_packet_hash": self.m3_packet_hash,
            "m3_report_hash": self.m3_report_hash,
            "parent_run_index_hash": self.parent_run_index_hash,
            "candidate_run_index_hash": self.candidate_run_index_hash,
            "candidate_bundle_hash": self.candidate_bundle_hash,
            "prephysical_proof_hash": self.prephysical_proof_hash,
            "final_proof_hash": self.final_proof_hash,
            "baseline_replay_digest": self.baseline_replay_digest,
            "candidate_replay_digest": self.candidate_replay_digest,
            "baseline_ledger_hash": self.baseline_ledger_hash,
            "candidate_ledger_hash": self.candidate_ledger_hash,
            "toolchain_receipt_hash": self.toolchain_receipt_hash,
            "platform_lock_hash": self.platform_lock_hash,
        }
        if self.input_set_hash != canonical_sha256(input_payload):
            raise ValueError("input_set_hash does not match the M4 evidence boundary")
        if self.report_hash != canonical_sha256(self, exclude=frozenset({"report_hash"})):
            raise ValueError("report_hash does not match canonical M4 sign-off report")
        return self


__all__ = [
    "CandidateRecord",
    "M4SignoffReport",
    "M4ViewComparison",
    "OptimizationOpportunity",
    "OptimizationProposal",
]
