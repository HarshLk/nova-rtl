from __future__ import annotations

import pytest

from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.release import (
    AblationComparison,
    EvidenceClaim,
    FinalCandidateSeal,
    FrequencySweepResult,
    FrequencySweepTrial,
    M9SignoffReport,
)


def _hash(seed: str) -> str:
    return f"sha256:{seed * 64}"[:71]


def test_primary_seal_requires_strict_equivalence_and_complete_views() -> None:
    payload = {
        "schema_version": 1,
        "candidate_id": "candidate_primary",
        "selection_class": "PRIMARY_STRICT",
        "correctness_contract": "STRICT_SEQ_EQUIV",
        "source_hash": _hash("1"),
        "patch_hash": _hash("2"),
        "constraint_hash": _hash("3"),
        "binding_hash": _hash("4"),
        "clock_hash": _hash("5"),
        "cdc_hash": _hash("6"),
        "formal_hash": _hash("7"),
        "platform_hash": _hash("8"),
        "recipe_hash": _hash("9"),
        "required_view_ids": ("asap7_hold", "asap7_setup"),
        "completed_view_ids": ("asap7_hold", "asap7_setup"),
        "strict_proof_outcome": "PASS",
    }
    seal = FinalCandidateSeal(**payload, seal_hash=canonical_sha256(payload))
    assert seal.selection_class == "PRIMARY_STRICT"

    with pytest.raises(ValueError, match="PRIMARY_STRICT requires STRICT_SEQ_EQUIV"):
        FinalCandidateSeal(
            **{**payload, "correctness_contract": "LATENCY_AWARE"},
            seal_hash=_hash("a"),
        )


def test_claim_requires_every_evidence_reference_to_resolve() -> None:
    with pytest.raises(ValueError, match="evidence artifacts must be declared"):
        EvidenceClaim(
            claim_id="claim_setup",
            label="Worst setup slack",
            value="-0.125 ns",
            authority="MEASURED_EDA",
            artifact_ids=("artifact_timing",),
            evidence_ids=("artifact_missing",),
            comparison_identity_hashes={"analysis_view_set": _hash("b")},
        )


def test_frequency_result_requires_setup_and_hold_for_every_passing_trial() -> None:
    trial = FrequencySweepTrial(
        trial_id="trial_001",
        period_ns=2.0,
        frequency_mhz=500.0,
        setup_status="PASS",
        hold_status="FAIL",
        setup_stage_result_id="stage_setup",
        hold_stage_result_id="stage_hold",
        overlay_hash=_hash("c"),
    )
    with pytest.raises(ValueError, match="passing sweep point requires setup and hold"):
        FrequencySweepResult(
            sweep_id="sweep_001",
            candidate_id="candidate_primary",
            contract_hash=_hash("d"),
            trials=(trial,),
            passing_trial_ids=("trial_001",),
            fmax_mhz=500.0,
            result_hash=_hash("e"),
        )


def test_ablation_comparison_rejects_unequal_eda_budgets() -> None:
    with pytest.raises(ValueError, match="identical EDA budgets"):
        AblationComparison(
            comparison_id="ablation_h_mc",
            baseline_variant="H",
            contender_variant="MC",
            baseline_budget_hash=_hash("1"),
            contender_budget_hash=_hash("2"),
            baseline_feasible_yield=0.2,
            contender_feasible_yield=0.3,
            baseline_best_strict_ppa=1.0,
            contender_best_strict_ppa=1.1,
            schema_valid_rate=1.0,
            trace_completeness=1.0,
            median_latency_seconds=10.0,
            cost_ratio=1.5,
            adopted=False,
            reasons=("budget mismatch",),
        )


def test_m9_report_is_commit_bound_and_self_hashed() -> None:
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": "1" * 40,
        "implementation_tree_hash": "2" * 40,
        "m8_commit_sha": "3" * 40,
        "m8_packet_hash": _hash("3"),
        "report_bundle_hash": _hash("4"),
        "replay_manifest_hash": _hash("5"),
        "final_candidate_seal_hash": _hash("6"),
        "ablation_hash": _hash("7"),
        "frequency_sweep_hash": _hash("8"),
        "acceptance_hash": _hash("9"),
        "submission_bundle_hash": _hash("a"),
        "gate_evidence_hash": _hash("b"),
        "created_at": "2026-09-15T00:00:00Z",
    }
    report = M9SignoffReport(**payload, report_hash=canonical_sha256(payload))
    assert report.status == "PASS"
