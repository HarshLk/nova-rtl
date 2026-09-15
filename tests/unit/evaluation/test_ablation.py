from __future__ import annotations

import pytest

from nova_rtl.evaluation.ablation import (
    AblationPolicy,
    EvaluationBudget,
    VariantObservation,
    compare_variants,
)


def _budget() -> EvaluationBudget:
    return EvaluationBudget.build(
        maximum_candidates=12,
        formal_slots=4,
        sta_slots=8,
        openroad_slots=2,
        analysis_view_ids=("asap7_hold", "asap7_setup"),
        seed=20260915,
        wall_clock_seconds=120,
    )


def _observation(variant: str, feasible: float, ppa: float) -> VariantObservation:
    return VariantObservation(
        variant=variant,
        budget_hash=_budget().budget_hash,
        feasible_yield=feasible,
        best_strict_ppa=ppa,
        schema_valid_rate=0.99,
        trace_completeness=1.0,
        median_latency_seconds=40.0,
        cost_ratio=1.5,
        semantic_duplicate_rate=0.1,
        unsafe_routes=0,
    )


def test_equal_budget_council_is_adopted_only_after_hard_thresholds() -> None:
    comparison = compare_variants(
        _observation("S", 0.40, 1.0),
        _observation("MC", 0.48, 1.01),
        AblationPolicy(),
    )

    assert comparison.adopted
    assert comparison.baseline_budget_hash == comparison.contender_budget_hash
    assert comparison.reasons == ()


def test_ablation_rejects_unfair_budgets_and_records_failed_thresholds() -> None:
    contender = _observation("MC", 0.44, 0.99).model_copy(
        update={"schema_valid_rate": 0.90}
    )
    rejected = compare_variants(_observation("S", 0.40, 1.0), contender, AblationPolicy())

    assert not rejected.adopted
    assert "FEASIBLE_YIELD_LT_15_PERCENT" in rejected.reasons
    assert "STRICT_PPA_REGRESSION" in rejected.reasons
    assert "SCHEMA_VALIDITY_BELOW_98_PERCENT" in rejected.reasons

    different = EvaluationBudget.build(
        maximum_candidates=12,
        formal_slots=4,
        sta_slots=8,
        openroad_slots=2,
        analysis_view_ids=("asap7_hold", "asap7_setup"),
        seed=1,
        wall_clock_seconds=120,
    )
    with pytest.raises(ValueError, match="budget identity"):
        compare_variants(
            _observation("S", 0.40, 1.0),
            _observation("MC", 0.48, 1.01).model_copy(
                update={"budget_hash": different.budget_hash}
            ),
            AblationPolicy(),
        )
