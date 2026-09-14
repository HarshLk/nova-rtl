from __future__ import annotations

from types import SimpleNamespace

import pytest

from nova_rtl.contracts.base import Diagnostic
from nova_rtl.contracts.execution import StageResult
from nova_rtl.recovery.classifier import ClassificationError, classify
from nova_rtl.recovery.compiler import compile_directive
from nova_rtl.recovery.policy import load_recovery_policy


def _stage(*, stage: str, status: str, code: str) -> StageResult:
    diagnostic = Diagnostic(
        code=code,
        severity="ERROR",
        message=f"normalized {code}",
        evidence_refs=("evidence_primary",),
    )
    return StageResult.model_construct(
        stage_result_id=f"stage_{stage.lower()}",
        run_id="run_recovery",
        candidate_id="candidate_one",
        stage=stage,
        analysis_view_id=(
            "asap7_setup"
            if stage in {"OPENSTA_FULL", "OPENROAD_PHYSICAL", "OPENROAD_ROUTED"}
            else None
        ),
        status=status,
        diagnostics=(diagnostic,),
        raw_artifacts=(SimpleNamespace(artifact_id="artifact_raw"),),
        input_hashes=SimpleNamespace(rtl_snapshot="sha256:" + "1" * 64),
        metrics=SimpleNamespace(),
    )


def _policy():
    from pathlib import Path

    return load_recovery_policy(
        Path(__file__).resolve().parents[3] / "config/policy/recovery_rules.yaml"
    )


@pytest.mark.parametrize(
    ("stage", "status", "code", "family"),
    [
        ("CONSTRAINT_BINDING", "FAIL", "CONSTRAINT_BINDING_DELTA", "CONSTRAINT_BINDING_DELTA"),
        ("CDC_INVARIANT", "FAIL", "CDC_INVARIANT_DELTA", "CDC_INVARIANT_DELTA"),
        ("FORMAL_EQUIVALENCE", "FAIL", "FORMAL_COUNTEREXAMPLE", "FORMAL_SEMANTIC_FAILURE"),
        ("FAST_SYNTH", "FAIL", "RTL_PARSE_ERROR", "RTL_PARSE_OR_ELAB_FAILURE"),
        ("OPENSTA_FULL", "FAIL", "HOLD_REGRESSION", "HOLD_REGRESSION"),
        ("OPENSTA_FULL", "FAIL", "AREA_POLICY_EXCEEDED", "AREA_POLICY_VIOLATION"),
    ],
)
def test_classifier_uses_normalized_diagnostics(
    stage: str, status: str, code: str, family: str
) -> None:
    event = classify(_stage(stage=stage, status=status, code=code), None, None, _policy())

    assert event.failure_family == family
    assert event.raw_stage_result_ref == f"stage_{stage.lower()}"
    assert event.classifier_version == "m7-classifier-v1"


def test_infrastructure_status_outranks_untrusted_diagnostic_text() -> None:
    event = classify(
        _stage(stage="OPENROAD_PHYSICAL", status="INFRASTRUCTURE_ERROR", code="UNKNOWN_TOOL_TEXT"),
        None,
        None,
        _policy(),
    )

    assert event.failure_family == "INFRASTRUCTURE_TRANSIENT"
    assert event.failure_scope == "INFRASTRUCTURE"


def test_unknown_nonpass_fails_closed_to_human_review() -> None:
    event = classify(
        _stage(stage="FAST_SYNTH", status="FAIL", code="UNREGISTERED_DIAGNOSTIC"),
        None,
        None,
        _policy(),
    )

    assert event.repairability == "HUMAN_REVIEW"
    assert not event.retryable


def test_classifier_rejects_passing_stage() -> None:
    with pytest.raises(ClassificationError, match="non-pass"):
        classify(
            _stage(stage="FAST_SYNTH", status="PASS", code="UNUSED"),
            None,
            None,
            _policy(),
        )


def test_timing_classifier_emits_complete_compiler_evidence() -> None:
    policy = _policy()
    event = classify(
        _stage(stage="OPENSTA_FULL", status="FAIL", code="TIMING_NO_GAIN"),
        None,
        None,
        policy,
    )

    assert {item.kind for item in event.primary_evidence_refs} == {"PATH", "METRIC"}
    assert compile_directive(event, event.primary_evidence_refs, policy)
