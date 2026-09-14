from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.recovery.signoff import verify_m7_signoff

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_m7_packet_is_independently_reconstructable(
    path_migration_report_path: Path,
    m6_packet_path: Path,
    m5_packet_path: Path,
    m4_packet_path: Path,
    m3_packet_path: Path,
) -> None:
    report = verify_m7_signoff(
        path_migration_report_path.parent / "m7-signoff.json",
        path_migration_report=path_migration_report_path,
        m6_packet=m6_packet_path,
        m5_packet=m5_packet_path,
        m4_packet=m4_packet_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert report.status == "PASS"
    assert report.deterministic_only
    assert report.failure_family == "CRITICAL_PATH_MIGRATION"
