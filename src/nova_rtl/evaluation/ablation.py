"""Fair, bounded planner and recovery ablation decisions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonNegativeFloat,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.release import AblationComparison, AblationVariant


class EvaluationBudget(StrictContract):
    maximum_candidates: NonNegativeInt = Field(gt=0)
    formal_slots: NonNegativeInt
    sta_slots: NonNegativeInt
    openroad_slots: NonNegativeInt
    analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    seed: NonNegativeInt
    wall_clock_seconds: NonNegativeInt = Field(gt=0)
    budget_hash: HashRef

    @classmethod
    def build(cls, **values: object) -> EvaluationBudget:
        values["analysis_view_ids"] = tuple(sorted(values["analysis_view_ids"]))
        return cls(**values, budget_hash=canonical_sha256(values))


class VariantObservation(StrictContract):
    variant: AblationVariant
    budget_hash: HashRef
    feasible_yield: NonNegativeFloat
    best_strict_ppa: float = Field(strict=True, allow_inf_nan=False)
    schema_valid_rate: NonNegativeFloat = Field(le=1)
    trace_completeness: NonNegativeFloat = Field(le=1)
    median_latency_seconds: NonNegativeFloat
    cost_ratio: NonNegativeFloat
    semantic_duplicate_rate: NonNegativeFloat = Field(le=1)
    unsafe_routes: NonNegativeInt


class AblationPolicy(StrictContract):
    minimum_relative_feasible_yield: NonNegativeFloat = 0.15
    minimum_schema_valid_rate: NonNegativeFloat = 0.98
    minimum_trace_completeness: NonNegativeFloat = 0.99
    maximum_latency_seconds: NonNegativeFloat = 120.0
    maximum_cost_ratio: NonNegativeFloat = 2.0
    require_nonregressing_ppa: Literal[True] = True


def compare_variants(
    baseline: VariantObservation,
    contender: VariantObservation,
    policy: AblationPolicy,
) -> AblationComparison:
    """Apply disclosed adoption thresholds to observations with one budget identity."""

    if baseline.budget_hash != contender.budget_hash:
        raise ValueError("ablation variants do not share the same budget identity")
    if baseline.variant == contender.variant:
        raise ValueError("ablation variants must differ")
    required_yield = baseline.feasible_yield * (1 + policy.minimum_relative_feasible_yield)
    reasons: list[str] = []
    if contender.feasible_yield < required_yield:
        reasons.append("FEASIBLE_YIELD_LT_15_PERCENT")
    if contender.best_strict_ppa < baseline.best_strict_ppa:
        reasons.append("STRICT_PPA_REGRESSION")
    if contender.schema_valid_rate < policy.minimum_schema_valid_rate:
        reasons.append("SCHEMA_VALIDITY_BELOW_98_PERCENT")
    if contender.trace_completeness <= policy.minimum_trace_completeness:
        reasons.append("TRACE_COMPLETENESS_NOT_ABOVE_99_PERCENT")
    if contender.median_latency_seconds > policy.maximum_latency_seconds:
        reasons.append("LATENCY_ABOVE_120_SECONDS")
    if contender.cost_ratio >= policy.maximum_cost_ratio:
        reasons.append("COST_NOT_BELOW_2X")
    if contender.semantic_duplicate_rate > baseline.semantic_duplicate_rate:
        reasons.append("SEMANTIC_DUPLICATES_INCREASED")
    if contender.unsafe_routes:
        reasons.append("UNSAFE_ROUTE_PRESENT")
    identity = canonical_sha256(
        {
            "baseline": baseline.model_dump(mode="json"),
            "contender": contender.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }
    )
    return AblationComparison(
        comparison_id="ablation_" + identity.removeprefix("sha256:")[:24],
        baseline_variant=baseline.variant,
        contender_variant=contender.variant,
        baseline_budget_hash=baseline.budget_hash,
        contender_budget_hash=contender.budget_hash,
        baseline_feasible_yield=baseline.feasible_yield,
        contender_feasible_yield=contender.feasible_yield,
        baseline_best_strict_ppa=baseline.best_strict_ppa,
        contender_best_strict_ppa=contender.best_strict_ppa,
        schema_valid_rate=contender.schema_valid_rate,
        trace_completeness=contender.trace_completeness,
        median_latency_seconds=contender.median_latency_seconds,
        cost_ratio=contender.cost_ratio,
        adopted=not reasons,
        reasons=tuple(reasons),
    )


__all__ = [
    "AblationPolicy",
    "EvaluationBudget",
    "VariantObservation",
    "compare_variants",
]
