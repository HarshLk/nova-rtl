from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.contracts.release import EvidenceClaim
from nova_rtl.reports.bundle import (
    ReportIntegrityError,
    build_report_bundle,
    verify_report_bundle,
)


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def _claim() -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="claim_setup",
        label="Worst setup slack",
        value="-0.125 ns",
        authority="MEASURED_EDA",
        artifact_ids=("artifact_setup",),
        evidence_ids=("artifact_setup",),
        comparison_identity_hashes={"analysis_view_set": _hash("a")},
    )


def test_report_bundle_is_deterministic_and_every_claim_resolves(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "setup.rpt").write_text("wns=-0.125\n", encoding="utf-8")
    (evidence / "hold.rpt").write_text("wns=0.010\n", encoding="utf-8")
    paths = {
        "artifact_setup": evidence / "setup.rpt",
        "artifact_hold": evidence / "hold.rpt",
    }

    first_path, first = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths=paths,
        claims=(_claim(),),
        output_directory=tmp_path / "first",
    )
    second_path, second = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths=paths,
        claims=(_claim(),),
        output_directory=tmp_path / "second",
    )

    assert first.bundle_hash == second.bundle_hash
    assert verify_report_bundle(first_path, evidence_root=evidence) == first
    assert verify_report_bundle(second_path, evidence_root=evidence) == second
    assert len(first.sections) == 15


def test_report_verification_rejects_corrupt_raw_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    report = evidence / "setup.rpt"
    report.write_text("wns=-0.125\n", encoding="utf-8")
    bundle_path, _ = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths={"artifact_setup": report},
        claims=(_claim(),),
        output_directory=tmp_path / "bundle",
    )

    report.write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(ReportIntegrityError, match="artifact_setup"):
        verify_report_bundle(bundle_path, evidence_root=evidence)


def test_report_creation_rejects_missing_claim_artifact(tmp_path: Path) -> None:
    with pytest.raises(ReportIntegrityError, match="artifact_setup"):
        build_report_bundle(
            run_id="run_full",
            selected_candidate_id="candidate_primary",
            evidence_paths={},
            claims=(_claim(),),
            output_directory=tmp_path / "bundle",
        )
