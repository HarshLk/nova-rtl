from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.recovery import FailureEvent, RecoveryDecision, RepairDirective
from nova_rtl.recovery.safety import (
    RecoverySafetyError,
    authorize_recovery_materialization,
    validate_recovery_evidence,
)

SNAPSHOT = "sha256:" + "1" * 64


def _decision(action: str) -> RecoveryDecision:
    return RecoveryDecision.model_construct(
        recovery_decision_id=f"decision_{action.lower()}", action=action
    )


def _evidence(snapshot: str = SNAPSHOT) -> EvidenceRef:
    return EvidenceRef(
        evidence_id="evidence_safety",
        kind="CDC",
        artifact_id="artifact_safety",
        json_pointer="/crossings/0",
        snapshot_hash=snapshot,
    )


def test_infrastructure_retry_preserves_rtl_and_creates_no_child() -> None:
    authorization = authorize_recovery_materialization(
        _decision("RETRY_INFRASTRUCTURE"),
        current_source_hash=SNAPSHOT,
        requested_source_hash=SNAPSHOT,
    )

    assert not authorization.creates_child_snapshot
    assert authorization.authorized_source_hash == SNAPSHOT


def test_infrastructure_retry_cannot_mutate_rtl() -> None:
    with pytest.raises(RecoverySafetyError, match="identical RTL"):
        authorize_recovery_materialization(
            _decision("RETRY_INFRASTRUCTURE"),
            current_source_hash=SNAPSHOT,
            requested_source_hash="sha256:" + "2" * 64,
        )


@pytest.mark.parametrize("action", ["REJECT_CANDIDATE", "STOP_RUN_OR_REQUEST_HUMAN"])
def test_protected_terminal_actions_never_create_child(action: str) -> None:
    authorization = authorize_recovery_materialization(
        _decision(action),
        current_source_hash=SNAPSHOT,
        requested_source_hash=None,
    )

    assert not authorization.creates_child_snapshot


def test_stale_or_unbound_evidence_is_rejected() -> None:
    failure = FailureEvent.model_construct(
        failure_event_id="failure_cdc",
        primary_evidence_refs=(_evidence(),),
    )
    directive = RepairDirective.model_construct(
        failure_event_id="failure_cdc",
        evidence_refs=(_evidence("sha256:" + "2" * 64),),
    )

    with pytest.raises(RecoverySafetyError, match="snapshot"):
        validate_recovery_evidence(failure, directive, expected_snapshot_hash=SNAPSHOT)


def test_recovery_package_has_no_planner_shell_or_executor_imports() -> None:
    root = Path(__file__).resolve().parents[3] / "src/nova_rtl/recovery"
    forbidden = {"subprocess", "nova_rtl.adapters", "nova_rtl.planner"}
    imports: set[str] = set()
    for path in root.glob("*.py"):
        if path.name == "signoff.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)

    assert not any(
        imported == blocked or imported.startswith(blocked + ".")
        for imported in imports
        for blocked in forbidden
    )
