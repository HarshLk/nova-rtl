from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.optimization.signoff import run_m4_signoff, verify_m4_signoff

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_m4_signoff_reconstructs_candidate_and_historical_dependencies(
    candidate_bundle_path: Path,
    m3_packet_path: Path,
) -> None:
    report_path, generated = run_m4_signoff(
        candidate_bundle_path,
        m3_packet_path,
        repository_root=PROJECT_ROOT,
    )
    first = verify_m4_signoff(
        report_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )
    second = verify_m4_signoff(
        report_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert generated.status == "PASS"
    assert generated.candidate_classification in {
        "VALID_NEGATIVE_RESULT",
        "FEASIBLE_PARETO",
        "SELECTED",
    }
    assert set(generated.formal_stage_result_hashes) == {
        "stage_m4_strict_final",
        "stage_m4_strict_prephysical",
    }
    assert len(generated.gate_event_ids) == 18
    assert first == generated
    assert second.report_hash == first.report_hash
