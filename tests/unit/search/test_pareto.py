from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nova_rtl.contracts.base import METRIC_VALUE_FIELDS, MetricSet
from nova_rtl.contracts.reporting import ParetoRecord
from nova_rtl.evaluation.metrics import (
    ComparableMetrics,
    IncomparableMetricsError,
    MissingOfficialMetricError,
    compare_metrics,
)
from nova_rtl.search.pareto import ParetoArchive


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _identities(view_hash: str | None = None) -> dict[str, str]:
    return {
        "analysis_view_set": view_hash or _hash("1"),
        "constraint_source": _hash("2"),
        "power_activity": _hash("3"),
        "platform_lock": _hash("4"),
        "constraint_binding": _hash("5"),
        "clock_graph": _hash("6"),
        "cdc_inventory": _hash("7"),
        "formal_model": _hash("8"),
    }


def _metric(view: str, setup: float, area: float, power: float) -> MetricSet:
    values = {
        "analysis_view_id": view,
        "setup_wns_ns": setup,
        "setup_tns_ns": min(setup, 0.0) * 10.0,
        "hold_wns_ns": 0.02,
        "hold_tns_ns": 0.0,
        "failing_endpoints": 10 if setup < 0.0 else 0,
        "critical_path_delay_ns": 1.0 - setup,
        "estimated_fmax_mhz": 1000.0 / (1.0 - setup),
        "mapped_area_um2": area,
        "physical_area_um2": area * 1.1,
        "cell_count": 1000,
        "register_count": 100,
        "buffer_count": 20,
        "power_total_uw": power,
        "wirelength_um": 500.0,
        "congestion_overflow": 0.0,
        "runtime_ms": 50,
        "missing_metric_reasons": {},
    }
    return MetricSet.model_validate(values)


def _comparable(
    candidate: str,
    *,
    setup: float,
    area: float,
    power: float,
    view_hash: str | None = None,
) -> ComparableMetrics:
    metrics = {
        "asap7_hold": _metric("asap7_hold", setup + 0.1, area, power),
        "asap7_setup": _metric("asap7_setup", setup, area, power),
    }
    return ComparableMetrics(
        candidate_id=candidate,
        correctness_contract="STRICT_SEQ_EQUIV",
        selection_class="PRIMARY_STRICT",
        required_view_metric_ids={
            "asap7_hold": f"metric_{candidate}_hold",
            "asap7_setup": f"metric_{candidate}_setup",
        },
        per_view_metrics=metrics,
        comparison_identity_hashes=_identities(view_hash),
        binding_hash=_hash("5"),
        clock_hash=_hash("6"),
        cdc_hash=_hash("7"),
        formal_hash=_hash("8"),
        physical_stage="OPENROAD_ROUTED",
        source_change_lines=4,
        evaluation_runtime_ms=100,
    )


def test_compare_metrics_requires_exact_identity_and_emits_complete_delta() -> None:
    baseline = _comparable("baseline_001", setup=-0.20, area=100.0, power=20.0)
    candidate = _comparable("candidate_001", setup=-0.05, area=99.0, power=19.0)

    delta = compare_metrics(baseline, candidate)

    assert delta.baseline_candidate_id == "baseline_001"
    assert delta.candidate_id == "candidate_001"
    assert delta.metric_deltas["setup_wns_ns"] == pytest.approx(0.15)
    assert delta.metric_deltas["physical_area_um2"] == pytest.approx(-1.1)
    assert set(delta.metric_deltas) == set(delta.baseline_vector)
    assert delta.comparison_hash.startswith("sha256:")


def test_mismatched_view_hash_is_not_comparable() -> None:
    baseline = _comparable("baseline_001", setup=-0.20, area=100.0, power=20.0)
    candidate = _comparable(
        "candidate_001",
        setup=-0.05,
        area=99.0,
        power=19.0,
        view_hash=_hash("9"),
    )

    with pytest.raises(IncomparableMetricsError, match="analysis_view_set"):
        compare_metrics(baseline, candidate)


