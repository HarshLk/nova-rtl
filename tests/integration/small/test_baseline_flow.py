"""External validation of one completed tiny-profile Phase 7 run."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.baseline.flow import inspect_baseline_run


@pytest.mark.integration
def test_tiny_baseline_is_complete_replayable_and_fully_resolvable(
    completed_run_path: Path,
) -> None:
    evidence = inspect_baseline_run(completed_run_path)

    assert evidence.profile == "tiny"
    assert evidence.expected_master_clocks == 5
    assert evidence.expected_generated_clocks == 10
    assert evidence.status == "PASS"
    assert evidence.required_views_complete
    assert evidence.raw_artifacts_resolvable
    assert evidence.resume_rerun_stage_ids == ()
    assert evidence.resume_reusable_stage_ids == evidence.stage_result_ids
    assert evidence.replay_digest_before == evidence.replay_digest_after
