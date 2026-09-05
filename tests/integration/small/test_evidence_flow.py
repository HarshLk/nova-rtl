from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.evidence import execution as evidence_execution
from nova_rtl.evidence.execution import analyze_evidence_run


@pytest.mark.integration
def test_m3_evidence_flow_is_complete_resumable_and_replayable(
    completed_run_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = analyze_evidence_run(
        completed_run_path,
        requested_stages=("evidence", "opportunities"),
    )

    def reject_reconstruction(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("a valid M3 cache hit must not reconstruct evidence")

    for name in (
        "parse_critical_paths",
        "build_source_map",
        "enrich_critical_paths",
        "build_evidence_graph",
        "cluster_paths",
        "rank_opportunities",
    ):
        monkeypatch.setattr(evidence_execution, name, reject_reconstruction)

    second = analyze_evidence_run(
        completed_run_path,
        requested_stages=("opportunities", "evidence"),
    )

    assert first.status == "PASS"
    assert first.path_count > 0
    assert first.cluster_count > 0
    assert first.opportunity_count > 0
    assert first.editable_opportunity_count > 0
    assert first.replay_digest_before == first.replay_digest_after
    assert second.evidence_snapshot_hash == first.evidence_snapshot_hash
    assert second.ranking_hash == first.ranking_hash
    assert second.reused_stage_result_ids == (
        "stage_evidence_graph",
        "stage_opportunity_formation",
    )
    assert second.replay_digest_before == first.replay_digest_after
    assert second.replay_digest_before == second.replay_digest_after
