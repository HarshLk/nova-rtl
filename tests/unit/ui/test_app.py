from __future__ import annotations

from pathlib import Path

from nova_rtl.contracts.release import EvidenceClaim
from nova_rtl.reports.bundle import build_report_bundle
from nova_rtl.ui.app import render_text_dashboard
from nova_rtl.ui.view_models import VIEW_ORDER, build_view_model


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def test_text_dashboard_exposes_all_views_authority_and_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "timing.rpt"
    evidence.write_text("wns=-0.125\n", encoding="utf-8")
    claim = EvidenceClaim(
        claim_id="claim_timing",
        label="Worst setup slack",
        value="-0.125 ns",
        authority="MEASURED_EDA",
        artifact_ids=("artifact_timing",),
        evidence_ids=("artifact_timing",),
        comparison_identity_hashes={"analysis_view_set": _hash("a")},
    )
    _, bundle = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths={"artifact_timing": evidence},
        claims=(claim,),
        output_directory=tmp_path / "report",
    )

    rendered = render_text_dashboard(build_view_model(bundle))

    assert all(view_id in rendered for view_id in VIEW_ORDER)
    assert "MEASURED_EDA [blue]" in rendered
    assert "artifact_timing" in rendered

