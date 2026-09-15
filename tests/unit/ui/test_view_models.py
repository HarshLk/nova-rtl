from __future__ import annotations

from pathlib import Path

from nova_rtl.contracts.release import EvidenceClaim
from nova_rtl.reports.bundle import build_report_bundle
from nova_rtl.reports.replay import seal_offline_replay
from nova_rtl.ui.view_models import (
    AUTHORITY_COLORS,
    VIEW_ORDER,
    build_view_model,
    build_view_model_from_replay,
)


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def test_seven_views_are_typed_projections_and_live_replay_are_identical(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    raw = evidence / "timing.rpt"
    raw.write_text("wns=-0.125\n", encoding="utf-8")
    claim = EvidenceClaim(
        claim_id="claim_timing",
        label="Worst setup slack",
        value="-0.125 ns",
        authority="MEASURED_EDA",
        artifact_ids=("artifact_timing",),
        evidence_ids=("artifact_timing",),
        comparison_identity_hashes={"analysis_view_set": _hash("a")},
    )
    report_path, bundle = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths={"artifact_timing": raw},
        claims=(claim,),
        output_directory=tmp_path / "report",
    )
    replay_path, _ = seal_offline_replay(
        report_path,
        evidence_root=evidence,
        output_directory=tmp_path / "replay",
        ledger_hash=_hash("b"),
    )

    live = build_view_model(bundle)
    replay = build_view_model_from_replay(replay_path.parent)

    assert live == replay
    assert tuple(view.view_id for view in live.views) == VIEW_ORDER
    assert live.views[0].cards[0].color == AUTHORITY_COLORS["MEASURED_EDA"]
    assert live.views[0].cards[0].evidence_ids == ("artifact_timing",)


def test_event_sequence_changes_snapshot_identity_not_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    raw = evidence / "formal.rpt"
    raw.write_text("PASS\n", encoding="utf-8")
    claim = EvidenceClaim(
        claim_id="claim_formal",
        label="Strict proof",
        value="PASS",
        authority="TRUTH_GATE",
        artifact_ids=("artifact_formal",),
        evidence_ids=("artifact_formal",),
        comparison_identity_hashes={"formal_model": _hash("c")},
    )
    _, bundle = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths={"artifact_formal": raw},
        claims=(claim,),
        output_directory=tmp_path / "report",
    )

    first = build_view_model(bundle, sequence=1)
    second = build_view_model(bundle, sequence=2)

    assert first.model_hash != second.model_hash
    assert first.views == second.views
