from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from nova_rtl.adapters.base import AdapterParseContext, ParsedAdapterResult
from nova_rtl.adapters.eqy import parse_report as parse_eqy
from nova_rtl.adapters.openroad import parse_report as parse_openroad
from nova_rtl.adapters.opensta import parse_report as parse_opensta
from nova_rtl.adapters.sby import parse_report as parse_sby
from nova_rtl.adapters.simulation import parse_report as parse_simulation
from nova_rtl.adapters.yosys import parse_report as parse_yosys

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/adapters"
Parser = Callable[..., ParsedAdapterResult]


@pytest.mark.parametrize(
    ("parser", "fixture", "exit_code", "stage", "analysis_view_id", "status"),
    [
        (parse_yosys, "yosys/pass.rpt", 0, "YOSYS_SYNTH", None, "PASS"),
        (
            parse_yosys,
            "yosys/malformed.rpt",
            0,
            "YOSYS_SYNTH",
            None,
            "INFRASTRUCTURE_ERROR",
        ),
        (
            parse_opensta,
            "opensta/pass_setup.rpt",
            0,
            "OPENSTA_FULL",
            "func_setup_slow",
            "PASS",
        ),
        (
            parse_opensta,
            "opensta/setup_violation.rpt",
            1,
            "OPENSTA_FULL",
            "func_setup_slow",
            "FAIL",
        ),
        (
            parse_opensta,
            "opensta/hold_violation.rpt",
            1,
            "OPENSTA_FULL",
            "func_hold_fast",
            "FAIL",
        ),
        (
            parse_opensta,
            "opensta/missing_summary.rpt",
            0,
            "OPENSTA_FULL",
            "func_setup_slow",
            "INFRASTRUCTURE_ERROR",
        ),
        (
            parse_opensta,
            "opensta/malformed_number.rpt",
            0,
            "OPENSTA_FULL",
            "func_setup_slow",
            "INFRASTRUCTURE_ERROR",
        ),
        (
            parse_openroad,
            "openroad/pass.rpt",
            0,
            "OPENROAD_ROUTED",
            "func_setup_slow",
            "PASS",
        ),
        (
            parse_openroad,
            "openroad/crash.log",
            134,
            "OPENROAD_ROUTED",
            "func_setup_slow",
            "INFRASTRUCTURE_ERROR",
        ),
        (parse_eqy, "eqy/pass.log", 0, "EQY_SMOKE", None, "PASS"),
        (parse_eqy, "eqy/fail.log", 1, "EQY_SMOKE", None, "FAIL"),
        (parse_eqy, "eqy/unknown.log", 4, "EQY_SMOKE", None, "INCONCLUSIVE"),
        (
            parse_eqy,
            "eqy/missing.log",
            0,
            "EQY_SMOKE",
            None,
            "INFRASTRUCTURE_ERROR",
        ),
        (parse_sby, "sby/pass.log", 0, "FORMAL_EQUIVALENCE", None, "PASS"),
        (parse_sby, "sby/fail.log", 1, "FORMAL_EQUIVALENCE", None, "FAIL"),
        (
            parse_sby,
            "sby/unknown.log",
            4,
            "FORMAL_EQUIVALENCE",
            None,
            "INCONCLUSIVE",
        ),
        (parse_simulation, "simulation/pass.log", 0, "SIMULATION", None, "PASS"),
        (parse_simulation, "simulation/fail.log", 1, "SIMULATION", None, "FAIL"),
        (
            parse_simulation,
            "simulation/crash.log",
            134,
            "SIMULATION",
            None,
            "INFRASTRUCTURE_ERROR",
        ),
    ],
)
def test_adapter_status_is_semantic_not_exit_code(
    parser: Parser,
    fixture: str,
    exit_code: int,
    stage: str,
    analysis_view_id: str | None,
    status: str,
) -> None:
    context = AdapterParseContext(
        stage=stage,
        analysis_view_id=analysis_view_id,
        runtime_ms=17,
        evidence_artifact_id="adapter_report_evidence",
    )

    result = parser(
        (FIXTURES / fixture).read_text(encoding="utf-8"),
        exit_code=exit_code,
        context=context,
    )

    assert result.status == status
    assert result.metrics.runtime_ms == 17
    assert (result.status == "PASS") == (not result.diagnostics)


