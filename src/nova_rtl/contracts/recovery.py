"""Deterministic failure classification and bounded recovery contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    EvidenceRef,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
    canonical_sha256,
)
from nova_rtl.contracts.execution import Stage
from nova_rtl.contracts.optimization import Fingerprint

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$"),
]
MachineToken = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,127}$"),
]
MetricDeltaName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$"),
]
Probability = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
FailureFamily = Literal[
    "BASELINE_INPUT_ERROR",
    "ANALYSIS_VIEW_OR_ACTIVITY_MISMATCH",
    "INFRASTRUCTURE_TRANSIENT",
    "ADAPTER_OR_PARSER_ERROR",
    "CONSTRAINT_BINDING_DELTA",
    "CDC_INVARIANT_DELTA",
    "FORMAL_MODEL_MISMATCH",
    "INVALID_PROPOSAL_SCHEMA",
    "UNKNOWN_OR_INAPPLICABLE_TRANSFORM",
    "PROTECTED_STRUCTURE_VIOLATION",
    "RTL_PARSE_OR_ELAB_FAILURE",
    "FAST_SYNTH_STRUCTURAL_FAILURE",
    "FORMAL_SEMANTIC_FAILURE",
    "FORMAL_INCONCLUSIVE",
    "TIMING_NO_GAIN",
    "TIMING_REGRESSION",
    "CRITICAL_PATH_MIGRATION",
    "HOLD_REGRESSION",
    "AREA_POLICY_VIOLATION",
    "POWER_POLICY_VIOLATION",
    "PHYSICAL_CORRELATION_MISS",
    "CONGESTION_OR_ROUTABILITY_RISK",
    "REPEATED_NON_PROGRESS",
    "VALID_BUT_DOMINATED",
]
Repairability = Literal[
    "NO_RETRY",
    "DETERMINISTIC_RETRY",
    "LOCAL_REVISION",
    "FAMILY_SWITCH",
    "OPPORTUNITY_REANALYSIS",
    "PARENT_BRANCH",
    "HUMAN_REVIEW",
]
RecoveryAction = Literal[
    "RETRY_INFRASTRUCTURE",
    "REPAIR_SCHEMA",
    "CORRECT_EXECUTOR_OUTPUT",
    "LOCAL_PARAMETER_REVISION",
    "NARROW_TRANSFORM_SCOPE",
    "SWITCH_OPERATION_SAME_FAMILY",
    "SWITCH_TRANSFORM_FAMILY",
    "OPPORTUNITY_REANALYSIS",
    "TARGET_NEW_PATH_CLUSTER",
    "BRANCH_FROM_ALTERNATE_PARENT",
    "REPARTITION_FORMAL_PROOF",
    "PHYSICAL_ONLY_RECOMMENDATION",
    "REJECT_CANDIDATE",
    "ABANDON_LINEAGE",
    "STOP_RUN_OR_REQUEST_HUMAN",
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
TERMINAL_ACTIONS = frozenset(
    {
        "PHYSICAL_ONLY_RECOMMENDATION",
        "REJECT_CANDIDATE",
        "ABANDON_LINEAGE",
        "STOP_RUN_OR_REQUEST_HUMAN",
    }
)
PROTECTED_FAILURE_ACTIONS = frozenset(
    {"REJECT_CANDIDATE", "STOP_RUN_OR_REQUEST_HUMAN"}
)
INFRASTRUCTURE_FAILURE_ACTIONS = frozenset(
    {"RETRY_INFRASTRUCTURE", "STOP_RUN_OR_REQUEST_HUMAN"}
)


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _validate_evidence(refs: tuple[EvidenceRef, ...], label: str) -> None:
    _require_unique(tuple(item.evidence_id for item in refs), f"{label} evidence IDs")
    if len({item.snapshot_hash for item in refs}) != 1:
        raise ValueError(f"{label} evidence must resolve in one snapshot")


class FailureEvent(StrictContract):
    """Authoritative deterministic bridge from evaluation to recovery."""

    schema_version: Literal[1] = 1
    failure_event_id: EntityId
    run_id: EntityId
    subject_type: Literal[
        "RUN",
        "BASELINE",
        "PLANNER",
        "PROPOSAL",
        "CANDIDATE",
        "OPPORTUNITY",
        "INFRASTRUCTURE",
    ]
    subject_id: EntityId
    candidate_id: EntityId | None
    proposal_id: EntityId | None
    parent_candidate_id: EntityId | None
    opportunity_id: EntityId | None
    failed_stage: Stage | Literal["PLANNER_PROVIDER"]
    analysis_view_id: EntityId | None
    failure_family: FailureFamily
    failure_scope: Literal[
        "RUN", "BASELINE", "PLANNER", "PROPOSAL", "CANDIDATE", "OPPORTUNITY", "INFRASTRUCTURE"
    ]
    repairability: Repairability
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    retryable: bool
    constraint_hash_verified: bool
    analysis_view_hash_verified: bool
    constraint_binding_status: Literal[
        "EQUIVALENT",
        "APPROVED_SEMANTIC_REMAP",
        "FORBIDDEN_DELTA",
        "NOT_CHECKED",
    ]
    protected_structure_status: Literal["UNCHANGED", "CHANGED", "UNKNOWN"]
    metric_delta: dict[MetricDeltaName, FiniteFloat]
    primary_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    raw_stage_result_ref: EntityId | None
    provider_result_ref: EntityId | None = None
    classifier_version: NonEmptyString

    @model_validator(mode="after")
    def subject_view_and_retry_are_coherent(self) -> Self:
        entity_field = {
            "CANDIDATE": self.candidate_id,
            "PROPOSAL": self.proposal_id,
            "OPPORTUNITY": self.opportunity_id,
            "BASELINE": self.candidate_id,
        }.get(self.subject_type)
        if self.subject_type in {"CANDIDATE", "PROPOSAL", "OPPORTUNITY", "BASELINE"}:
            if entity_field is None:
                required = {
                    "CANDIDATE": "candidate_id",
                    "PROPOSAL": "proposal_id",
                    "OPPORTUNITY": "opportunity_id",
                    "BASELINE": "candidate_id",
                }[self.subject_type]
                raise ValueError(f"{self.subject_type} failure requires {required}")
            if entity_field != self.subject_id:
                raise ValueError("subject_id must match its entity-specific ID")
        if self.failed_stage in VIEW_STAGES and self.analysis_view_id is None:
            raise ValueError("view-specific failure requires analysis_view_id")
        if self.failed_stage not in VIEW_STAGES and self.analysis_view_id is not None:
            raise ValueError("view-independent failure cannot declare analysis_view_id")
        if self.repairability == "NO_RETRY" and self.retryable:
            raise ValueError("NO_RETRY failure cannot be retryable")
        if self.repairability == "HUMAN_REVIEW" and self.retryable:
            raise ValueError("HUMAN_REVIEW failure cannot be automatically retryable")
        if self.failed_stage == "PLANNER_PROVIDER":
            if self.subject_type != "PLANNER" or self.provider_result_ref is None:
                raise ValueError(
                    "PLANNER_PROVIDER failure requires PLANNER subject and provider_result_ref"
                )
            if self.raw_stage_result_ref is not None:
                raise ValueError("PLANNER_PROVIDER failure cannot fabricate a stage result")
        else:
            if self.raw_stage_result_ref is None:
                raise ValueError("EDA failure requires raw_stage_result_ref")
            if self.provider_result_ref is not None:
                raise ValueError("EDA failure cannot declare provider_result_ref")
        _validate_evidence(self.primary_evidence_refs, "failure")
        return self


class RepairDirective(StrictContract):
    schema_version: Literal[1] = 1
    repair_directive_id: EntityId
    failure_event_id: EntityId
    allowed_scope: StableUpperString
    observed: tuple[NonEmptyString, ...] = Field(min_length=1)
    preserve: tuple[MachineToken, ...] = Field(min_length=1)
    prohibit: tuple[MachineToken, ...] = Field(min_length=1)
    recommended_actions: tuple[NonEmptyString, ...] = Field(min_length=1)
    recommended_roles: tuple[EntityId, ...]
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    compiler_rule_refs: tuple[StableUpperString, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def directive_is_machine_checkable(self) -> Self:
        for values, label in (
            (self.observed, "observations"),
            (self.preserve, "preserve tokens"),
            (self.prohibit, "prohibit tokens"),
            (self.recommended_actions, "recommended actions"),
            (self.recommended_roles, "recommended roles"),
            (self.compiler_rule_refs, "compiler rule refs"),
        ):
            _require_unique(values, label)
        if set(self.preserve) & set(self.prohibit):
            raise ValueError("preserve and prohibit tokens must be disjoint")
        _validate_evidence(self.evidence_refs, "repair directive")
        return self


class CandidateFailureFingerprint(StrictContract):
    schema_version: Literal[1] = 1
    candidate_failure_fingerprint_id: EntityId
    candidate_id: EntityId
    target_cone_fingerprint: Fingerprint
    operation_family: StableUpperString
    operation: StableUpperString
    parameter_fingerprint: Fingerprint
    ast_diff_fingerprint: Fingerprint
    mapped_delta_fingerprint: Fingerprint
    ancestor_lineage: tuple[EntityId, ...] = Field(min_length=1)
    failure_family: FailureFamily
    formal_counterexample_fingerprint: Fingerprint | None
    metric_response_class: StableUpperString

    @model_validator(mode="after")
    def lineage_is_ancestral(self) -> Self:
        _require_unique(self.ancestor_lineage, "ancestor lineage")
        if self.candidate_id in self.ancestor_lineage:
            raise ValueError("candidate cannot appear in its ancestor lineage")
        return self


class RecoveryRoutePlan(StrictContract):
    schema_version: Literal[1] = 1
    recovery_route_plan_id: EntityId
    failure_event_id: EntityId
    repair_directive_id: EntityId
    allowed_actions: tuple[RecoveryAction, ...] = Field(min_length=1)
    eligible_roles: tuple[EntityId, ...]
    excluded_transform_families: tuple[StableUpperString, ...]
    next_parent_candidate_ids: tuple[EntityId, ...]
    remaining_family_budget: NonNegativeInt
    remaining_lineage_budget: NonNegativeInt
    remaining_token_budget: NonNegativeInt
    remaining_latency_budget_ms: NonNegativeInt
    policy_hash: HashRef
    route_plan_hash: HashRef

    @model_validator(mode="after")
    def route_is_unique_and_self_hashed(self) -> Self:
        for values, label in (
            (self.allowed_actions, "allowed actions"),
            (self.eligible_roles, "eligible roles"),
            (self.excluded_transform_families, "excluded transform families"),
            (self.next_parent_candidate_ids, "next parent candidate IDs"),
        ):
            _require_unique(values, label)
        expected = canonical_sha256(self, exclude=frozenset({"route_plan_hash"}))
        if self.route_plan_hash != expected:
            raise ValueError("route_plan_hash does not match canonical route plan")
        return self


class RecoveryRequest(StrictContract):
    schema_version: Literal[1] = 1
    recovery_request_id: EntityId
    failure_event_id: EntityId
    failure_event_hash: HashRef
    failure_family: FailureFamily
    repair_directive_id: EntityId
    repair_directive_hash: HashRef
    recovery_route_plan_id: EntityId
    recovery_route_plan_hash: HashRef
    route_plan: RecoveryRoutePlan
    candidate_lineage_ids: tuple[EntityId, ...] = Field(min_length=1)
    candidate_failure_fingerprint_ids: tuple[EntityId, ...]
    allowed_actions: tuple[RecoveryAction, ...] = Field(min_length=1)
    excluded_transform_families: tuple[StableUpperString, ...]
    eligible_roles: tuple[EntityId, ...]
    authorized_evidence_ids: tuple[EntityId, ...] = Field(min_length=1)
    remaining_family_budget: NonNegativeInt
    remaining_lineage_budget: NonNegativeInt
    remaining_token_budget: NonNegativeInt
    remaining_latency_budget_ms: NonNegativeInt
    policy_hash: HashRef
    deadline: UtcDatetime
    request_hash: HashRef

    @model_validator(mode="after")
    def request_envelope_is_deterministic(self) -> Self:
        for values, label in (
            (self.candidate_lineage_ids, "candidate lineage IDs"),
            (self.candidate_failure_fingerprint_ids, "candidate failure fingerprint IDs"),
            (self.allowed_actions, "allowed actions"),
            (self.excluded_transform_families, "excluded transform families"),
            (self.eligible_roles, "eligible roles"),
            (self.authorized_evidence_ids, "authorized evidence IDs"),
        ):
            _require_unique(values, label)
        plan = self.route_plan
        if (
            self.recovery_route_plan_id != plan.recovery_route_plan_id
            or self.recovery_route_plan_hash != plan.route_plan_hash
        ):
            raise ValueError("RecoveryRequest must bind the exact route plan")
        if (
            self.failure_event_id != plan.failure_event_id
            or self.repair_directive_id != plan.repair_directive_id
        ):
            raise ValueError("RecoveryRequest lineage must match the route plan")
        if self.policy_hash != plan.policy_hash:
            raise ValueError("RecoveryRequest policy hash must match the route plan")
        if not set(self.allowed_actions).issubset(plan.allowed_actions):
            raise ValueError("RecoveryRequest allowed actions exceed the route plan")
        if not set(self.eligible_roles).issubset(plan.eligible_roles):
            raise ValueError("RecoveryRequest eligible roles exceed the route plan")
        if not set(self.excluded_transform_families).issuperset(
            plan.excluded_transform_families
        ):
            raise ValueError("RecoveryRequest exclusions cannot weaken the route plan")
        if not set(plan.next_parent_candidate_ids).issubset(self.candidate_lineage_ids):
            raise ValueError("route-plan parents must resolve in request candidate lineage")
        if self.failure_family == "PROTECTED_STRUCTURE_VIOLATION" and not set(
            self.allowed_actions
        ).issubset(PROTECTED_FAILURE_ACTIONS):
            raise ValueError(
                "protected-structure failure cannot authorize nonterminal recovery"
            )
        if self.failure_family == "INFRASTRUCTURE_TRANSIENT" and not set(
            self.allowed_actions
        ).issubset(INFRASTRUCTURE_FAILURE_ACTIONS):
            raise ValueError("infrastructure failure cannot authorize RTL repair")
        request_budgets = (
            self.remaining_family_budget,
            self.remaining_lineage_budget,
            self.remaining_token_budget,
            self.remaining_latency_budget_ms,
        )
        plan_budgets = (
            plan.remaining_family_budget,
            plan.remaining_lineage_budget,
            plan.remaining_token_budget,
            plan.remaining_latency_budget_ms,
        )
        if any(
            request > route
            for request, route in zip(request_budgets, plan_budgets, strict=True)
        ):
            raise ValueError("RecoveryRequest budget cannot exceed the route plan")
        expected = canonical_sha256(self, exclude=frozenset({"request_hash"}))
        if self.request_hash != expected:
            raise ValueError("request_hash does not match canonical RecoveryRequest")
        return self


class RecoveryAdvice(StrictContract):
    """Advisory recovery output with no budget or classification authority."""

    schema_version: Literal[1] = 1
    recovery_advice_id: EntityId
    failure_event_id: EntityId
    repair_directive_id: EntityId
    recovery_request_id: EntityId
    recovery_request_hash: HashRef
    recovery_route_plan_id: EntityId
    recovery_route_plan_hash: HashRef
    advisor_mode: Literal["HEURISTIC", "SINGLE_AGENT", "AGENT_COUNCIL"]
    selected_role: EntityId | None
    proposed_action: RecoveryAction
    proposed_parent_candidate_id: EntityId | None
    proposed_excluded_transform_families: tuple[StableUpperString, ...]
    rationale_codes: tuple[StableUpperString, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    uncertainty: Probability
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    output_hash: HashRef

    @model_validator(mode="after")
    def advice_is_coherent_and_self_hashed(self) -> Self:
        _require_unique(
            self.proposed_excluded_transform_families, "proposed excluded families"
        )
        _require_unique(self.rationale_codes, "rationale codes")
        _validate_evidence(self.evidence_refs, "recovery advice")
        if self.advisor_mode == "HEURISTIC" and self.selected_role is not None:
            raise ValueError("heuristic recovery advice cannot select a reasoning role")
        if self.advisor_mode != "HEURISTIC" and self.selected_role is None:
            raise ValueError("reasoning recovery advice requires a selected role")
        if self.proposed_action in TERMINAL_ACTIONS:
            if self.proposed_parent_candidate_id is not None:
                raise ValueError("terminal recovery advice cannot propose a next parent")
        elif self.proposed_parent_candidate_id is None:
            raise ValueError("nonterminal recovery advice requires a proposed parent")
        expected = canonical_sha256(self, exclude=frozenset({"output_hash"}))
        if self.output_hash != expected:
            raise ValueError("output_hash does not match canonical recovery advice")
        return self


class RecoveryDecision(StrictContract):
    """Authoritative deterministic recovery decision."""

    schema_version: Literal[1] = 1
    recovery_decision_id: EntityId
    failure_event_id: EntityId
    recovery_request_id: EntityId
    recovery_request_hash: HashRef
    recovery_route_plan_id: EntityId
    recovery_route_plan_hash: HashRef
    action: RecoveryAction
    next_parent_candidate_id: EntityId | None
    invoke_reasoning: bool
    selected_roles: tuple[EntityId, ...]
    excluded_transform_families: tuple[StableUpperString, ...]
    remaining_family_budget: NonNegativeInt
    remaining_lineage_budget: NonNegativeInt
    reason_codes: tuple[StableUpperString, ...] = Field(min_length=1)
    policy_version: NonEmptyString
    recovery_advice_id: EntityId | None
    decision_hash: HashRef

    @model_validator(mode="after")
    def decision_is_terminal_or_routed_and_self_hashed(self) -> Self:
        _require_unique(self.selected_roles, "selected roles")
        _require_unique(self.excluded_transform_families, "excluded transform families")
        _require_unique(self.reason_codes, "reason codes")
        if self.action in TERMINAL_ACTIONS:
            if self.next_parent_candidate_id is not None:
                raise ValueError("terminal recovery decision cannot have a next parent")
        elif self.next_parent_candidate_id is None:
            raise ValueError("nonterminal recovery decision requires a next parent")
        if self.invoke_reasoning:
            if not self.selected_roles or self.recovery_advice_id is None:
                raise ValueError("reasoning decision requires roles and recovery advice")
        elif self.selected_roles or self.recovery_advice_id is not None:
            raise ValueError("deterministic-only decision cannot reference reasoning output")
        expected = canonical_sha256(self, exclude=frozenset({"decision_hash"}))
        if self.decision_hash != expected:
            raise ValueError("decision_hash does not match canonical recovery decision")
        return self


def validate_recovery_authority_chain(
    *,
    failure_event: FailureEvent,
    repair_directive: RepairDirective,
    request: RecoveryRequest,
    advice: RecoveryAdvice | None = None,
    decision: RecoveryDecision | None = None,
) -> None:
    """Resolve recovery references and reject any authority-envelope expansion."""

    plan = request.route_plan
    if request.failure_event_id != failure_event.failure_event_id:
        raise ValueError("RecoveryRequest failure reference does not resolve")
    if request.failure_event_hash != canonical_sha256(failure_event):
        raise ValueError("RecoveryRequest failure hash does not resolve")
    if request.failure_family != failure_event.failure_family:
        raise ValueError("RecoveryRequest failure family does not resolve")
    if request.repair_directive_id != repair_directive.repair_directive_id:
        raise ValueError("RecoveryRequest repair directive reference does not resolve")
    if request.repair_directive_hash != canonical_sha256(repair_directive):
        raise ValueError("RecoveryRequest repair directive hash does not resolve")
    if repair_directive.failure_event_id != failure_event.failure_event_id:
        raise ValueError("RepairDirective does not belong to the failure event")

    if advice is not None:
        if (
            advice.failure_event_id != failure_event.failure_event_id
            or advice.repair_directive_id != repair_directive.repair_directive_id
            or advice.recovery_request_id != request.recovery_request_id
            or advice.recovery_request_hash != request.request_hash
            or advice.recovery_route_plan_id != plan.recovery_route_plan_id
            or advice.recovery_route_plan_hash != plan.route_plan_hash
        ):
            raise ValueError("RecoveryAdvice authority references do not resolve")
        if advice.proposed_action not in request.allowed_actions:
            raise ValueError("RecoveryAdvice proposed action is outside the allowed action set")
        if (
            advice.selected_role is not None
            and advice.selected_role not in request.eligible_roles
        ):
            raise ValueError("RecoveryAdvice selected role is not an eligible role")
        if (
            advice.proposed_parent_candidate_id is not None
            and (
                advice.proposed_parent_candidate_id not in plan.next_parent_candidate_ids
                or advice.proposed_parent_candidate_id not in request.candidate_lineage_ids
            )
        ):
            raise ValueError("RecoveryAdvice parent is outside the route plan")
        if not set(advice.proposed_excluded_transform_families).issuperset(
            request.excluded_transform_families
        ):
            raise ValueError("RecoveryAdvice cannot weaken excluded transform families")
        if not {
            item.evidence_id for item in advice.evidence_refs
        }.issubset(request.authorized_evidence_ids):
            raise ValueError("RecoveryAdvice evidence exceeds request authority")
        if advice.input_tokens + advice.output_tokens > request.remaining_token_budget:
            raise ValueError("RecoveryAdvice token use exceeds request budget")
        if advice.latency_ms > request.remaining_latency_budget_ms:
            raise ValueError("RecoveryAdvice latency exceeds request budget")

    if decision is not None:
        if (
            decision.failure_event_id != failure_event.failure_event_id
            or decision.recovery_request_id != request.recovery_request_id
            or decision.recovery_request_hash != request.request_hash
            or decision.recovery_route_plan_id != plan.recovery_route_plan_id
            or decision.recovery_route_plan_hash != plan.route_plan_hash
        ):
            raise ValueError("RecoveryDecision authority references do not resolve")
        if decision.action not in request.allowed_actions:
            raise ValueError("RecoveryDecision action is outside the allowed action set")
        if (
            decision.next_parent_candidate_id is not None
            and (
                decision.next_parent_candidate_id not in plan.next_parent_candidate_ids
                or decision.next_parent_candidate_id not in request.candidate_lineage_ids
            )
        ):
            raise ValueError("RecoveryDecision parent is outside the route plan")
        if not set(decision.selected_roles).issubset(request.eligible_roles):
            raise ValueError("RecoveryDecision selected roles exceed eligible roles")
        if not set(decision.excluded_transform_families).issuperset(
            request.excluded_transform_families
        ):
            raise ValueError("RecoveryDecision cannot weaken excluded transform families")
        if (
            decision.remaining_family_budget > request.remaining_family_budget
            or decision.remaining_lineage_budget > request.remaining_lineage_budget
        ):
            raise ValueError("RecoveryDecision budget cannot increase")
        if (
            failure_event.failure_family == "PROTECTED_STRUCTURE_VIOLATION"
            and decision.action not in PROTECTED_FAILURE_ACTIONS
        ):
            raise ValueError(
                "PROTECTED_STRUCTURE_VIOLATION requires reject or stop action"
            )
        if (
            failure_event.failure_family == "INFRASTRUCTURE_TRANSIENT"
            and decision.action not in INFRASTRUCTURE_FAILURE_ACTIONS
        ):
            raise ValueError(
                "INFRASTRUCTURE_TRANSIENT requires deterministic retry or stop"
            )

        if decision.invoke_reasoning:
            if advice is None or decision.recovery_advice_id != advice.recovery_advice_id:
                raise ValueError("reasoning decision must resolve its RecoveryAdvice")
            if decision.action != advice.proposed_action:
                raise ValueError("reasoning decision action must match validated advice")
            if decision.next_parent_candidate_id != advice.proposed_parent_candidate_id:
                raise ValueError("reasoning decision parent must match validated advice")
            if (
                advice.selected_role is not None
                and advice.selected_role not in decision.selected_roles
            ):
                raise ValueError("reasoning decision must retain the advice role")
            if not set(decision.excluded_transform_families).issuperset(
                advice.proposed_excluded_transform_families
            ):
                raise ValueError("RecoveryDecision cannot weaken advice exclusions")
        elif advice is not None:
            raise ValueError("deterministic-only decision cannot consume RecoveryAdvice")


__all__ = [
    "CandidateFailureFingerprint",
    "FailureEvent",
    "RecoveryAdvice",
    "RecoveryDecision",
    "RecoveryRequest",
    "RecoveryRoutePlan",
    "RepairDirective",
    "validate_recovery_authority_chain",
]
