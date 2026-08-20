from __future__ import annotations

from pathlib import Path

from nova_rtl.platform.lock import (
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)

POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "config/platform/platform-selection-policy.yaml"
)


def test_production_asap7_policy_is_strict_and_pins_two_distinct_real_corners() -> None:
    policy = load_platform_selection_policy(POLICY_PATH)

    assert policy.platform_id == "asap7"
    assert policy.orfs_commit == "4c06bcb2466996a90d31101d85d705ad015950bc"
    assert policy.setup_corner.corner_id == "asap7_wc"
    assert policy.hold_corner.corner_id == "asap7_bc"
    assert policy.setup_corner.liberty_files != policy.hold_corner.liberty_files
    assert len(policy.setup_corner.liberty_files) == 5
    assert len(policy.hold_corner.liberty_files) == 5
    assert policy.rc_config == "flow/platforms/asap7/setRC.tcl"
    assert policy.redistribution_status == "REVIEW_REQUIRED"
    assert selection_policy_content_identity_hash(policy) == (
        "sha256:73ea1f3114304a39c89b7a6f1917a19bb2c8e67dd08583d2da45e1bae4416b97"
    )
