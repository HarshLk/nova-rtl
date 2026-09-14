from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.recovery import FailureEvent
from nova_rtl.recovery.compiler import DirectiveCompilationError, compile_directive
from nova_rtl.recovery.policy import load_recovery_policy

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = "sha256:" + "1" * 64


def _evidence(kind: str, suffix: str) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"evidence_{suffix}",
        kind=kind,
        artifact_id=f"artifact_{suffix}",
        json_pointer=f"/{suffix}",
        snapshot_hash=SNAPSHOT,
    )


def _failure(family: str, evidence: tuple[EvidenceRef, ...]) -> FailureEvent:
    policy = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    rule = policy.rules[family]
    return FailureEvent(
        failure_event_id=f"failure_{family.lower()}",
        run_id="run_recovery",
        subject_type="CANDIDATE",
        subject_id="candidate_one",
        candidate_id="candidate_one",
        proposal_id="proposal_one",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_one",
        failed_stage="OPENSTA_FULL",
        analysis_view_id="asap7_setup",
        failure_family=family,
        failure_scope="CANDIDATE",
        repairability=rule.repairability,
        severity=rule.severity,
        retryable=rule.retryable,
        constraint_hash_verified=True,
        analysis_view_hash_verified=True,
        constraint_binding_status="EQUIVALENT",
        protected_structure_status="UNCHANGED",
        metric_delta={},
        primary_evidence_refs=evidence,
        raw_stage_result_ref="stage_failed",
        classifier_version="m7-classifier-v1",
    )


def test_compiler_emits_checker_aware_path_migration_directive() -> None:
    policy = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    evidence = (_evidence("PATH", "path"), _evidence("METRIC", "metric"))
    directive = compile_directive(
        _failure("CRITICAL_PATH_MIGRATION", evidence), evidence, policy
    )

    assert directive.allowed_scope == "NEW_OR_SHARED_TIMING_CONE"
    assert "successful_local_mechanism" in directive.preserve
    assert "identical_target_repetition" in directive.prohibit
    assert directive.compiler_rule_refs == ("RECOVERY_CRITICAL_PATH_MIGRATION",)
    assert directive.recommended_roles == ("timing_forensics",)


def test_compiler_rejects_missing_required_evidence_kind() -> None:
    policy = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    evidence = (_evidence("PATH", "path"),)

    with pytest.raises(DirectiveCompilationError, match="required evidence"):
        compile_directive(_failure("CRITICAL_PATH_MIGRATION", evidence), evidence, policy)


def test_protected_failure_directive_forbids_rtl_repair() -> None:
    policy = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    evidence = (_evidence("HISTORY", "protected"),)
    directive = compile_directive(
        _failure("PROTECTED_STRUCTURE_VIOLATION", evidence), evidence, policy
    )

    assert directive.allowed_scope == "NO_RTL_MUTATION"
    assert "rtl_mutation" in directive.prohibit
    assert directive.recommended_actions == ("REJECT_CANDIDATE",)
