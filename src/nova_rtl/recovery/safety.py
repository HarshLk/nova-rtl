"""Final deterministic safety gate before any recovery materialization."""

from __future__ import annotations

from typing import Literal

from nova_rtl.contracts.base import HashRef, StrictContract
from nova_rtl.contracts.recovery import (
    TERMINAL_ACTIONS,
    FailureEvent,
    RecoveryAction,
    RecoveryDecision,
    RepairDirective,
)


class RecoverySafetyError(ValueError):
    pass


class RecoveryMaterializationAuthorization(StrictContract):
    action: RecoveryAction
    creates_child_snapshot: bool
    authorized_source_hash: HashRef | None
    status: Literal["AUTHORIZED"] = "AUTHORIZED"


NON_RTL_ACTIONS: frozenset[RecoveryAction] = frozenset(
    {
        "RETRY_INFRASTRUCTURE",
        "REPAIR_SCHEMA",
        "CORRECT_EXECUTOR_OUTPUT",
        "REPARTITION_FORMAL_PROOF",
    }
)


def validate_recovery_evidence(
    failure: FailureEvent,
    directive: RepairDirective,
    *,
    expected_snapshot_hash: str,
) -> None:
    """Reject stale, mixed-snapshot, or unbound recovery evidence."""

    if directive.failure_event_id != failure.failure_event_id:
        raise RecoverySafetyError("directive failure identity does not resolve")
    refs = (*failure.primary_evidence_refs, *directive.evidence_refs)
    if not refs or any(item.snapshot_hash != expected_snapshot_hash for item in refs):
        raise RecoverySafetyError("recovery evidence snapshot identity differs")
    directive_ids = {item.evidence_id for item in directive.evidence_refs}
    if not {item.evidence_id for item in failure.primary_evidence_refs}.issubset(
        directive_ids
    ):
        raise RecoverySafetyError("directive does not bind all primary failure evidence")


def authorize_recovery_materialization(
    decision: RecoveryDecision,
    *,
    current_source_hash: str,
    requested_source_hash: str | None,
) -> RecoveryMaterializationAuthorization:
    """Ensure infrastructure and terminal routes cannot create an RTL child."""

    if decision.action in NON_RTL_ACTIONS:
        if requested_source_hash != current_source_hash:
            raise RecoverySafetyError(
                "non-RTL recovery action requires identical RTL source hash"
            )
        return RecoveryMaterializationAuthorization(
            action=decision.action,
            creates_child_snapshot=False,
            authorized_source_hash=current_source_hash,
        )
    if decision.action in TERMINAL_ACTIONS:
        if requested_source_hash not in {None, current_source_hash}:
            raise RecoverySafetyError("terminal recovery action cannot mutate RTL")
        return RecoveryMaterializationAuthorization(
            action=decision.action,
            creates_child_snapshot=False,
            authorized_source_hash=None,
        )
    if requested_source_hash is None or requested_source_hash == current_source_hash:
        raise RecoverySafetyError("RTL recovery action requires a distinct authorized child")
    return RecoveryMaterializationAuthorization(
        action=decision.action,
        creates_child_snapshot=True,
        authorized_source_hash=requested_source_hash,
    )


__all__ = [
    "RecoveryMaterializationAuthorization",
    "RecoverySafetyError",
    "authorize_recovery_materialization",
    "validate_recovery_evidence",
]
