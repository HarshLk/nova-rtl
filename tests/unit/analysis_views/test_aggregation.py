from __future__ import annotations

import pytest

from nova_rtl.analysis_views.aggregation import (
    IncompleteRequiredViewError,
    aggregate_required_views,
)
from nova_rtl.contracts.analysis import AnalysisViewContract
from nova_rtl.contracts.base import (
    ArtifactRef,
    MetricSet,
    StageInputHashes,
    canonical_sha256,
)
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.platform import ToolFingerprint


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def view(view_id: str, check: str, digit: str) -> AnalysisViewContract:
    metric = {
        "SETUP": "setup_wns_ns",
        "HOLD": "hold_wns_ns",
        "POWER": "power_total_uw",
    }[check]
    return AnalysisViewContract.model_validate(
        {
            "schema_version": 1,
            "analysis_view_id": view_id,
            "mode": "FUNCTIONAL",
            "check": check,
            "required": True,
            "liberty_corner": {"id": f"lib_{view_id}", "artifact_hash": hash_ref(digit)},
            "rc_corner": {"id": f"rc_{view_id}", "artifact_hash": hash_ref("a")},
            "sdc_hash": hash_ref("b"),
            "operating_condition": "LOCKED_PVT",
            "derate_policy_hash": hash_ref("c"),
            "clock_uncertainty_policy_hash": hash_ref("d"),
            "hard_limits": {metric: 0.0},
            "required_stages": (
                ["POWER_ANALYSIS"]
                if check == "POWER"
                else ["OPENSTA_FULL", "OPENROAD_PHYSICAL"]
            ),
            "power_activity_contract_id": (
                "power_activity_main" if check == "POWER" else None
            ),
        }
    )


def stage_result(
    contract: AnalysisViewContract,
    stage: str,
    *,
    wns: float,
    hash_override: str | None = None,
    status: str = "PASS",
    diagnostic_code: str = "INFRASTRUCTURE_TEST_FAILURE",
) -> StageResult:
    hashes = StageInputHashes(
        rtl_snapshot=hash_ref("1"),
        design_contract=hash_ref("2"),
        constraints=contract.sdc_hash,
        constraint_binding=hash_ref("3"),
        analysis_view=hash_override or canonical_sha256(contract),
        power_activity=hash_ref("e") if contract.check == "POWER" else None,
        platform_lock=hash_ref("4"),
        tool_recipe=hash_ref("5"),
        formal_model=None,
        parent_stage_result=None,
        extensions={},
    )
    metrics = MetricSet(
        analysis_view_id=contract.analysis_view_id,
        setup_wns_ns=wns if contract.check == "SETUP" else None,
        setup_tns_ns=wns * 2 if contract.check == "SETUP" else None,
        hold_wns_ns=wns if contract.check == "HOLD" else None,
        hold_tns_ns=wns * 2 if contract.check == "HOLD" else None,
        failing_endpoints=1,
        critical_path_delay_ns=2.0,
        estimated_fmax_mhz=500.0,
        mapped_area_um2=1000.0,
        physical_area_um2=1100.0,
        cell_count=500,
        register_count=100,
        buffer_count=20,
        power_total_uw=wns if contract.check == "POWER" else None,
        wirelength_um=5000.0,
        congestion_overflow=0.0,
        runtime_ms=100,
        missing_metric_reasons={
            name: "not applicable to this analysis view"
            for name, value in {
                "setup_wns_ns": wns if contract.check == "SETUP" else None,
                "setup_tns_ns": wns * 2 if contract.check == "SETUP" else None,
                "hold_wns_ns": wns if contract.check == "HOLD" else None,
                "hold_tns_ns": wns * 2 if contract.check == "HOLD" else None,
                "power_total_uw": wns if contract.check == "POWER" else None,
            }.items()
            if value is None
        },
    )
    result_id = f"result_{contract.analysis_view_id}_{stage.lower()}"
    raw_artifact = ArtifactRef(
        artifact_id=f"artifact_{contract.analysis_view_id}_{stage.lower()}",
        uri=f"artifact://results/{contract.analysis_view_id}/{stage.lower()}.log",
        sha256=hash_ref("e"),
        media_type="text/plain",
        size_bytes=10,
        created_at="2026-08-21T06:30:00Z",
        producer_stage_result_id=result_id,
        classification="INTERNAL",
    )
    return StageResult(
        stage_result_id=result_id,
        run_id="run_001",
        candidate_id="cand_001",
        analysis_view_id=contract.analysis_view_id,
        stage=stage,
        status=status,
        tool_fingerprint=ToolFingerprint(
            tool_id="opensta",
            executable="/opt/nova/bin/sta",
            version="OpenSTA fixture",
            version_args=("-version",),
            executable_sha256=hash_ref("a"),
            build_hash=hash_ref("b"),
            adapter_version="opensta-adapter-v1",
            container_digest=None,
        ),
        input_hashes=hashes,
        metrics=metrics,
        diagnostics=(
            ()
            if status == "PASS"
            else (
                {
                    "code": diagnostic_code,
                    "severity": "ERROR",
                    "message": "fixture non-pass result",
                    "evidence_refs": (
                        ()
                        if diagnostic_code.startswith("INFRASTRUCTURE_")
                        else (raw_artifact.artifact_id,)
                    ),
                },
            )
        ),
        raw_artifacts=(raw_artifact,),
        started_at="2026-08-21T06:30:00Z",
        ended_at="2026-08-21T06:30:01Z",
    )