def test_negative_slack_is_a_design_failure_with_preserved_metrics() -> None:
    context = AdapterParseContext(
        stage="OPENSTA_FULL",
        analysis_view_id="func_setup_slow",
        runtime_ms=25,
        evidence_artifact_id="opensta_report_evidence",
    )

    result = parse_opensta(
        (FIXTURES / "opensta/setup_violation.rpt").read_text(encoding="utf-8"),
        exit_code=1,
        context=context,
    )

    assert result.status == "FAIL"
    assert result.metrics.setup_wns_ns == -0.18
    assert result.metrics.setup_tns_ns == -12.4
    assert result.metrics.failing_endpoints == 83
    assert result.diagnostics[0].code == "TIMING_SETUP_VIOLATION"


@pytest.mark.parametrize(
    ("check", "expected_status"),
    [("SETUP", "PASS"), ("HOLD", "FAIL")],
)
def test_openroad_judges_only_the_requested_timing_check(
    check: str,
    expected_status: str,
) -> None:
    context = AdapterParseContext(
        stage="OPENROAD_PHYSICAL",
        analysis_view_id=f"asap7_{check.lower()}",
        runtime_ms=25,
        evidence_artifact_id="openroad_report_evidence",
    )
    report = f"""
NOVA_OPENROAD_SUMMARY
check: {check}
setup_wns_ns: 6.981
setup_tns_ns: 0.0
hold_wns_ns: -0.0956
hold_tns_ns: -5.005
failing_endpoints: {0 if check == "SETUP" else 1}
physical_area_um2: 1694.1
cell_count: 18032
register_count: 1127
buffer_count: 0
wirelength_um: 0.0
congestion_overflow: 0.0
power_total_uw: 0.0
NOVA_OPENROAD_END
"""

    result = parse_openroad(report, exit_code=0, context=context)

    assert result.status == expected_status
    assert result.metrics.setup_wns_ns == 6.981
    assert result.metrics.hold_wns_ns == -0.0956
    if check == "HOLD":
        assert result.diagnostics[0].code == "PHYSICAL_TIMING_VIOLATION"


def test_openroad_congestion_is_not_misclassified_as_timing_violation() -> None:
    context = AdapterParseContext(
        stage="OPENROAD_PHYSICAL",
        analysis_view_id="asap7_setup",
        runtime_ms=25,
        evidence_artifact_id="openroad_report_evidence",
    )
    report = """
NOVA_OPENROAD_SUMMARY
check: SETUP
setup_wns_ns: 0.2
setup_tns_ns: 0.0
hold_wns_ns: 0.1
hold_tns_ns: 0.0
failing_endpoints: 0
physical_area_um2: 1694.1
cell_count: 18032
register_count: 1127
buffer_count: 0
wirelength_um: 100.0
congestion_overflow: 0.5
power_total_uw: 0.0
NOVA_OPENROAD_END
"""

    result = parse_openroad(report, exit_code=0, context=context)

    assert result.status == "FAIL"
    assert result.diagnostics[0].code == "PHYSICAL_CONGESTION_VIOLATION"


@pytest.mark.parametrize(
    "fixture",
    ["opensta/missing_summary.rpt", "opensta/malformed_number.rpt"],
)
def test_missing_sections_and_malformed_numbers_fail_closed(fixture: str) -> None:
    context = AdapterParseContext(
        stage="OPENSTA_FULL",
        analysis_view_id="func_setup_slow",
        runtime_ms=25,
        evidence_artifact_id="opensta_report_evidence",
    )

    result = parse_opensta(
        (FIXTURES / fixture).read_text(encoding="utf-8"),
        exit_code=0,
        context=context,
    )

    assert result.status == "INFRASTRUCTURE_ERROR"
    assert result.diagnostics[0].code in {
        "INFRASTRUCTURE_MALFORMED_REPORT",
        "INFRASTRUCTURE_MISSING_REPORT_SECTION",
    }


def test_yosys_parser_accepts_complete_native_mapped_statistics() -> None:
    context = AdapterParseContext(
        stage="YOSYS_SYNTH",
        analysis_view_id=None,
        runtime_ms=31,
        evidence_artifact_id="yosys_native_report",
    )
    report = """
=== design hierarchy ===
     6239  849.883 cells
      999  378.701   DFFASRHQNx1_ASAP7_75t_R
      128   37.325   DFFHQNx1_ASAP7_75t_R
   Chip area for top module '\\nebula_top': 849.882780
"""

    result = parse_yosys(report, exit_code=0, context=context)

    assert result.status == "PASS"
    assert result.metrics.cell_count == 6239
    assert result.metrics.register_count == 1127
    assert result.metrics.mapped_area_um2 == 849.88278
