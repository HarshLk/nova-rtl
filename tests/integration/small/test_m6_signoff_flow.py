from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.optimization.planner_signoff import verify_m6_signoff

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_m6_signoff_packet_is_independently_reconstructable(
    planner_run_path: Path,
    m5_packet_path: Path,
    m4_packet_path: Path,
    m3_packet_path: Path,
) -> None:
    report = verify_m6_signoff(
        planner_run_path.parent / "m6-signoff.json",
        planner_run=planner_run_path,
        m5_packet=m5_packet_path,
        m4_packet=m4_packet_path,
        m3_packet=m3_packet_path,
        repository_root=PROJECT_ROOT,
    )

    assert report.status == "PASS"
    assert report.fallback_count >= 1
    assert report.validated_planner_modes == ("HEURISTIC", "SINGLE_AGENT")