def complete_results(
    contracts: tuple[AnalysisViewContract, ...]
) -> dict[str, dict[str, StageResult]]:
    values = {
        "setup_slow": -0.20,
        "setup_typ": -0.05,
        "hold_fast": -0.12,
        "hold_typ": 0.02,
    }
    return {
        contract.analysis_view_id: {
            stage: stage_result(contract, stage, wns=values[contract.analysis_view_id])
            for stage in contract.required_stages
        }
        for contract in contracts
    }


@pytest.fixture
def contracts() -> tuple[AnalysisViewContract, ...]:
    return (
        view("setup_slow", "SETUP", "6"),
        view("setup_typ", "SETUP", "7"),
        view("hold_fast", "HOLD", "8"),
        view("hold_typ", "HOLD", "9"),
    )


def test_aggregation_selects_worst_complete_setup_and_hold_views(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    aggregated = aggregate_required_views(complete_results(contracts), contracts)

    assert aggregated.setup_wns_ns == -0.20
    assert aggregated.setup_view_id == "setup_slow"
    assert aggregated.hold_wns_ns == -0.12
    assert aggregated.hold_view_id == "hold_fast"
    assert set(aggregated.per_view_metrics) == {
        "setup_slow",
        "setup_typ",
        "hold_fast",
        "hold_typ",
    }


def test_required_power_view_is_validated_and_aggregated(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    power = view("power_typ", "POWER", "f")
    all_contracts = (*contracts, power)
    results = complete_results(contracts)
    results[power.analysis_view_id] = {
        "POWER_ANALYSIS": stage_result(power, "POWER_ANALYSIS", wns=420.0)
    }

    aggregated = aggregate_required_views(results, all_contracts)
    assert aggregated.power_total_uw == 420.0
    assert aggregated.power_view_id == "power_typ"

    results.pop("power_typ")
    with pytest.raises(IncompleteRequiredViewError, match="power_typ"):
        aggregate_required_views(results, all_contracts)


def test_worst_view_requires_complete_setup_and_hold(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    results = complete_results(contracts)
    results["hold_fast"].pop("OPENROAD_PHYSICAL")

    with pytest.raises(IncompleteRequiredViewError, match="hold_fast.*OPENROAD_PHYSICAL"):
        aggregate_required_views(results, contracts)


def test_aggregation_rejects_failed_missing_metric_and_mismatched_view_identity(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    failed = complete_results(contracts)
    failed["setup_slow"]["OPENSTA_FULL"] = stage_result(
        contracts[0], "OPENSTA_FULL", wns=-0.2, status="INCONCLUSIVE"
    )
    with pytest.raises(IncompleteRequiredViewError, match="neither PASS"):
        aggregate_required_views(failed, contracts)

    missing = complete_results(contracts)
    missing_result = missing["setup_slow"]["OPENSTA_FULL"]
    missing["setup_slow"]["OPENSTA_FULL"] = missing_result.model_copy(
        update={
            "metrics": missing_result.metrics.model_copy(
                update={"setup_wns_ns": None}
            )
        }
    )
    with pytest.raises(IncompleteRequiredViewError, match="setup_wns_ns"):
        aggregate_required_views(missing, contracts)

    mismatched = complete_results(contracts)
    mismatched["setup_slow"]["OPENSTA_FULL"] = stage_result(
        contracts[0], "OPENSTA_FULL", wns=-0.2, hash_override=hash_ref("f")
    )
    with pytest.raises(IncompleteRequiredViewError, match="identity"):
        aggregate_required_views(mismatched, contracts)


def test_aggregation_accepts_complete_negative_slack_as_measured_design_result(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    results = complete_results(contracts)
    results["setup_slow"]["OPENSTA_FULL"] = stage_result(
        contracts[0],
        "OPENSTA_FULL",
        wns=-0.2,
        status="FAIL",
        diagnostic_code="TIMING_SETUP_VIOLATION",
    )
    results["hold_fast"]["OPENROAD_PHYSICAL"] = stage_result(
        contracts[2],
        "OPENROAD_PHYSICAL",
        wns=-0.12,
        status="FAIL",
        diagnostic_code="PHYSICAL_TIMING_VIOLATION",
    )

    aggregated = aggregate_required_views(results, contracts)

    assert aggregated.setup_wns_ns == -0.2
    assert aggregated.hold_wns_ns == -0.12


def test_aggregation_rejects_mixed_design_identity_and_tracks_limiting_stage(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    results = complete_results(contracts)
    setup_opensta = results["setup_slow"]["OPENSTA_FULL"]
    results["setup_slow"]["OPENSTA_FULL"] = setup_opensta.model_copy(
        update={
            "metrics": setup_opensta.metrics.model_copy(
                update={"setup_wns_ns": -0.35, "setup_tns_ns": -1.2}
            )
        }
    )
    aggregated = aggregate_required_views(results, contracts)
    assert aggregated.per_view_metrics["setup_slow"].limiting_stage == "OPENSTA_FULL"

    updated = results["setup_slow"]["OPENSTA_FULL"]
    results["setup_slow"]["OPENSTA_FULL"] = updated.model_copy(
        update={
            "input_hashes": updated.input_hashes.model_copy(
                update={"rtl_snapshot": hash_ref("f")}
            )
        }
    )
    with pytest.raises(IncompleteRequiredViewError, match="immutable identity"):
        aggregate_required_views(results, contracts)


def test_aggregation_rejects_untyped_result_objects(
    contracts: tuple[AnalysisViewContract, ...],
) -> None:
    results = complete_results(contracts)
    results["setup_slow"]["OPENSTA_FULL"] = object()  # type: ignore[assignment]

    with pytest.raises(IncompleteRequiredViewError, match="typed StageResult"):
        aggregate_required_views(results, contracts)
