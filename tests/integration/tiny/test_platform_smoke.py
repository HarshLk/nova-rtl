from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.platform.smoke import PlatformSmokeRequest, run_platform_smoke

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = PROJECT_ROOT / "config/platform/toolchain-sources.json"
POLICY_PATH = PROJECT_ROOT / "config/platform/platform-selection-policy.yaml"
SMOKE_RTL = Path(__file__).resolve().parent / "rtl/two_flop_smoke.sv"
SMOKE_SDC = Path(__file__).resolve().parent / "constraints/two_flop_smoke.sdc"


@pytest.mark.integration
def test_locked_asap7_two_view_smoke_reaches_openroad_cts(
    platform_lock_path: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "smoke"

    report = run_platform_smoke(
        PlatformSmokeRequest(
            project_root=PROJECT_ROOT,
            toolchain_manifest=MANIFEST_PATH,
            selection_policy=POLICY_PATH,
            platform_lock=platform_lock_path,
            rtl=SMOKE_RTL,
            constraints=SMOKE_SDC,
            output_directory=output,
        )
    )

    assert report.status == "PASS"
    assert report.platform_id == "asap7"
    assert report.setup_view.corner_id == "asap7_wc"
    assert report.setup_view.path_type == "max"
    assert report.hold_view.corner_id == "asap7_bc"
    assert report.hold_view.path_type == "min"
    assert report.setup_view.slack_ps >= 0
    assert report.hold_view.slack_ps >= 0
    assert (output / report.synthesis_database.relative_path).stat().st_size > 0
    assert (output / report.cts_database.relative_path).stat().st_size > 0
    assert (output / "smoke-report.json").stat().st_size > 0
