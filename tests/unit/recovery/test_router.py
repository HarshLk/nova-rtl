from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nova_rtl.contracts.recovery import FailureEvent, RecoveryAdvice, RepairDirective
from nova_rtl.recovery.policy import load_recovery_policy
from nova_rtl.recovery.router import (
    RecoveryBudgets,
    RecoveryHistory,
    RecoveryRoutingError,
    route_recovery,
)


def _policy():
    from pathlib import Path

    return load_recovery_policy(
        Path(__file__).resolve().parents[3] / "config/policy/recovery_rules.yaml"
    )


def _failure(family: str) -> FailureEvent:
    return FailureEvent.model_construct(
        failure_event_id=f"failure_{family.lower()}",
        failure_family=family,
        candidate_id="candidate_one",
        parent_candidate_id="baseline",
    )


def _directive(family: str) -> RepairDirective:
    return RepairDirective.model_construct(
        repair_directive_id=f"directive_{family.lower()}",
        failure_event_id=f"failure_{family.lower()}",
        recommended_roles=(),
    )


def _budgets(**overrides: object) -> RecoveryBudgets:
    values: dict[str, object] = {
        "remaining_family_budget": 2,
        "remaining_lineage_budget": 4,
        "remaining_token_budget": 0,
        "remaining_latency_budget_ms": 1000,
        "deadline": datetime(2026, 9, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return RecoveryBudgets.model_validate(values)


@pytest.mark.parametrize(
    ("family", "action"),
    [
        ("CONSTRAINT_BINDING_DELTA", "REJECT_CANDIDATE"),
        ("CDC_INVARIANT_DELTA", "REJECT_CANDIDATE"),
        ("INFRASTRUCTURE_TRANSIENT", "RETRY_INFRASTRUCTURE"),
        ("CRITICAL_PATH_MIGRATION", "OPPORTUNITY_REANALYSIS"),
        ("AREA_POLICY_VIOLATION", "LOCAL_PARAMETER_REVISION"),
    ],
)
def test_failure_family_owns_allowed_action(family: str, action: str) -> None:
    decision = route_recovery(
        _failure(family),
        _directive(family),
        RecoveryHistory(),
        _budgets(),
        policy=_policy(),
    )

    assert decision.action == action
    assert not decision.invoke_reasoning


def test_stagnation_forces_new_target_and_excludes_current_family() -> None:
    decision = route_recovery(
        _failure("REPEATED_NON_PROGRESS"),
        _directive("REPEATED_NON_PROGRESS"),
        RecoveryHistory(
            recovery_depth=2,
            previous_actions=("LOCAL_PARAMETER_REVISION", "SWITCH_TRANSFORM_FAMILY"),
            current_transform_family="LOGIC_RESTRUCTURE",
        ),
        _budgets(),
        policy=_policy(),
    )

    assert decision.action == "TARGET_NEW_PATH_CLUSTER"
    assert decision.excluded_transform_families == ("LOGIC_RESTRUCTURE",)


def test_exhausted_lineage_budget_abandons_without_parent() -> None:
    decision = route_recovery(
        _failure("TIMING_NO_GAIN"),
        _directive("TIMING_NO_GAIN"),
        RecoveryHistory(recovery_depth=4),
        _budgets(remaining_lineage_budget=0),
        policy=_policy(),
    )

    assert decision.action == "ABANDON_LINEAGE"
    assert decision.next_parent_candidate_id is None


def test_advice_cannot_escape_deterministic_action_envelope() -> None:
    advice = RecoveryAdvice.model_construct(
        proposed_action="LOCAL_PARAMETER_REVISION",
        proposed_parent_candidate_id="candidate_one",
    )
    with pytest.raises(RecoveryRoutingError, match="outside"):
        route_recovery(
            _failure("INFRASTRUCTURE_TRANSIENT"),
            _directive("INFRASTRUCTURE_TRANSIENT"),
            RecoveryHistory(),
            _budgets(),
            advice=advice,
            policy=_policy(),
        )
