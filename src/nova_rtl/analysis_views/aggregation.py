"""Complete-required-view validation and conservative worst-view aggregation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from nova_rtl.contracts.analysis import AnalysisViewContract
from nova_rtl.contracts.base import (
    EntityId,
    FiniteFloat,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.execution import StageResult


class IncompleteRequiredViewError(RuntimeError):
    """A required view, stage, metric, or immutable identity is incomplete."""


_MEASURED_TIMING_VIOLATION_CODES = frozenset(
    {
        "TIMING_SETUP_VIOLATION",
        "TIMING_HOLD_VIOLATION",
        "PHYSICAL_TIMING_VIOLATION",
    }
)


def is_complete_measured_timing_violation(result: StageResult) -> bool:
    return (
        result.status == "FAIL"
        and result.stage in {"OPENSTA_FULL", "OPENROAD_PHYSICAL"}
        and bool(result.diagnostics)
        and all(
            diagnostic.code in _MEASURED_TIMING_VIOLATION_CODES
            for diagnostic in result.diagnostics
        )
    )


class PerViewMetrics(StrictContract):
    analysis_view_id: EntityId
    check: Literal["SETUP", "HOLD", "POWER"]
    wns_ns: FiniteFloat | None
    tns_ns: FiniteFloat | None
    power_total_uw: FiniteFloat | None
    limiting_stage: str


class AggregatedMetrics(StrictContract):
    candidate_id: EntityId
    setup_wns_ns: FiniteFloat
    setup_tns_ns: FiniteFloat
    setup_view_id: EntityId
    hold_wns_ns: FiniteFloat
    hold_tns_ns: FiniteFloat
    hold_view_id: EntityId
    power_total_uw: FiniteFloat | None
    power_view_id: EntityId | None
    per_view_metrics: dict[EntityId, PerViewMetrics]


def aggregate_required_views(
    results: Mapping[str, Mapping[str, StageResult]],
    contracts: Iterable[AnalysisViewContract],
) -> AggregatedMetrics:
    """Validate every mandated stage and select minimum setup/hold WNS."""

    required = tuple(contract for contract in contracts if contract.required)
    view_ids = tuple(contract.analysis_view_id for contract in required)
    if len(view_ids) != len(set(view_ids)):
        raise IncompleteRequiredViewError("required analysis-view IDs must be unique")
    if not any(contract.check == "SETUP" for contract in required):
        raise IncompleteRequiredViewError("required setup view is missing")
    if not any(contract.check == "HOLD" for contract in required):
        raise IncompleteRequiredViewError("required hold view is missing")

    identity_envelopes: set[tuple[str, str, str, str, str, str]] = set()
    power_activity_hashes: set[str] = set()
    per_view: dict[str, PerViewMetrics] = {}
    for contract in required:
        stage_results = results.get(contract.analysis_view_id)
        if stage_results is None:
            raise IncompleteRequiredViewError(
                f"required view {contract.analysis_view_id} is missing"
            )
        required_stage_results: list[StageResult] = []
        expected_view_hash = canonical_sha256(contract)
        for stage in contract.required_stages:
            result = stage_results.get(stage)
            if result is None:
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} is missing stage {stage}"
                )
            if not isinstance(result, StageResult):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} stage {stage} "
                    "is not a typed StageResult"
                )
            if result.analysis_view_id != contract.analysis_view_id:
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} result label mismatch"
                )
            if result.stage != stage:
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} stage label mismatch for {stage}"
                )
            if result.status != "PASS" and not is_complete_measured_timing_violation(result):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} stage {stage} "
                    "is neither PASS nor a complete measured timing violation"
                )
            input_hashes = result.input_hashes
            if (
                input_hashes.analysis_view != expected_view_hash
                or input_hashes.constraints != contract.sdc_hash
            ):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} identity mismatch"
                )
            candidate_id = result.candidate_id
            run_id = result.run_id
            envelope = (
                run_id,
                candidate_id,
                input_hashes.rtl_snapshot,
                input_hashes.design_contract,
                input_hashes.platform_lock,
                input_hashes.constraint_binding,
            )
            if any(not isinstance(item, str) for item in envelope):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} immutable identity is incomplete"
                )
            identity_envelopes.add(envelope)
            required_stage_results.append(result)

        if contract.check == "POWER":
            measured_power: list[tuple[float, str]] = []
            for result in required_stage_results:
                input_hashes = result.input_hashes
                if input_hashes.power_activity is None:
                    raise IncompleteRequiredViewError(
                        f"required view {contract.analysis_view_id} lacks power activity identity"
                    )
                power_activity_hashes.add(input_hashes.power_activity)
                power = getattr(result.metrics, "power_total_uw", None)
                if not isinstance(power, (int, float)):
                    raise IncompleteRequiredViewError(
                        f"required view {contract.analysis_view_id} lacks power_total_uw"
                    )
                measured_power.append((float(power), str(result.stage)))
            worst_power, limiting_stage = max(measured_power, key=lambda item: item[0])
            per_view[contract.analysis_view_id] = PerViewMetrics(
                analysis_view_id=contract.analysis_view_id,
                check="POWER",
                wns_ns=None,
                tns_ns=None,
                power_total_uw=worst_power,
                limiting_stage=limiting_stage,
            )
            continue

        wns_field = "setup_wns_ns" if contract.check == "SETUP" else "hold_wns_ns"
        tns_field = "setup_tns_ns" if contract.check == "SETUP" else "hold_tns_ns"
        measured: list[tuple[float, float, str]] = []
        for stage, result in zip(
            contract.required_stages, required_stage_results, strict=True
        ):
            metrics = getattr(result, "metrics", None)
            wns = getattr(metrics, wns_field, None)
            tns = getattr(metrics, tns_field, None)
            if not isinstance(wns, (int, float)):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} lacks {wns_field}"
                )
            if not isinstance(tns, (int, float)):
                raise IncompleteRequiredViewError(
                    f"required view {contract.analysis_view_id} lacks {tns_field}"
                )
            measured.append((float(wns), float(tns), stage))
        worst_wns, corresponding_tns, limiting_stage = min(measured, key=lambda item: item[0])
        per_view[contract.analysis_view_id] = PerViewMetrics(
            analysis_view_id=contract.analysis_view_id,
            check=contract.check,
            wns_ns=worst_wns,
            tns_ns=corresponding_tns,
            power_total_uw=None,
            limiting_stage=limiting_stage,
        )

    if len(identity_envelopes) != 1:
        raise IncompleteRequiredViewError(
            "required analysis views do not share one immutable identity envelope"
        )
    if len(power_activity_hashes) > 1:
        raise IncompleteRequiredViewError(
            "required power views do not share one activity identity"
        )
    setup = min(
        (metrics for metrics in per_view.values() if metrics.check == "SETUP"),
        key=lambda metrics: metrics.wns_ns,
    )
    hold = min(
        (metrics for metrics in per_view.values() if metrics.check == "HOLD"),
        key=lambda metrics: metrics.wns_ns,
    )
    power_views = tuple(
        metrics for metrics in per_view.values() if metrics.check == "POWER"
    )
    power = (
        max(power_views, key=lambda metrics: float(metrics.power_total_uw))
        if power_views
        else None
    )
    identity = next(iter(identity_envelopes))
    return AggregatedMetrics(
        candidate_id=identity[1],
        setup_wns_ns=setup.wns_ns,
        setup_tns_ns=setup.tns_ns,
        setup_view_id=setup.analysis_view_id,
        hold_wns_ns=hold.wns_ns,
        hold_tns_ns=hold.tns_ns,
        hold_view_id=hold.analysis_view_id,
        power_total_uw=power.power_total_uw if power else None,
        power_view_id=power.analysis_view_id if power else None,
        per_view_metrics=per_view,
    )


__all__ = [
    "AggregatedMetrics",
    "IncompleteRequiredViewError",
    "PerViewMetrics",
    "aggregate_required_views",
    "is_complete_measured_timing_violation",
]
