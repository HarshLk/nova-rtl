from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nova_rtl.contracts.base import ArtifactRef, canonical_sha256
from nova_rtl.contracts.optimization import CandidateRecord
from nova_rtl.evaluation.cascade import EVALUATION_GATE_ORDER, EvaluationCascade, GateAssessment
from nova_rtl.optimization.flow import M4TerminalCandidateBundle, _terminal_classification


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _artifact(identifier: str, character: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=identifier,
        uri=f"artifact://sha256/{character * 64}",
        sha256=_hash(character),
        media_type="application/json",
        size_bytes=10,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        producer_stage_result_id=None,
        classification="INTERNAL",
    )


def _candidate(stage_ids: tuple[str, ...], classification: str) -> CandidateRecord:
    return CandidateRecord(
        candidate_id="cand_terminal",
        run_id="run_parent",
        parent_candidate_id="baseline",
        lineage_depth=1,
        proposal_id="proposal_terminal",
        opportunity_id="opportunity_terminal",
        rtl_snapshot_artifact=_artifact("artifact_rtl", "1"),
        patch_artifact=_artifact("artifact_patch", "2"),
        source_hash=_hash("1"),
        changed_spans=("span_terminal",),
        transform_fingerprint="priority_mux:v1:terminal",
        required_correctness_contract="STRICT_SEQ_EQUIV",
        stage_result_ids=stage_ids,
        per_view_metrics={},
        proof_result_id=None,
        binding_manifest_id=None,
        clock_inventory_id=None,
        cdc_inventory_id=None,
        hard_gate_summary=None,
        classification=classification,
        selection_class="PRIMARY_STRICT",
        terminal_disposition=classification,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        recovery_parent_failure_id=None,
    )


@pytest.mark.parametrize("terminal_gate", EVALUATION_GATE_ORDER)
def test_terminal_bundle_accepts_exact_attempted_gate_prefix(terminal_gate: str) -> None:
    stage_by_gate = {
        gate: (() if gate in {"0", "0.5"} else (f"stage_gate_{gate}",))
        for gate in EVALUATION_GATE_ORDER
    }

    def run_gate(_candidate: CandidateRecord, gate: str) -> GateAssessment:
        return GateAssessment(
            gate_id=gate,
            status="FAIL" if gate == terminal_gate else "PASS",
            stage_result_ids=stage_by_gate[gate],
            diagnostic_codes=("TERMINAL_REJECTION",) if gate == terminal_gate else (),
        )

    seed = _candidate((), "REJECTED_POLICY")
    evaluation = EvaluationCascade(
        {
            gate: (lambda candidate, selected=gate: run_gate(candidate, selected))
            for gate in EVALUATION_GATE_ORDER
        }
    ).evaluate(seed)
    attempted_stage_ids = tuple(
        dict.fromkeys(
            stage_id
            for assessment in evaluation.assessments
            for stage_id in assessment.stage_result_ids
        )
    )
    candidate = _candidate(attempted_stage_ids, _terminal_classification(evaluation))
    stage_artifacts = {
        stage_id: _artifact(f"artifact_{stage_id}", chr(ord("3") + index))
        for index, stage_id in enumerate(attempted_stage_ids)
    }
    payload = {
        "schema_version": 1,
        "bundle_kind": "EARLY_TERMINAL",
        "parent_run_id": "run_parent",
        "parent_run_index_hash": _hash("a"),
        "candidate_run_id": "run_candidate",
        "candidate_run_index_hash": _hash("b"),
        "m3_source_map_artifact": _artifact("artifact_source_map", "c"),
        "m3_ranked_opportunities_artifact": _artifact("artifact_ranked", "d"),
        "syntax_span_artifact": _artifact("artifact_syntax", "e"),
        "proposal_artifact": _artifact("artifact_proposal", "f"),
        "candidate": candidate,
        "evaluation": evaluation,
        "candidate_stage_result_artifacts": stage_artifacts,
        "formal_stage_result_artifacts": {},
        "experiment_record_artifact": _artifact("artifact_experiment", "9"),
        "replay_event_sequence_range": (3, 3 + 2 * len(evaluation.assessments) + 3),
        "gate_event_ids": tuple(
            f"event_{index}" for index in range(2 * len(evaluation.assessments))
        ),
        "replay_prefix_digest": _hash("8"),
        "status": "FAIL",
    }
    provisional = M4TerminalCandidateBundle.model_construct(
        **payload, bundle_hash=_hash("0")
    )
    bundle = M4TerminalCandidateBundle(
        **payload,
        bundle_hash=canonical_sha256(provisional, exclude=frozenset({"bundle_hash"})),
    )

    assert bundle.evaluation.terminal_gate_id == terminal_gate
    assert len(bundle.gate_event_ids) == 2 * len(bundle.evaluation.assessments)
    assert set(bundle.candidate_stage_result_artifacts) == set(attempted_stage_ids)


