from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nova_rtl.contracts.base import ArtifactRef
from nova_rtl.contracts.optimization import CandidateRecord
from nova_rtl.evaluation.cascade import (
    EVALUATION_GATE_ORDER,
    EvaluationCascade,
    GateAssessment,
)
from nova_rtl.evaluation.feasibility import (
    CandidateFeasibilityEvidence,
    FeasibilityPolicy,
    is_feasible,
)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _artifact(identifier: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=identifier,
        uri=f"artifact://sha256/{digest * 64}",
        sha256=_hash(digest),
        media_type="application/json",
        size_bytes=10,
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        producer_stage_result_id=None,
        classification="RESTRICTED_RTL",
    )


def _candidate() -> CandidateRecord:
    return CandidateRecord(
        candidate_id="cand_priority_mux",
        run_id="run_m4_tiny",
        parent_candidate_id="baseline",
        lineage_depth=1,
        proposal_id="proposal_priority_mux",
        opportunity_id="opportunity_priority_mux",
        rtl_snapshot_artifact=_artifact("artifact_snapshot", "1"),
        patch_artifact=_artifact("artifact_patch", "2"),
        source_hash=_hash("1"),
        changed_spans=("source_priority_mux",),
        transform_fingerprint="priority_mux:v1:abcd",
        required_correctness_contract="STRICT_SEQ_EQUIV",
        stage_result_ids=(),
        per_view_metrics={},
        proof_result_id=None,
        binding_manifest_id=None,
        clock_inventory_id=None,
        cdc_inventory_id=None,
        hard_gate_summary=None,
        classification="INCONCLUSIVE",
        selection_class="PRIMARY_STRICT",
        terminal_disposition="PENDING_EVALUATION",
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        recovery_parent_failure_id=None,
    )


def _policy() -> FeasibilityPolicy:
    return FeasibilityPolicy.build(
        required_analysis_view_ids=("asap7_hold", "asap7_setup"),
        baseline_analysis_view_set_hash=_hash("a"),
        baseline_constraint_source_hash=_hash("b"),
        baseline_generated_clock_graph_hash=_hash("c"),
        max_area_growth_percent=5.0,
    )


def _evidence(**overrides: object) -> CandidateFeasibilityEvidence:
    values: dict[str, object] = {
        "candidate_id": "cand_priority_mux",
        "source_hash": _hash("1"),
        "mandatory_proof_outcome": "PASS",
        "proof_contract": "STRICT_SEQ_EQUIV",
        "analysis_view_set_hash": _hash("a"),
        "constraint_source_hash": _hash("b"),
        "effective_constraint_binding": "EQUIVALENT",
        "unresolved_constraint_selectors": 0,
        "generated_clock_graph_hash": _hash("c"),
        "new_or_unapproved_cdc_crossings": 0,
        "changed_approved_cdc_structures": 0,
        "unconstrained_endpoints": 0,
        "area_growth_percent": 2.0,
        "view_complete": {"asap7_hold": True, "asap7_setup": True},
        "view_hard_limits_pass": {"asap7_hold": True, "asap7_setup": True},
        "view_metrics_present": {"asap7_hold": True, "asap7_setup": True},
        "no_hard_domain_regression": True,
    }
    values.update(overrides)
    return CandidateFeasibilityEvidence.build(**values)  # type: ignore[arg-type]


def test_hard_feasibility_requires_every_architecture_predicate() -> None:
    assert is_feasible(_candidate(), _evidence(), _policy()) is True

    failures = (
        {"mandatory_proof_outcome": "INCONCLUSIVE"},
        {"analysis_view_set_hash": _hash("d")},
        {"effective_constraint_binding": "FORBIDDEN_DELTA"},
        {"unresolved_constraint_selectors": 1},
        {"generated_clock_graph_hash": _hash("d")},
        {"new_or_unapproved_cdc_crossings": 1},
        {"changed_approved_cdc_structures": 1},
        {"unconstrained_endpoints": 1},
        {"area_growth_percent": 5.1},
        {"view_complete": {"asap7_hold": True, "asap7_setup": False}},
        {"view_hard_limits_pass": {"asap7_hold": False, "asap7_setup": True}},
        {"view_metrics_present": {"asap7_hold": True, "asap7_setup": False}},
        {"no_hard_domain_regression": False},
    )
    for override in failures:
        assert is_feasible(_candidate(), _evidence(**override), _policy()) is False


def test_inconclusive_formal_is_never_feasible() -> None:
    evidence = _evidence(mandatory_proof_outcome="INCONCLUSIVE")
    assert is_feasible(_candidate(), evidence, _policy()) is False


def test_view_maps_must_exactly_cover_policy() -> None:
    with pytest.raises(ValueError, match="same exact view set"):
        _evidence(view_complete={"asap7_setup": True})


def test_cascade_runs_gates_in_order_and_emits_started_and_terminal_events() -> None:
    attempted: list[str] = []

    def pass_gate(gate_id: str):  # type: ignore[no-untyped-def]
        def run(candidate: CandidateRecord) -> GateAssessment:
            attempted.append(gate_id)
            return GateAssessment(
                gate_id=gate_id,
                status="PASS",
                stage_result_ids=(f"stage_{gate_id.replace('.', '_')}",),
                diagnostic_codes=(),
            )

        return run

    events = []
    cascade = EvaluationCascade(
        {gate_id: pass_gate(gate_id) for gate_id in EVALUATION_GATE_ORDER},
        event_sink=events.append,
    )
    result = cascade.evaluate(_candidate())

    assert tuple(attempted) == EVALUATION_GATE_ORDER
    assert result.status == "PASS"
    assert len(events) == 2 * len(EVALUATION_GATE_ORDER)
    assert tuple(event.sequence for event in events) == tuple(range(len(events)))
    assert events[0].event_type == "GATE_STARTED"
    assert events[-1].event_type == "GATE_COMPLETED"


def test_cascade_stops_on_first_hard_nonpass_without_skipping_invariants() -> None:
    attempted: list[str] = []

    def run(candidate: CandidateRecord, gate_id: str) -> GateAssessment:
        attempted.append(gate_id)
        status = "INCONCLUSIVE" if gate_id == "4" else "PASS"
        return GateAssessment(
            gate_id=gate_id,
            status=status,
            stage_result_ids=(f"stage_{gate_id.replace('.', '_')}",),
            diagnostic_codes=() if status == "PASS" else ("FORMAL_INCONCLUSIVE",),
        )

    cascade = EvaluationCascade(
        {
            gate_id: (lambda candidate, selected=gate_id: run(candidate, selected))
            for gate_id in EVALUATION_GATE_ORDER
        }
    )
    result = cascade.evaluate(_candidate())

    assert tuple(attempted) == ("0", "0.5", "1", "2", "3", "4")
    assert result.status == "INCONCLUSIVE"
    assert result.terminal_gate_id == "4"
    assert "2" in attempted  # binding / clock / CDC invariant gate is mandatory
