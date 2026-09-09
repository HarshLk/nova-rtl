from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.baseline.signoff import run_m2_signoff, verify_m2_signoff
from nova_rtl.evidence.execution import analyze_evidence_run

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_full_m2_signoff_generates_and_verifies_from_raw_evidence(
    completed_run_path: Path,
    calibration_directory_path: Path,
    m1_packet_path: Path,
) -> None:
    report_path, generated = run_m2_signoff(
        completed_run_path,
        calibration_directory_path,
        m1_packet_path,
        repository_root=PROJECT_ROOT,
    )
    verified = verify_m2_signoff(
        report_path,
        calibration_directory=calibration_directory_path,
        m1_packet=m1_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert generated == verified
    assert generated.status == "PASS"
    assert generated.expected_master_clocks == 5
    assert generated.expected_generated_clocks == 105


@pytest.mark.integration
def test_m2_signoff_remains_verifiable_after_m3_extends_the_live_run(
    completed_run_path: Path,
    calibration_directory_path: Path,
    m1_packet_path: Path,
) -> None:
    report_path, generated = run_m2_signoff(
        completed_run_path,
        calibration_directory_path,
        m1_packet_path,
        repository_root=PROJECT_ROOT,
    )
    frozen_index = completed_run_path / "m2-run-index.json"
    frozen_ledger = completed_run_path / "m2-experiment-ledger.sqlite3"
    assert frozen_index.is_file()
    assert frozen_ledger.is_file()

    analyze_evidence_run(
        completed_run_path,
        requested_stages=("evidence", "opportunities"),
    )
    verified = verify_m2_signoff(
        report_path,
        calibration_directory=calibration_directory_path,
        m1_packet=m1_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert verified == generated