def test_missing_official_metric_is_infeasible() -> None:
    candidate = _comparable("candidate_001", setup=-0.05, area=99.0, power=19.0)
    values = candidate.per_view_metrics["asap7_setup"].model_dump(mode="python")
    values["power_total_uw"] = None
    values["missing_metric_reasons"] = {"power_total_uw": "report omitted power"}
    for name in METRIC_VALUE_FIELDS:
        if values[name] is None and name != "power_total_uw":
            values["missing_metric_reasons"][name] = "not required by fixture"
    incomplete = candidate.model_copy(
        update={
            "per_view_metrics": {
                **candidate.per_view_metrics,
                "asap7_setup": MetricSet.model_validate(values),
            }
        }
    )

    with pytest.raises(MissingOfficialMetricError, match="power_total_uw"):
        compare_metrics(candidate, incomplete)


def _record(
    candidate: str,
    *,
    setup: float,
    area: float,
    contract: str = "STRICT_SEQ_EQUIV",
    selection: str = "PRIMARY_STRICT",
) -> ParetoRecord:
    return ParetoRecord(
        pareto_record_id=f"pareto_{candidate}",
        candidate_id=candidate,
        correctness_contract=contract,
        selection_class=selection,
        feasibility_predicate_version="feasibility-v1",
        required_view_metric_ids={"asap7_setup": f"metric_{candidate}"},
        metric_vector={
            "setup_wns_ns": setup,
            "setup_tns_ns": min(setup, 0.0) * 10.0,
            "hold_wns_ns": 0.01,
            "hold_tns_ns": 0.0,
            "failing_endpoints": 1.0,
            "mapped_area_um2": area,
            "physical_area_um2": area,
            "power_total_uw": 10.0,
            "source_change_lines": 4.0,
            "evaluation_runtime_ms": 100.0,
        },
        dominated_candidate_ids=(),
        objective_policy_rank=1,
        binding_hash=_hash("5"),
        clock_hash=_hash("6"),
        cdc_hash=_hash("7"),
        formal_hash=_hash("8"),
        physical_stage="OPENROAD_ROUTED",
        comparison_identity_hashes=_identities(),
        recorded_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_pareto_archive_is_deterministic_and_removes_dominated_candidates() -> None:
    weak = _record("candidate_weak", setup=-0.20, area=105.0)
    strong = _record("candidate_strong", setup=-0.05, area=100.0)
    first = ParetoArchive()
    second = ParetoArchive()

    first.add(weak)
    update = first.add(strong)
    second.add(strong)
    second.add(weak)

    assert update.removed_candidate_ids == ("candidate_weak",)
    assert tuple(item.candidate_id for item in first.frontier("STRICT_SEQ_EQUIV")) == (
        "candidate_strong",
    )
    assert first.snapshot().archive_hash == second.snapshot().archive_hash


def test_latency_aware_candidate_cannot_dominate_strict_candidate() -> None:
    archive = ParetoArchive()
    archive.add(_record("candidate_strict", setup=-0.05, area=100.0))
    archive.add(
        _record(
            "candidate_latency",
            setup=0.20,
            area=50.0,
            contract="LATENCY_AWARE",
            selection="EXPLORATORY_LATENCY_AWARE",
        )
    )

    assert tuple(
        item.candidate_id for item in archive.frontier("STRICT_SEQ_EQUIV")
    ) == ("candidate_strict",)
    assert tuple(item.candidate_id for item in archive.frontier("LATENCY_AWARE")) == (
        "candidate_latency",
    )


def test_incomparable_records_are_rejected_from_same_partition() -> None:
    archive = ParetoArchive()
    archive.add(_record("candidate_one", setup=-0.05, area=100.0))
    changed = _record("candidate_two", setup=-0.04, area=99.0).model_copy(
        update={"comparison_identity_hashes": _identities(_hash("9"))}
    )

    with pytest.raises(IncomparableMetricsError, match="analysis_view_set"):
        archive.add(changed)
