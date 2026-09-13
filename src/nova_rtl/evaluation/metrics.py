"""Exact metric comparability and deterministic multi-view aggregation."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    FiniteFloat,
    HashRef,
    MetricSet,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.optimization import SelectionClass
from nova_rtl.contracts.reporting import StableLowerName


class IncomparableMetricsError(ValueError):
    """Metric evidence was produced under different authoritative inputs."""


class MissingOfficialMetricError(ValueError):
    """A required official metric is absent from at least one required view."""


REQUIRED_COMPARISON_IDENTITIES = frozenset(
    {
        "analysis_view_set",
        "constraint_source",
        "power_activity",
        "platform_lock",
        "constraint_binding",
        "clock_graph",
        "cdc_inventory",
        "formal_model",
    }
)

OFFICIAL_METRIC_VECTOR = (
    "setup_wns_ns",
    "setup_tns_ns",
    "hold_wns_ns",
    "hold_tns_ns",
    "failing_endpoints",
    "mapped_area_um2",
    "physical_area_um2",
    "power_total_uw",
    "source_change_lines",
    "evaluation_runtime_ms",
)


class ComparableMetrics(StrictContract):
    """Complete metrics and identities eligible for an official comparison."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    correctness_contract: CorrectnessContract
    selection_class: SelectionClass
    required_view_metric_ids: dict[EntityId, EntityId] = Field(min_length=1)
    per_view_metrics: dict[EntityId, MetricSet] = Field(min_length=1)
    comparison_identity_hashes: dict[StableLowerName, HashRef]
    binding_hash: HashRef
    clock_hash: HashRef
    cdc_hash: HashRef
    formal_hash: HashRef
    physical_stage: Literal[
        "OPENROAD_PHYSICAL", "OPENROAD_PLACED_CTS", "OPENROAD_ROUTED"
    ]
    source_change_lines: NonNegativeInt
    evaluation_runtime_ms: NonNegativeInt

    @field_validator(
        "required_view_metric_ids", "per_view_metrics", "comparison_identity_hashes"
    )
    @classmethod
    def maps_are_canonically_ordered(cls, value: dict[str, object]) -> dict[str, object]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def views_contract_and_identities_are_closed(self) -> Self:
        expected_contract = {
            "PRIMARY_STRICT": "STRICT_SEQ_EQUIV",
            "SECONDARY_RETIMED": "RETIMING_EQUIV",
            "EXPLORATORY_LATENCY_AWARE": "LATENCY_AWARE",
        }[self.selection_class]
        if self.correctness_contract != expected_contract:
            raise ValueError(f"{self.selection_class} requires {expected_contract}")
        if set(self.required_view_metric_ids) != set(self.per_view_metrics):
            raise ValueError("required metric IDs and metrics must cover the same views")
        for view_id, metrics in self.per_view_metrics.items():
            if metrics.analysis_view_id != view_id:
                raise ValueError("metric view key must match MetricSet analysis_view_id")
        supplied = set(self.comparison_identity_hashes)
        if supplied != REQUIRED_COMPARISON_IDENTITIES:
            missing = sorted(REQUIRED_COMPARISON_IDENTITIES - supplied)
            extra = sorted(supplied - REQUIRED_COMPARISON_IDENTITIES)
            raise ValueError(
                "comparison identities must be exact "
                f"(missing={missing}, extra={extra})"
            )
        linked = {
            "constraint_binding": self.binding_hash,
            "clock_graph": self.clock_hash,
            "cdc_inventory": self.cdc_hash,
            "formal_model": self.formal_hash,
        }
        for name, expected in linked.items():
            if self.comparison_identity_hashes[name] != expected:
                raise ValueError(f"{name} identity disagrees with its authoritative hash")
        return self


