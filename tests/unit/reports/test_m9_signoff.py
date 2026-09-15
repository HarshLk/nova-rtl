from __future__ import annotations

import pytest

from nova_rtl.contracts.release import M9SignoffReport
from nova_rtl.reports.signoff import assemble_m9_report


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def test_m9_report_binds_final_commit_and_every_release_artifact() -> None:
    report = assemble_m9_report(
        commit_sha="a" * 40,
        implementation_tree_hash="b" * 40,
        m8_commit_sha="c" * 40,
        m8_packet_hash=_hash("1"),
        report_bundle_hash=_hash("2"),
        replay_manifest_hash=_hash("3"),
        final_candidate_seal_hash=_hash("4"),
        ablation_hash=_hash("5"),
        frequency_sweep_hash=_hash("6"),
        acceptance_hash=_hash("7"),
        submission_bundle_hash=_hash("8"),
        gate_evidence_hash=_hash("9"),
        created_at="2026-09-15T00:00:00Z",
    )

    assert isinstance(report, M9SignoffReport)
    assert report.status == "PASS"
    assert report.report_hash.startswith("sha256:")


def test_m9_report_refuses_missing_release_hash() -> None:
    with pytest.raises(ValueError):
        assemble_m9_report(
            commit_sha="a" * 40,
            implementation_tree_hash="b" * 40,
            m8_commit_sha="c" * 40,
            m8_packet_hash="",
            report_bundle_hash=_hash("2"),
            replay_manifest_hash=_hash("3"),
            final_candidate_seal_hash=_hash("4"),
            ablation_hash=_hash("5"),
            frequency_sweep_hash=_hash("6"),
            acceptance_hash=_hash("7"),
            submission_bundle_hash=_hash("8"),
            gate_evidence_hash=_hash("9"),
            created_at="2026-09-15T00:00:00Z",
        )
