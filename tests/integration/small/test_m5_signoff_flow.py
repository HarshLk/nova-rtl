from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.search.signoff import run_m5_signoff, verify_m5_signoff

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_m5_signoff_reconstructs_search_and_historical_dependencies(
    search_bundle_path: Path,
    m4_packet_path: Path,
    m3_packet_path: Path,
) -> None:
    report_path, generated = run_m5_signoff(
        search_bundle_path,
        m4_packet=m4_packet_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )
    first = verify_m5_signoff(
        report_path,
        m4_packet=m4_packet_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )
    second = verify_m5_signoff(
        report_path,
        m4_packet=m4_packet_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert generated.status == "PASS"
    assert generated.transform_operations == (
        "BALANCE_BOOLEAN_TREE",
        "FACTOR_COMMON_PREDICATE",
        "FSM_DECODE_RESTRUCTURE",
        "RESTRUCTURE_PRIORITY_MUX",
    )
    assert generated.valid_negative_candidate_ids
    assert generated.budget_compliance is True
    assert first == generated
    assert second.report_hash == first.report_hash
