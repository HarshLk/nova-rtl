from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.evidence.signoff import run_m3_signoff, verify_m3_signoff


@pytest.mark.integration
def test_full_m3_signoff_recomputes_and_verifies_all_evidence(
    completed_run_path: Path,
    m2_packet_path: Path,
) -> None:
    report_path, generated = run_m3_signoff(
        completed_run_path,
        m2_packet_path,
        repository_root=Path.cwd(),
    )
    first = verify_m3_signoff(
        report_path,
        m2_packet=m2_packet_path,
        repository_root=Path.cwd(),
    )
    second = verify_m3_signoff(
        report_path,
        m2_packet=m2_packet_path,
        repository_root=Path.cwd(),
    )

    assert generated.status == "PASS"
    assert generated.profile == "full"
    assert generated.expected_master_clocks == 5
    assert generated.expected_generated_clocks == 105
    assert generated.path_count > 0
    assert generated.cluster_count > 0
    assert generated.opportunity_count > 0
    assert generated.editable_opportunity_count > 0
    assert first == generated
    assert second.report_hash == first.report_hash
