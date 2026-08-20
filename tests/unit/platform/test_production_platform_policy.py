from __future__ import annotations

from pathlib import Path

import yaml

from nova_rtl.contracts.platform import PlatformAnalysisViews
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import (
    hash_file,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = PROJECT_ROOT / "config/platform/platform-selection-policy.yaml"
LOCK_PATH = PROJECT_ROOT / "config/platform/platform.lock.yaml"
VIEWS_PATH = PROJECT_ROOT / "config/analysis/views.yaml"
MANIFEST_PATH = PROJECT_ROOT / "config/platform/toolchain-sources.json"


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


def test_committed_lock_and_views_are_bound_to_reviewed_provenance() -> None:
    policy = load_platform_selection_policy(POLICY_PATH)
    manifest = load_toolchain_source_manifest(MANIFEST_PATH)
    lock = load_platform_lock(LOCK_PATH)
    views = PlatformAnalysisViews.model_validate(
        yaml.safe_load(VIEWS_PATH.read_text(encoding="utf-8"))
    )

    assert lock.selection_policy_hash == selection_policy_content_identity_hash(policy)
    assert lock.source_manifest_hash == manifest.content_identity_hash
    assert lock.orfs_commit == policy.orfs_commit
    assert views.platform_id == lock.platform_id
    assert views.platform_lock_hash == hash_file(LOCK_PATH)
    assert views.views[0].liberty_corner_id == lock.setup_corner.corner_id
    assert views.views[1].liberty_corner_id == lock.hold_corner.corner_id
    assert views.views[0].liberty_artifact_hashes == tuple(
        artifact.sha256 for artifact in lock.setup_corner.liberty_files
    )
    assert views.views[1].liberty_artifact_hashes == tuple(
        artifact.sha256 for artifact in lock.hold_corner.liberty_files
    )
