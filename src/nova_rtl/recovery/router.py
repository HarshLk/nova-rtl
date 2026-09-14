"""Budgeted deterministic recovery routing with monotonic escalation."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from pydantic import Field

from nova_rtl.contracts.base import NonNegativeInt, StrictContract, canonical_sha256
from nova_rtl.contracts.recovery import (
    TERMINAL_ACTIONS,
    FailureEvent,
    RecoveryAction,
    RecoveryAdvice,
    RecoveryDecision,
    RecoveryRoutePlan,
    RepairDirective,
)
from nova_rtl.recovery.policy import RecoveryPolicyRegistry


class RecoveryRoutingError(ValueError):
    pass


class RecoveryBudgets(StrictContract):
    remaining_family_budget: NonNegativeInt
    remaining_lineage_budget: NonNegativeInt
    remaining_token_budget: NonNegativeInt
    remaining_latency_budget_ms: NonNegativeInt
    deadline: datetime


class RecoveryHistory(StrictContract):
    recovery_depth: NonNegativeInt = 0
    previous_actions: tuple[RecoveryAction, ...] = ()
    current_transform_family: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_]{2,127}$"
    )


DEFAULT_ACTION: dict[str, RecoveryAction] = {
    "BASELINE_INPUT_ERROR": "STOP_RUN_OR_REQUEST_HUMAN",
    "ANALYSIS_VIEW_OR_ACTIVITY_MISMATCH": "STOP_RUN_OR_REQUEST_HUMAN",
    "INFRASTRUCTURE_TRANSIENT": "RETRY_INFRASTRUCTURE",
    "ADAPTER_OR_PARSER_ERROR": "CORRECT_EXECUTOR_OUTPUT",
    "CONSTRAINT_BINDING_DELTA": "REJECT_CANDIDATE",
    "CDC_INVARIANT_DELTA": "REJECT_CANDIDATE",
    "FORMAL_MODEL_MISMATCH": "STOP_RUN_OR_REQUEST_HUMAN",
    "INVALID_PROPOSAL_SCHEMA": "REPAIR_SCHEMA",
    "UNKNOWN_OR_INAPPLICABLE_TRANSFORM": "REJECT_CANDIDATE",
    "PROTECTED_STRUCTURE_VIOLATION": "REJECT_CANDIDATE",
    "RTL_PARSE_OR_ELAB_FAILURE": "NARROW_TRANSFORM_SCOPE",
    "FAST_SYNTH_STRUCTURAL_FAILURE": "NARROW_TRANSFORM_SCOPE",
    "FORMAL_SEMANTIC_FAILURE": "NARROW_TRANSFORM_SCOPE",
    "FORMAL_INCONCLUSIVE": "REPARTITION_FORMAL_PROOF",
    "TIMING_NO_GAIN": "LOCAL_PARAMETER_REVISION",
    "TIMING_REGRESSION": "SWITCH_TRANSFORM_FAMILY",
    "CRITICAL_PATH_MIGRATION": "OPPORTUNITY_REANALYSIS",
    "HOLD_REGRESSION": "LOCAL_PARAMETER_REVISION",
    "AREA_POLICY_VIOLATION": "LOCAL_PARAMETER_REVISION",
    "POWER_POLICY_VIOLATION": "LOCAL_PARAMETER_REVISION",
    "PHYSICAL_CORRELATION_MISS": "PHYSICAL_ONLY_RECOMMENDATION",
    "CONGESTION_OR_ROUTABILITY_RISK": "LOCAL_PARAMETER_REVISION",
    "REPEATED_NON_PROGRESS": "TARGET_NEW_PATH_CLUSTER",
    "VALID_BUT_DOMINATED": "ABANDON_LINEAGE",
}

ESCALATION_LEVEL: dict[RecoveryAction, int] = {
    "RETRY_INFRASTRUCTURE": 0,
    "REPAIR_SCHEMA": 1,
    "CORRECT_EXECUTOR_OUTPUT": 1,
    "LOCAL_PARAMETER_REVISION": 2,
    "NARROW_TRANSFORM_SCOPE": 2,
    "SWITCH_OPERATION_SAME_FAMILY": 3,
    "SWITCH_TRANSFORM_FAMILY": 3,
    "REPARTITION_FORMAL_PROOF": 3,
    "OPPORTUNITY_REANALYSIS": 5,
    "TARGET_NEW_PATH_CLUSTER": 5,
    "BRANCH_FROM_ALTERNATE_PARENT": 5,
    "PHYSICAL_ONLY_RECOMMENDATION": 6,
    "REJECT_CANDIDATE": 6,
    "ABANDON_LINEAGE": 6,
    "STOP_RUN_OR_REQUEST_HUMAN": 6,
}


def _id(prefix: str, payload: object) -> str:
    digest = sha256(str(payload).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _terminal_action(allowed: tuple[RecoveryAction, ...]) -> RecoveryAction:
    for action in ("ABANDON_LINEAGE", "REJECT_CANDIDATE", "STOP_RUN_OR_REQUEST_HUMAN"):
        if action in allowed:
            return action  # type: ignore[return-value]
    raise RecoveryRoutingError("exhausted recovery policy has no terminal action")


def _select_action(
    failure: FailureEvent,
    history: RecoveryHistory,
    budgets: RecoveryBudgets,
    policy: RecoveryPolicyRegistry,
) -> RecoveryAction:
    allowed = policy.rules[failure.failure_family].allowed_actions
    if (
        budgets.remaining_lineage_budget == 0
        or history.recovery_depth >= policy.max_recovery_depth
    ):
        return _terminal_action(allowed)
    selected = DEFAULT_ACTION[failure.failure_family]
    previous_level = max((ESCALATION_LEVEL[item] for item in history.previous_actions), default=-1)
    if ESCALATION_LEVEL[selected] < previous_level:
        candidates = [item for item in allowed if ESCALATION_LEVEL[item] >= previous_level]
        selected = candidates[0] if candidates else _terminal_action(allowed)
    return selected


def _plan(
    failure: FailureEvent,
    directive: RepairDirective,
    history: RecoveryHistory,
    budgets: RecoveryBudgets,
    policy: RecoveryPolicyRegistry,
) -> tuple[RecoveryRoutePlan, RecoveryAction, str | None, tuple[str, ...]]:
    action = _select_action(failure, history, budgets, policy)
    parent = None
    if action not in TERMINAL_ACTIONS:
        parent = (
            failure.candidate_id
            if action == "RETRY_INFRASTRUCTURE"
            else failure.parent_candidate_id
        )
        parent = parent or failure.candidate_id
        if parent is None:
            raise RecoveryRoutingError("nonterminal recovery has no authorized parent")
    excluded = (
        (history.current_transform_family,)
        if failure.failure_family == "REPEATED_NON_PROGRESS"
        and history.current_transform_family is not None
        else ()
    )
    plan_payload = {
        "schema_version": 1,
        "recovery_route_plan_id": _id(
            "route_plan", (failure.failure_event_id, directive.repair_directive_id, action)
        ),
        "failure_event_id": failure.failure_event_id,
        "repair_directive_id": directive.repair_directive_id,
        "allowed_actions": policy.rules[failure.failure_family].allowed_actions,
        "eligible_roles": directive.recommended_roles,
        "excluded_transform_families": excluded,
        "next_parent_candidate_ids": () if parent is None else (parent,),
        "remaining_family_budget": budgets.remaining_family_budget,
        "remaining_lineage_budget": budgets.remaining_lineage_budget,
        "remaining_token_budget": budgets.remaining_token_budget,
        "remaining_latency_budget_ms": budgets.remaining_latency_budget_ms,
        "policy_hash": policy.policy_hash,
    }
    plan = RecoveryRoutePlan(**plan_payload, route_plan_hash=canonical_sha256(plan_payload))
    return plan, action, parent, excluded


def route_recovery(
    failure: FailureEvent,
    directive: RepairDirective,
    history: RecoveryHistory,
    budgets: RecoveryBudgets,
    advice: RecoveryAdvice | None = None,
    *,
    policy: RecoveryPolicyRegistry,
) -> RecoveryDecision:
    """Resolve a bounded action; advisory reasoning has no authority in M7."""

    plan, action, parent, excluded = _plan(failure, directive, history, budgets, policy)
    if advice is not None:
        if advice.proposed_action not in plan.allowed_actions:
            raise RecoveryRoutingError("recovery advice action is outside the policy envelope")
        raise RecoveryRoutingError("M7 deterministic routing cannot consume recovery advice")
    request_payload = {
        "failure_event": failure.failure_event_id,
        "directive": directive.repair_directive_id,
        "route_plan_hash": plan.route_plan_hash,
        "deadline": budgets.deadline.isoformat(),
    }
    request_hash = canonical_sha256(request_payload)
    request_id = _id("recovery_request", request_hash)
    decrement_lineage = action not in TERMINAL_ACTIONS and action != "RETRY_INFRASTRUCTURE"
    decrement_family = action not in TERMINAL_ACTIONS
    decision_payload = {
        "schema_version": 1,
        "recovery_decision_id": _id("recovery_decision", (request_hash, action)),
        "failure_event_id": failure.failure_event_id,
        "recovery_request_id": request_id,
        "recovery_request_hash": request_hash,
        "recovery_route_plan_id": plan.recovery_route_plan_id,
        "recovery_route_plan_hash": plan.route_plan_hash,
        "action": action,
        "next_parent_candidate_id": parent,
        "invoke_reasoning": False,
        "selected_roles": (),
        "excluded_transform_families": excluded,
        "remaining_family_budget": max(
            0, budgets.remaining_family_budget - int(decrement_family)
        ),
        "remaining_lineage_budget": max(
            0, budgets.remaining_lineage_budget - int(decrement_lineage)
        ),
        "reason_codes": (f"ROUTED_{failure.failure_family}",),
        "policy_version": policy.policy_version,
        "recovery_advice_id": None,
    }
    return RecoveryDecision(
        **decision_payload, decision_hash=canonical_sha256(decision_payload)
    )


__all__ = [
    "RecoveryBudgets",
    "RecoveryHistory",
    "RecoveryRoutingError",
    "route_recovery",
]