class MetricDelta(StrictContract):
    """Canonical candidate-minus-baseline metric vector."""

    schema_version: Literal[1] = 1
    baseline_candidate_id: EntityId
    candidate_id: EntityId
    comparison_identity_hashes: dict[StableLowerName, HashRef]
    baseline_vector: dict[StableLowerName, FiniteFloat]
    candidate_vector: dict[StableLowerName, FiniteFloat]
    metric_deltas: dict[StableLowerName, FiniteFloat]
    comparison_hash: HashRef

    @model_validator(mode="after")
    def vectors_and_hash_are_canonical(self) -> Self:
        names = set(self.baseline_vector)
        if names != set(self.candidate_vector) or names != set(self.metric_deltas):
            raise ValueError("metric delta vectors must contain identical dimensions")
        expected = canonical_sha256(self, exclude=frozenset({"comparison_hash"}))
        if self.comparison_hash != expected:
            raise ValueError("metric comparison hash is not canonical")
        return self


def _require_metric(metrics: MetricSet, name: str) -> float:
    value = getattr(metrics, name)
    if value is None:
        reason = metrics.missing_metric_reasons.get(name, "missing")
        raise MissingOfficialMetricError(
            f"required official metric {name} is unavailable in "
            f"{metrics.analysis_view_id}: {reason}"
        )
    return float(value)


def metric_vector(evidence: ComparableMetrics) -> dict[str, float]:
    """Aggregate complete required-view metrics conservatively and deterministically."""

    metrics = tuple(evidence.per_view_metrics[key] for key in sorted(evidence.per_view_metrics))
    setup_wns = [_require_metric(item, "setup_wns_ns") for item in metrics]
    setup_tns = [_require_metric(item, "setup_tns_ns") for item in metrics]
    hold_wns = [_require_metric(item, "hold_wns_ns") for item in metrics]
    hold_tns = [_require_metric(item, "hold_tns_ns") for item in metrics]
    failing = [_require_metric(item, "failing_endpoints") for item in metrics]
    mapped_area = [_require_metric(item, "mapped_area_um2") for item in metrics]
    physical_area = [_require_metric(item, "physical_area_um2") for item in metrics]
    power = [_require_metric(item, "power_total_uw") for item in metrics]
    return {
        "setup_wns_ns": min(setup_wns),
        "setup_tns_ns": sum(setup_tns),
        "hold_wns_ns": min(hold_wns),
        "hold_tns_ns": sum(hold_tns),
        "failing_endpoints": sum(failing),
        "mapped_area_um2": max(mapped_area),
        "physical_area_um2": max(physical_area),
        "power_total_uw": max(power),
        "source_change_lines": float(evidence.source_change_lines),
        "evaluation_runtime_ms": float(evidence.evaluation_runtime_ms),
    }


def _require_comparable(baseline: ComparableMetrics, candidate: ComparableMetrics) -> None:
    if set(baseline.per_view_metrics) != set(candidate.per_view_metrics):
        raise IncomparableMetricsError("required analysis view IDs differ")
    if baseline.physical_stage != candidate.physical_stage:
        raise IncomparableMetricsError("physical_stage differs")
    for name in sorted(REQUIRED_COMPARISON_IDENTITIES):
        if baseline.comparison_identity_hashes[name] != candidate.comparison_identity_hashes[name]:
            raise IncomparableMetricsError(f"comparison identity differs: {name}")


def compare_metrics(
    baseline: ComparableMetrics, candidate: ComparableMetrics
) -> MetricDelta:
    """Compare only complete measurements made under identical authoritative inputs."""

    _require_comparable(baseline, candidate)
    baseline_vector = metric_vector(baseline)
    candidate_vector = metric_vector(candidate)
    deltas = {
        name: candidate_vector[name] - baseline_vector[name]
        for name in OFFICIAL_METRIC_VECTOR
    }
    payload = {
        "schema_version": 1,
        "baseline_candidate_id": baseline.candidate_id,
        "candidate_id": candidate.candidate_id,
        "comparison_identity_hashes": candidate.comparison_identity_hashes,
        "baseline_vector": baseline_vector,
        "candidate_vector": candidate_vector,
        "metric_deltas": deltas,
    }
    return MetricDelta(**payload, comparison_hash=canonical_sha256(payload))


__all__ = [
    "ComparableMetrics",
    "IncomparableMetricsError",
    "MetricDelta",
    "MissingOfficialMetricError",
    "OFFICIAL_METRIC_VECTOR",
    "REQUIRED_COMPARISON_IDENTITIES",
    "compare_metrics",
    "metric_vector",
]