@pytest.mark.parametrize(
    ("terminal_gate", "expected"),
    (
        ("0", "REJECTED_SAFETY"),
        ("0.5", "REJECTED_SAFETY"),
        ("1", "REJECTED_SAFETY"),
        ("2", "REJECTED_SAFETY"),
        ("3", "REJECTED_POLICY"),
        ("4", "REJECTED_CORRECTNESS"),
        ("5", "REJECTED_POLICY"),
        ("6", "REJECTED_POLICY"),
        ("7", "REJECTED_POLICY"),
    ),
)
def test_terminal_gate_maps_to_durable_rejection_classification(
    terminal_gate: str, expected: str
) -> None:
    candidate = _candidate((), "INCONCLUSIVE")
    evaluation = EvaluationCascade(
        {
            gate: (
                lambda _candidate, selected=gate: GateAssessment(
                    gate_id=selected,
                    status="FAIL" if selected == terminal_gate else "PASS",
                    stage_result_ids=(),
                    diagnostic_codes=("REJECTED",) if selected == terminal_gate else (),
                )
            )
            for gate in EVALUATION_GATE_ORDER
        }
    ).evaluate(candidate)

    assert _terminal_classification(evaluation) == expected


def test_terminal_bundle_rejects_unattempted_stage_artifacts() -> None:
    candidate = _candidate((), "REJECTED_SAFETY")
    evaluation = EvaluationCascade(
        {
            gate: (
                lambda _candidate, selected=gate: GateAssessment(
                    gate_id=selected,
                    status="FAIL" if selected == "0" else "PASS",
                    stage_result_ids=(),
                    diagnostic_codes=("POLICY_REJECTION",) if selected == "0" else (),
                )
            )
            for gate in EVALUATION_GATE_ORDER
        }
    ).evaluate(candidate)
    payload = {
        "schema_version": 1,
        "bundle_kind": "EARLY_TERMINAL",
        "parent_run_id": "run_parent",
        "parent_run_index_hash": _hash("a"),
        "candidate_run_id": "run_candidate",
        "candidate_run_index_hash": _hash("b"),
        "m3_source_map_artifact": _artifact("artifact_source_map", "c"),
        "m3_ranked_opportunities_artifact": _artifact("artifact_ranked", "d"),
        "syntax_span_artifact": _artifact("artifact_syntax", "e"),
        "proposal_artifact": _artifact("artifact_proposal", "f"),
        "candidate": candidate,
        "evaluation": evaluation,
        "candidate_stage_result_artifacts": {
            "stage_unattempted": _artifact("artifact_unattempted", "3")
        },
        "formal_stage_result_artifacts": {},
        "experiment_record_artifact": _artifact("artifact_experiment", "9"),
        "replay_event_sequence_range": (3, 7),
        "gate_event_ids": ("event_0", "event_1"),
        "replay_prefix_digest": _hash("8"),
        "status": "FAIL",
    }
    provisional = M4TerminalCandidateBundle.model_construct(
        **payload, bundle_hash=_hash("0")
    )

    with pytest.raises(ValueError, match="attempted stage result set"):
        M4TerminalCandidateBundle(
            **payload,
            bundle_hash=canonical_sha256(
                provisional, exclude=frozenset({"bundle_hash"})
            ),
        )
