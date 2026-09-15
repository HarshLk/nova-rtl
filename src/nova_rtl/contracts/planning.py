"""Planner, context, provider, and bounded-council contracts."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    Diagnostic,
    EntityId,
    EvidenceRef,
    HashRef,
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
SchemaName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,95}$"),
]
Confidence = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
Percentage = Annotated[float, Field(strict=True, ge=0.0, le=100.0, allow_inf_nan=False)]
PlannerMode = Literal["HEURISTIC", "SINGLE_AGENT", "AGENT_COUNCIL"]
RoleStatus = Literal["PASS", "NO_SAFE_PROPOSAL", "ERROR", "TIMEOUT"]


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _require_one_snapshot(refs: tuple[EvidenceRef, ...], label: str) -> str:
    snapshots = {item.snapshot_hash for item in refs}
    if len(snapshots) != 1:
        raise ValueError(f"{label} evidence must resolve in one snapshot")
    return next(iter(snapshots))


class CouncilPolicy(StrictContract):
    """Hard authority and resource ceiling for one M8 deliberation."""

    schema_version: Literal[1] = 1
    max_reasoning_roles: int = Field(strict=True, gt=0, le=5)
    max_strategist_roles: int = Field(strict=True, ge=2, le=3)
    max_final_proposals: int = Field(strict=True, gt=0, le=3)
    max_revisions: int = Field(strict=True, ge=0, le=1)
    max_aggregate_tokens: int = Field(strict=True, gt=0, le=30_000)
    deadline_seconds: int = Field(strict=True, gt=0, le=120)
    fan_out_limit: int = Field(strict=True, gt=0, le=4)
    fan_in_limit: int = Field(strict=True, gt=0, le=4)
    fallback_order: tuple[Literal["SINGLE_AGENT", "HEURISTIC"], ...] = Field(
        min_length=1, max_length=2
    )
    allow_model_tools: Literal[False]
    allow_model_writes: Literal[False]
    external_tracing: Literal[False]
    policy_hash: HashRef

    @model_validator(mode="after")
    def bounds_and_identity_are_coherent(self) -> Self:
        _require_unique(self.fallback_order, "council fallback order")
        if self.fallback_order[-1] != "HEURISTIC":
            raise ValueError("council policy must terminate in deterministic fallback")
        if self.fan_in_limit > self.fan_out_limit:
            raise ValueError("council fan-in cannot exceed fan-out")
        if self.policy_hash != canonical_sha256(
            self, exclude=frozenset({"policy_hash"})
        ):
            raise ValueError("council policy hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> CouncilPolicy:
        payload = {"schema_version": 1, **values}
        return cls(**payload, policy_hash=canonical_sha256(payload))


class CouncilRoute(StrictContract):
    """Deterministic, auditable selection of bounded council roles."""

    schema_version: Literal[1] = 1
    council_route_id: EntityId
    opportunity_id: EntityId
    policy_hash: HashRef
    root_cause: StableUpperString
    proposer_roles: tuple[EntityId, ...] = Field(min_length=2, max_length=2)
    critic_roles: tuple[EntityId, ...] = Field(min_length=2, max_length=2)
    chair_role: EntityId
    reason_codes: tuple[StableUpperString, ...] = Field(min_length=1)
    route_hash: HashRef

    @model_validator(mode="after")
    def route_is_independent_and_self_hashed(self) -> Self:
        _require_unique(self.proposer_roles, "council proposer roles")
        _require_unique(self.critic_roles, "council critic roles")
        _require_unique(self.reason_codes, "council route reason codes")
        if set(self.critic_roles) != {"formal_critic", "ppa_critic"}:
            raise ValueError("council route requires formal and PPA critics")
        selected = (*self.proposer_roles, *self.critic_roles, self.chair_role)
        _require_unique(selected, "council selected roles")
        if len(selected) > 5:
            raise ValueError("council route exceeds the absolute role bound")
        if self.route_hash != canonical_sha256(
            self, exclude=frozenset({"council_route_id", "route_hash"})
        ):
            raise ValueError("council route hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> CouncilRoute:
        payload = {"schema_version": 1, **values}
        route_hash = canonical_sha256(payload)
        return cls(
            **payload,
            council_route_id=f"council_route_{route_hash[-24:]}",
            route_hash=route_hash,
        )


class PlannerRequest(StrictContract):
    schema_version: Literal[1] = 1
    planner_request_id: EntityId
    run_id: EntityId
    parent_candidate_id: EntityId
    opportunity_id: EntityId
    evidence_snapshot_hash: HashRef
    design_contract_hash: HashRef
    policy_hash: HashRef
    transform_registry_hash: HashRef
    authorized_evidence_ids: tuple[EntityId, ...] = Field(min_length=1)
    planner_mode: PlannerMode
    proposal_limit: int = Field(strict=True, gt=0)
    token_budget: int = Field(strict=True, gt=0)
    latency_budget_ms: int = Field(strict=True, gt=0)
    deadline: UtcDatetime
    deterministic_seed: NonNegativeInt
    required_output_schema_name: SchemaName
    required_output_schema_version: int = Field(strict=True, gt=0)

    @field_validator("authorized_evidence_ids")
    @classmethod
    def evidence_authority_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique(value, "authorized evidence IDs")
        return value


class ContextRequest(StrictContract):
    schema_version: Literal[1] = 1
    context_request_id: EntityId
    planner_request_id: EntityId
    role: EntityId
    common_envelope_hash: HashRef
    private_evidence_ids: tuple[EntityId, ...]
    retrieval_allowlist: tuple[EntityId, ...]
    snapshot_hash: HashRef
    token_budget: int = Field(strict=True, gt=0)
    redaction_policy_hash: HashRef

    @model_validator(mode="after")
    def private_evidence_does_not_expand_retrieval(self) -> Self:
        _require_unique(self.private_evidence_ids, "private evidence IDs")
        _require_unique(self.retrieval_allowlist, "retrieval allowlist")
        if not set(self.private_evidence_ids).issubset(self.retrieval_allowlist):
            raise ValueError("private evidence IDs must be within the retrieval allowlist")
        return self


class RoleContextPack(StrictContract):
    schema_version: Literal[1] = 1
    role_context_pack_id: EntityId
    context_request_id: EntityId
    role: EntityId
    common_envelope_artifact: ArtifactRef
    common_envelope_hash: HashRef
    private_pack_artifact: ArtifactRef
    private_pack_hash: HashRef
    retrieval_grants: tuple[EntityId, ...]
    rendered_message_artifact: ArtifactRef
    rendered_message_hash: HashRef
    estimated_tokens: NonNegativeInt
    created_at: UtcDatetime

    @model_validator(mode="after")
    def artifact_hashes_are_exact(self) -> Self:
        pairs = (
            (self.common_envelope_hash, self.common_envelope_artifact.sha256),
            (self.private_pack_hash, self.private_pack_artifact.sha256),
            (self.rendered_message_hash, self.rendered_message_artifact.sha256),
        )
        if any(declared != actual for declared, actual in pairs):
            raise ValueError("context-pack hash must match exact artifact bytes")
        _require_unique(self.retrieval_grants, "retrieval grants")
        artifact_ids = {
            self.common_envelope_artifact.artifact_id,
            self.private_pack_artifact.artifact_id,
            self.rendered_message_artifact.artifact_id,
        }
        if len(artifact_ids) != 3:
            raise ValueError("context-pack artifacts must be distinct")
        return self

    def validate_against(
        self, context_request: ContextRequest, planner_request: PlannerRequest
    ) -> Self:
        """Resolve and enforce the persisted context authority chain."""

        if self.context_request_id != context_request.context_request_id:
            raise ValueError("context pack must resolve to its ContextRequest")
        if context_request.planner_request_id != planner_request.planner_request_id:
            raise ValueError("ContextRequest must resolve to its PlannerRequest")
        if self.role != context_request.role:
            raise ValueError("context pack role must match ContextRequest role")
        if self.common_envelope_hash != context_request.common_envelope_hash:
            raise ValueError("context pack common envelope must match ContextRequest")
        if context_request.snapshot_hash != planner_request.evidence_snapshot_hash:
            raise ValueError("context snapshot must match planner evidence snapshot")
        if not set(self.retrieval_grants).issubset(context_request.retrieval_allowlist):
            raise ValueError("context pack retrieval grants must stay within retrieval allowlist")
        if self.estimated_tokens > context_request.token_budget:
            raise ValueError("context pack token estimate cannot exceed ContextRequest budget")
        if not set(context_request.retrieval_allowlist).issubset(
            planner_request.authorized_evidence_ids
        ):
            raise ValueError("ContextRequest retrieval allowlist exceeds planner authority")
        return self


class ProviderResult(StrictContract):
    schema_version: Literal[1] = 1
    provider_result_id: EntityId
    planner_request_id: EntityId
    context_request_id: EntityId
    status: Literal[
        "PASS", "INVALID_OUTPUT", "PROVIDER_ERROR", "TIMEOUT", "BUDGET_EXHAUSTED"
    ]
    provider_id: EntityId
    model_id: NonEmptyString
    provider_configuration_hash: HashRef
    prompt_hash: HashRef
    requested_schema_name: SchemaName
    requested_schema_version: int = Field(strict=True, gt=0)
    structured_output_artifact: ArtifactRef | None
    structured_output_hash: HashRef | None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    error_code: StableUpperString | None
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def terminal_output_matches_status(self) -> Self:
        if self.status == "PASS":
            if self.structured_output_artifact is None or self.structured_output_hash is None:
                raise ValueError("PASS provider result requires structured output")
            if self.structured_output_artifact.sha256 != self.structured_output_hash:
                raise ValueError("structured output hash must match its artifact")
            if self.error_code is not None:
                raise ValueError("PASS provider result cannot declare an error code")
        else:
            has_output = (
                self.structured_output_artifact is not None
                or self.structured_output_hash is not None
            )
            if has_output:
                raise ValueError("non-pass provider result cannot expose normalized output")
            if self.error_code is None:
                raise ValueError("non-pass provider result requires an error code")
        return self


class M6GateEvidence(StrictContract):
    """One reproducible M6 safety/equivalence gate and its preserved output."""

    evidence_id: EntityId
    argv: tuple[NonEmptyString, ...] = Field(min_length=1)
    output_relative_path: NonEmptyString
    output_hash: HashRef
    output_size_bytes: NonNegativeInt
    passed_test_count: int = Field(strict=True, gt=0)


class M6SignoffReport(StrictContract):
    """Commit-bound proof that constrained single-agent planning is safe."""

    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    implementation_tree_hash: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{40}$")
    ]
    m5_commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    m5_packet_hash: HashRef
    m5_report_hash: HashRef
    run_id: EntityId
    m5_search_bundle_hash: HashRef
    planner_run_hash: HashRef
    planner_implementation_hash: HashRef
    planner_policy_hash: HashRef
    provider_response_hash: HashRef | None
    planner_request_hashes: tuple[HashRef, ...] = Field(min_length=1)
    context_pack_hashes: tuple[HashRef, ...] = Field(min_length=1)
    provider_result_hashes: tuple[HashRef, ...] = Field(min_length=1)
    planner_result_hashes: tuple[HashRef, ...] = Field(min_length=1)
    proposal_hashes: tuple[HashRef, ...] = Field(min_length=1)
    normalized_plan_hashes: tuple[HashRef, ...] = Field(min_length=1)
    registered_operations: tuple[StableUpperString, ...] = Field(min_length=1)
    validated_planner_modes: tuple[PlannerMode, ...]
    provider_attempt_count: int = Field(strict=True, gt=0)
    fallback_count: int = Field(strict=True, gt=0)
    retrieval_grant_count: NonNegativeInt
    retrieval_denial_count: NonNegativeInt
    budget_compliance: Literal[True]
    planner_matrix_evidence: M6GateEvidence
    input_set_hash: HashRef
    report_hash: HashRef

    @model_validator(mode="after")
    def milestone_boundary_is_complete_and_hashed(self) -> Self:
        if self.validated_planner_modes != ("HEURISTIC", "SINGLE_AGENT"):
            raise ValueError("M6 must validate heuristic and single-agent modes")
        for values, label in (
            (self.planner_request_hashes, "planner request hashes"),
            (self.context_pack_hashes, "context pack hashes"),
            (self.provider_result_hashes, "provider result hashes"),
            (self.planner_result_hashes, "planner result hashes"),
            (self.proposal_hashes, "proposal hashes"),
            (self.normalized_plan_hashes, "normalized plan hashes"),
            (self.registered_operations, "registered operations"),
        ):
            _require_unique(values, label)
        if self.fallback_count > self.provider_attempt_count:
            raise ValueError("fallback count cannot exceed provider attempts")
        payload = {
            key: value
            for key, value in self.model_dump(mode="python").items()
            if key not in {"schema_version", "status", "input_set_hash", "report_hash"}
        }
        if self.input_set_hash != canonical_sha256(payload):
            raise ValueError("M6 input_set_hash differs from sign-off evidence")
        if self.report_hash != canonical_sha256(self, exclude=frozenset({"report_hash"})):
            raise ValueError("M6 report_hash is not canonical")
        return self


class PlannerResult(StrictContract):
    schema_version: Literal[1] = 1
    planner_result_id: EntityId
    run_id: EntityId
    opportunity_id: EntityId
    planner_mode: PlannerMode
    status: Literal[
        "PASS", "NO_SAFE_PROPOSAL", "PARTIAL", "PROVIDER_ERROR", "SCHEMA_ERROR"
    ]
    proposal_ids: tuple[EntityId, ...]
    rejected_output_diagnostics: tuple[Diagnostic, ...]
    context_pack_hashes: tuple[HashRef, ...]
    council_result_id: EntityId | None
    provider_id: EntityId | None
    model_id: NonEmptyString | None
    prompt_hash: HashRef | None
    output_schema_hash: HashRef
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    fallback_used: bool
    upstream_provider_result_id: EntityId | None

    @model_validator(mode="after")
    def status_and_lineage_are_coherent(self) -> Self:
        _require_unique(self.proposal_ids, "proposal IDs")
        _require_unique(self.context_pack_hashes, "context pack hashes")
        if self.status == "PASS" and not self.proposal_ids:
            raise ValueError("PASS planner result requires a validated proposal")
        if self.status in {"NO_SAFE_PROPOSAL", "PROVIDER_ERROR", "SCHEMA_ERROR"} and (
            self.proposal_ids
        ):
            raise ValueError(f"{self.status} planner result cannot contain proposals")
        if self.fallback_used:
            if self.upstream_provider_result_id is None:
                raise ValueError("fallback must retain its upstream provider result")
            if not self.rejected_output_diagnostics:
                raise ValueError("fallback must retain upstream rejection diagnostics")
        elif self.upstream_provider_result_id is not None:
            raise ValueError("non-fallback result cannot declare an upstream provider result")
        if self.planner_mode == "AGENT_COUNCIL" and self.status in {"PASS", "PARTIAL"}:
            if self.council_result_id is None:
                raise ValueError("council planner result requires council_result_id")
        elif self.planner_mode != "AGENT_COUNCIL" and self.council_result_id is not None:
            raise ValueError("non-council planner result cannot declare council_result_id")
        return self


class PlanningRootCause(StrictContract):
    category: StableUpperString
    confidence: Confidence


class EvidenceBackedClaim(StrictContract):
    claim: NonEmptyString
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)


class DiagnosisReport(StrictContract):
    schema_version: Literal[1] = 1
    diagnosis_report_id: EntityId
    planner_request_id: EntityId
    role: EntityId
    opportunity_id: EntityId
    snapshot_hash: HashRef
    ranked_root_causes: tuple[PlanningRootCause, ...]
    editability: Literal[
        "RTL_EDITABLE", "PROTECTED_OR_UNSAFE", "NO_RTL_ACTION", "INSUFFICIENT_EVIDENCE"
    ]
    safety_assessment: StableUpperString
    claims: tuple[EvidenceBackedClaim, ...]
    uncertainties: tuple[NonEmptyString, ...]
    disposition: Literal["ACTIONABLE", "NO_RTL_ACTION", "INSUFFICIENT_EVIDENCE"]

    @model_validator(mode="after")
    def claims_are_snapshot_bound(self) -> Self:
        refs = tuple(ref for claim in self.claims for ref in claim.evidence_refs)
        if refs and _require_one_snapshot(refs, "diagnosis") != self.snapshot_hash:
            raise ValueError("diagnosis evidence snapshot must match snapshot_hash")
        if self.disposition == "ACTIONABLE" and not (self.ranked_root_causes and self.claims):
            raise ValueError("actionable diagnosis requires root causes and evidence-backed claims")
        if self.disposition == "ACTIONABLE" and self.editability != "RTL_EDITABLE":
            raise ValueError("actionable diagnosis must be RTL_EDITABLE")
        return self


class TransformRecommendation(StrictContract):
    schema_version: Literal[1] = 1
    transform_recommendation_id: EntityId
    diagnosis_report_id: EntityId
    family: StableUpperString
    operation: StableUpperString
    target_source_span_id: EntityId
    parameters: dict[str, JsonScalar] = Field(min_length=1)
    preconditions: tuple[StableUpperString, ...] = Field(min_length=1)
    contract: CorrectnessContract
    abort_conditions: tuple[StableUpperString, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    expected_structural_direction: StableUpperString

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for name, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"recommendation parameter {name} must be finite")
        return value

    @model_validator(mode="after")
    def recommendation_is_safe_and_grounded(self) -> Self:
        _require_unique(self.preconditions, "recommendation preconditions")
        _require_unique(self.abort_conditions, "recommendation abort conditions")
        if "NO_PROTECTED_NODE_IN_EDIT_SET" not in self.preconditions:
            raise ValueError("recommendation requires NO_PROTECTED_NODE_IN_EDIT_SET")
        _require_one_snapshot(self.evidence_refs, "recommendation")
        if self.target_source_span_id not in {
            item.evidence_id for item in self.evidence_refs
        }:
            raise ValueError("recommendation target must resolve in evidence refs")
        return self


class CritiqueObjection(StrictContract):
    objection_id: EntityId
    severity: Literal["MANDATORY", "ADVISORY"]
    category: StableUpperString
    message: NonEmptyString
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    requested_disposition: Literal["ACCEPT", "REVISE", "REJECT"]


class CritiqueReport(StrictContract):
    schema_version: Literal[1] = 1
    critique_report_id: EntityId
    critic_role: EntityId
    critic_class: Literal["FORMAL", "PPA"]
    proposal_card_id: EntityId
    objections: tuple[CritiqueObjection, ...]

    @model_validator(mode="after")
    def objections_are_unique_and_grounded(self) -> Self:
        _require_unique(
            tuple(item.objection_id for item in self.objections), "critique objection IDs"
        )
        refs = tuple(ref for item in self.objections for ref in item.evidence_refs)
        if refs:
            _require_one_snapshot(refs, "critique")
        return self


class CritiqueDisposition(StrictContract):
    schema_version: Literal[1] = 1
    critique_disposition_id: EntityId
    objection_id: EntityId
    action: Literal["ACCEPT", "REVISE", "REJECT"]
    reason_code: StableUpperString
    resulting_proposal_id: EntityId | None
    chair_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def action_has_a_coherent_result(self) -> Self:
        _require_one_snapshot(self.chair_evidence_refs, "chair disposition")
        if self.action == "REJECT" and self.resulting_proposal_id is not None:
            raise ValueError("REJECT disposition cannot produce a proposal")
        if self.action in {"ACCEPT", "REVISE"} and self.resulting_proposal_id is None:
            raise ValueError(f"{self.action} disposition requires a resulting proposal")
        return self


class ProposalShortlist(StrictContract):
    schema_version: Literal[1] = 1
    proposal_shortlist_id: EntityId
    status: Literal["PASS", "NO_SAFE_PROPOSAL", "INSUFFICIENT_EVIDENCE", "PARTIAL"]
    ordered_proposal_ids: tuple[EntityId, ...] = Field(max_length=3)
    fallback_eligible: bool
    unresolved_mandatory_finding_count: NonNegativeInt

    @model_validator(mode="after")
    def shortlist_is_executable_only_when_closed(self) -> Self:
        _require_unique(self.ordered_proposal_ids, "shortlisted proposal IDs")
        if self.ordered_proposal_ids and self.unresolved_mandatory_finding_count != 0:
            raise ValueError("executable shortlist cannot have unresolved mandatory findings")
        if self.status == "PASS" and not self.ordered_proposal_ids:
            raise ValueError("PASS shortlist requires at least one proposal")
        if self.status in {"NO_SAFE_PROPOSAL", "INSUFFICIENT_EVIDENCE"} and (
            self.ordered_proposal_ids
        ):
            raise ValueError(f"{self.status} shortlist cannot contain proposals")
        return self


class CouncilTraceEvent(StrictContract):
    sequence: NonNegativeInt
    event_type: StableUpperString
    role_id: EntityId
    timestamp: UtcDatetime


class CouncilTrace(StrictContract):
    schema_version: Literal[1] = 1
    council_trace_id: EntityId
    selected_role_ids: tuple[EntityId, ...] = Field(min_length=1)
    events: tuple[CouncilTraceEvent, ...] = Field(min_length=1)
    prompt_hashes: tuple[HashRef, ...]
    model_configuration_hashes: tuple[HashRef, ...]
    context_pack_hashes: tuple[HashRef, ...]
    retrieval_log_hashes: tuple[HashRef, ...]
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    deadline_outcome: Literal["MET", "EXCEEDED", "CANCELLED"]
    cancelled: bool
    trace_completeness_percent: Percentage

    @model_validator(mode="after")
    def trace_is_ordered_and_role_scoped(self) -> Self:
        _require_unique(self.selected_role_ids, "selected role IDs")
        sequences = tuple(item.sequence for item in self.events)
        if sequences != tuple(range(len(self.events))):
            raise ValueError("council trace event sequence must be contiguous from zero")
        if any(item.role_id not in self.selected_role_ids for item in self.events):
            raise ValueError("trace event role must be selected")
        if self.cancelled != (self.deadline_outcome == "CANCELLED"):
            raise ValueError("cancelled flag must match deadline outcome")
        for hashes, label in (
            (self.prompt_hashes, "prompt hashes"),
            (self.model_configuration_hashes, "model configuration hashes"),
            (self.context_pack_hashes, "context pack hashes"),
            (self.retrieval_log_hashes, "retrieval log hashes"),
        ):
            _require_unique(hashes, label)
        return self


class CouncilRequest(StrictContract):
    schema_version: Literal[1] = 1
    council_request_id: EntityId
    planner_request_id: EntityId
    council_route_id: EntityId
    council_route_hash: HashRef
    blinded_proposer_roles: tuple[EntityId, ...] = Field(min_length=2)
    critic_roles: tuple[EntityId, ...] = Field(min_length=1)
    chair_role: EntityId
    context_policy_hash: HashRef
    fan_out_limit: int = Field(strict=True, gt=0)
    fan_in_limit: int = Field(strict=True, gt=0)
    aggregate_token_budget: int = Field(strict=True, gt=0)
    aggregate_latency_budget_ms: int = Field(strict=True, gt=0)
    deadline: UtcDatetime
    event_stream_id: EntityId

    @model_validator(mode="after")
    def bounded_route_has_independent_roles(self) -> Self:
        _require_unique(self.blinded_proposer_roles, "blinded proposer roles")
        _require_unique(self.critic_roles, "critic roles")
        mandatory_critics = {"formal_critic", "ppa_critic"}
        if not mandatory_critics.issubset(self.critic_roles):
            raise ValueError("council request requires both mandatory critics")
        selected = set(self.blinded_proposer_roles) | set(self.critic_roles)
        if len(selected) != len(self.blinded_proposer_roles) + len(self.critic_roles):
            raise ValueError("proposer and critic roles must be disjoint")
        if self.chair_role in selected:
            raise ValueError("chair role must be independent from proposer and critic roles")
        if self.fan_out_limit < len(selected):
            raise ValueError("fan_out_limit cannot be smaller than selected parallel roles")
        if self.fan_in_limit > self.fan_out_limit:
            raise ValueError("fan_in_limit cannot exceed fan_out_limit")
        return self


class PrivateRoleRecord(StrictContract):
    role_id: EntityId
    role_kind: Literal["PROPOSER", "CRITIC", "CHAIR"]
    private_pack_hash: HashRef
    retrieved_evidence_ids: tuple[EntityId, ...]
    blinded_round: bool
    structured_submission_artifact: ArtifactRef
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    status: RoleStatus

    @model_validator(mode="after")
    def role_record_is_private_and_unique(self) -> Self:
        _require_unique(self.retrieved_evidence_ids, "retrieved evidence IDs")
        if self.role_kind in {"PROPOSER", "CRITIC"} and not self.blinded_round:
            raise ValueError("proposer and critic records must be blinded")
        return self


class ProposalCard(StrictContract):
    proposal_card_id: EntityId
    proposal_id: EntityId
    normalized_proposal_hash: HashRef


class CouncilRevisionRecord(StrictContract):
    revision_id: EntityId
    source_proposal_id: EntityId
    revised_proposal_id: EntityId
    reason_codes: tuple[StableUpperString, ...] = Field(min_length=1)


class CouncilResult(StrictContract):
    schema_version: Literal[1] = 1
    council_result_id: EntityId
    council_request_id: EntityId
    status: Literal[
        "PASS",
        "NO_SAFE_PROPOSAL",
        "INSUFFICIENT_EVIDENCE",
        "PARTIAL",
        "PLANNER_ERROR",
        "CANCELLED",
        "BUDGET_EXHAUSTED",
    ]
    proposal_shortlist: ProposalShortlist
    council_trace_id: EntityId
    run_id: EntityId
    opportunity_id: EntityId
    snapshot_hash: HashRef
    policy_hash: HashRef
    route_reason_codes: tuple[StableUpperString, ...] = Field(min_length=1)
    selected_role_ids: tuple[EntityId, ...] = Field(min_length=1)
    common_safety_envelope_hash: HashRef
    private_role_records: tuple[PrivateRoleRecord, ...]
    proposal_cards: tuple[ProposalCard, ...]
    critique_reports: tuple[CritiqueReport, ...]
    critique_dispositions: tuple[CritiqueDisposition, ...]
    revision_records: tuple[CouncilRevisionRecord, ...] = Field(max_length=1)
    final_ordered_proposal_ids: tuple[EntityId, ...] = Field(max_length=3)
    total_tokens: NonNegativeInt
    total_latency_ms: NonNegativeInt
    deadline_outcome: Literal["MET", "EXCEEDED", "CANCELLED"]
    trace_completeness_percent: Percentage

    @model_validator(mode="after")
    def council_is_complete_bounded_and_dispositioned(self) -> Self:
        _require_unique(self.route_reason_codes, "route reason codes")
        _require_unique(self.selected_role_ids, "selected role IDs")
        role_ids = tuple(item.role_id for item in self.private_role_records)
        _require_unique(role_ids, "private role IDs")
        if not set(role_ids).issubset(self.selected_role_ids):
            raise ValueError("private role records must resolve to selected roles")
        if len({item.private_pack_hash for item in self.private_role_records}) != len(
            self.private_role_records
        ):
            raise ValueError("private pack hashes must not leak across roles")
        executable = bool(self.final_ordered_proposal_ids)
        if executable:
            if set(role_ids) != set(self.selected_role_ids):
                raise ValueError("executable council requires every selected role record")
            passing_proposers = sum(
                item.role_kind == "PROPOSER" and item.status == "PASS"
                for item in self.private_role_records
            )
            if passing_proposers < 2:
                raise ValueError(
                    "executable council requires two passing independent proposers"
                )
            passing_chairs = sum(
                item.role_kind == "CHAIR" and item.status == "PASS"
                for item in self.private_role_records
            )
            if passing_chairs != 1:
                raise ValueError("executable council requires exactly one passing chair")

        critic_classes = {item.critic_class for item in self.critique_reports}
        critic_status_by_role = {
            item.role_id: item.status
            for item in self.private_role_records
            if item.role_kind == "CRITIC"
        }
        mandatory_critics_passed = (
            critic_status_by_role.get("formal_critic") == "PASS"
            and critic_status_by_role.get("ppa_critic") == "PASS"
        )
        if executable and (
            critic_classes != {"FORMAL", "PPA"} or not mandatory_critics_passed
        ):
            raise ValueError("executable council requires both mandatory critics to pass")
        card_ids = tuple(item.proposal_card_id for item in self.proposal_cards)
        proposal_ids = tuple(item.proposal_id for item in self.proposal_cards)
        _require_unique(card_ids, "proposal card IDs")
        _require_unique(proposal_ids, "proposal card proposal IDs")
        if any(item.proposal_card_id not in set(card_ids) for item in self.critique_reports):
            raise ValueError("critique must resolve to a neutral proposal card")

        objections = tuple(
            objection.objection_id
            for report in self.critique_reports
            for objection in report.objections
        )
        _require_unique(objections, "council objection IDs")
        dispositioned = tuple(item.objection_id for item in self.critique_dispositions)
        _require_unique(dispositioned, "critique disposition objection IDs")
        if self.final_ordered_proposal_ids and set(dispositioned) != set(objections):
            raise ValueError("every critique objection requires exactly one disposition")
        if not set(dispositioned).issubset(objections):
            raise ValueError("critique disposition must resolve to an objection")

        _require_unique(self.final_ordered_proposal_ids, "final proposal IDs")
        if not set(self.final_ordered_proposal_ids).issubset(proposal_ids):
            raise ValueError("final proposals must resolve through neutral proposal cards")
        if self.deadline_outcome != "MET" and self.final_ordered_proposal_ids:
            raise ValueError("deadline failure cannot emit executable final proposals")
        if executable and self.trace_completeness_percent != 100.0:
            raise ValueError("met council deadline requires a complete trace")

        if tuple(self.final_ordered_proposal_ids) != tuple(
            self.proposal_shortlist.ordered_proposal_ids
        ):
            raise ValueError("CouncilResult proposals must match its ProposalShortlist")
        normal_statuses = {
            "PASS",
            "NO_SAFE_PROPOSAL",
            "INSUFFICIENT_EVIDENCE",
            "PARTIAL",
        }
        if self.status in normal_statuses and self.status != self.proposal_shortlist.status:
            raise ValueError("CouncilResult status must match its ProposalShortlist")
        if self.status == "PASS" and not executable:
            raise ValueError("PASS council result requires an executable shortlist")
        if self.status in {
            "NO_SAFE_PROPOSAL",
            "INSUFFICIENT_EVIDENCE",
            "PLANNER_ERROR",
            "CANCELLED",
            "BUDGET_EXHAUSTED",
        } and executable:
            raise ValueError(f"{self.status} council result cannot emit proposals")
        if self.status == "CANCELLED" and self.deadline_outcome != "CANCELLED":
            raise ValueError("CANCELLED council status requires cancelled deadline outcome")
        if self.deadline_outcome == "CANCELLED" and self.status != "CANCELLED":
            raise ValueError("cancelled deadline requires CANCELLED council status")
        if self.deadline_outcome == "EXCEEDED" and self.status != "BUDGET_EXHAUSTED":
            raise ValueError("exceeded deadline requires BUDGET_EXHAUSTED council status")

        expected_tokens = sum(
            item.input_tokens + item.output_tokens for item in self.private_role_records
        )
        expected_latency = sum(item.latency_ms for item in self.private_role_records)
        if self.total_tokens != expected_tokens:
            raise ValueError("total_tokens must equal private role token consumption")
        if self.total_latency_ms != expected_latency:
            raise ValueError("total_latency_ms must equal private role latency")

        refs = tuple(
            ref
            for report in self.critique_reports
            for objection in report.objections
            for ref in objection.evidence_refs
        ) + tuple(
            ref
            for disposition in self.critique_dispositions
            for ref in disposition.chair_evidence_refs
        )
        if refs and _require_one_snapshot(refs, "council") != self.snapshot_hash:
            raise ValueError("council evidence must match result snapshot_hash")
        return self


__all__ = [
    "ContextRequest",
    "CouncilRequest",
    "CouncilPolicy",
    "CouncilRoute",
    "CouncilResult",
    "CouncilTrace",
    "CritiqueDisposition",
    "CritiqueReport",
    "DiagnosisReport",
    "M6GateEvidence",
    "M6SignoffReport",
    "PlannerRequest",
    "PlannerResult",
    "ProposalShortlist",
    "ProviderResult",
    "RoleContextPack",
    "TransformRecommendation",
]
