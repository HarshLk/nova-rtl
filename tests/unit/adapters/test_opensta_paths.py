from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.adapters.opensta import CriticalPathParseError, parse_critical_paths

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/adapters/opensta"
CLOCK_IDS = {
    "clk_compute": "clk_master_compute",
    "clk_scheduler": "clk_master_scheduler",
}


def parse(text: str):  # type: ignore[no-untyped-def]
    return parse_critical_paths(
        text,
        candidate_id="baseline",
        analysis_view_id="asap7_setup",
        raw_report_artifact_id="stage_opensta_asap7_setup_stdout",
        clock_ids_by_name=CLOCK_IDS,
    )


def test_opensta_paths_are_canonical_and_negative_slack_is_evidence() -> None:
    report = (FIXTURES / "critical_paths_setup.rpt").read_text(encoding="utf-8")

    records = parse(report)

    assert len(records) == 2
    scheduler = next(record for record in records if record.path_group == "clk_scheduler")
    assert scheduler.launch_clock_id == "clk_master_scheduler"
    assert scheduler.capture_clock_id == "clk_master_scheduler"
    assert scheduler.startpoint == "cell:u_scheduler/start_reg"
    assert scheduler.endpoint == "cell:u_scheduler/end_reg"
    assert scheduler.arrival_ns == 1.2
    assert scheduler.required_ns == 1.0
    assert scheduler.slack_ns == -0.2
    assert scheduler.cell_delay_ns == 1.2
    assert scheduler.net_delay_ns == 0.0
    assert scheduler.logic_depth == 3
    assert scheduler.max_fanout == 0
    assert scheduler.object_sequence == (
        "pin:u_scheduler/start_reg/Q",
        "pin:u_scheduler/priority_and/Y",
        "pin:u_scheduler/priority_mux/Y",
        "pin:u_scheduler/end_reg/D",
    )
    assert scheduler.source_span_refs == ()
    assert scheduler.raw_report_artifact_id == "stage_opensta_asap7_setup_stdout"


def test_opensta_path_identity_and_order_ignore_report_order() -> None:
    report = (FIXTURES / "critical_paths_setup.rpt").read_text(encoding="utf-8")
    first_block, remainder = report.split("\n\nStartpoint:", maxsplit=1)
    second_block, summary = remainder.split("\nNOVA_OPENSTA_SUMMARY", maxsplit=1)
    reversed_report = (
        f"Startpoint:{second_block}\n\n{first_block}\nNOVA_OPENSTA_SUMMARY{summary}"
    )

    assert parse(reversed_report) == parse(report)


def test_opensta_paths_fail_closed_on_malformed_arithmetic_or_clock() -> None:
    malformed = (FIXTURES / "critical_paths_malformed.rpt").read_text(encoding="utf-8")
    with pytest.raises(CriticalPathParseError, match="slack arithmetic"):
        parse(malformed)

    valid = (FIXTURES / "critical_paths_setup.rpt").read_text(encoding="utf-8")
    with pytest.raises(CriticalPathParseError, match="unresolved launch clock"):
        parse_critical_paths(
            valid,
            candidate_id="baseline",
            analysis_view_id="asap7_setup",
            raw_report_artifact_id="stage_opensta_asap7_setup_stdout",
            clock_ids_by_name={"clk_compute": "clk_master_compute"},
        )


def test_opensta_paths_accept_six_decimal_report_rounding() -> None:
    report = (FIXTURES / "critical_paths_setup.rpt").read_text(encoding="utf-8")
    _, compute = report.split("Startpoint: u_compute/start_reg", maxsplit=1)
    compute = compute.replace("0.800000   data arrival time", "2.657319   data arrival time")
    compute = compute.replace("1.000000   data required time", "21.863960   data required time")
    compute = compute.replace("-0.800000   data arrival time", "-2.657319   data arrival time")
    compute = compute.replace("0.200000   slack (MET)", "19.206642   slack (MET)")
    report = "Startpoint: u_compute/start_reg" + compute

    record = parse(report)[0]

    assert record.required_ns == record.arrival_ns + record.slack_ns


def test_opensta_paths_accept_inline_port_descriptions() -> None:
    report = (FIXTURES / "critical_paths_setup.rpt").read_text(encoding="utf-8")
    report = report.replace(
        "Startpoint: u_compute/start_reg\n"
        "            (rising edge-triggered flip-flop clocked by clk_compute)",
        "Startpoint: workload_phase[2] (input port clocked by clk_compute)",
        1,
    )

    record = next(item for item in parse(report) if item.path_group == "clk_compute")

    assert record.startpoint == "port:workload_phase[2]"


def test_opensta_paths_require_at_least_one_complete_path() -> None:
    with pytest.raises(CriticalPathParseError, match="no complete timing paths"):
        parse("NOVA_OPENSTA_SUMMARY\ncheck: SETUP\nNOVA_OPENSTA_END\n")
