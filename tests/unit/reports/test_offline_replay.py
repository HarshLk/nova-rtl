from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from nova_rtl.contracts.release import EvidenceClaim
from nova_rtl.reports.bundle import build_report_bundle
from nova_rtl.reports.replay import (
    OfflineReplayError,
    seal_offline_replay,
    verify_offline_replay,
)


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def _report(tmp_path: Path) -> tuple[Path, Path]:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    raw = evidence / "setup.rpt"
    raw.write_text("wns=-0.125\n", encoding="utf-8")
    claim = EvidenceClaim(
        claim_id="claim_setup",
        label="Worst setup slack",
        value="-0.125 ns",
        authority="MEASURED_EDA",
        artifact_ids=("artifact_setup",),
        evidence_ids=("artifact_setup",),
        comparison_identity_hashes={"analysis_view_set": _hash("a")},
    )
    report_path, _ = build_report_bundle(
        run_id="run_full",
        selected_candidate_id="candidate_primary",
        evidence_paths={"artifact_setup": raw},
        claims=(claim,),
        output_directory=tmp_path / "report",
    )
    return report_path, evidence


def test_offline_replay_is_deterministic_and_never_invokes_external_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path, evidence = _report(tmp_path)
    first_path, first = seal_offline_replay(
        report_path,
        evidence_root=evidence,
        output_directory=tmp_path / "replay_first",
        ledger_hash=_hash("b"),
    )
    _, second = seal_offline_replay(
        report_path,
        evidence_root=evidence,
        output_directory=tmp_path / "replay_second",
        ledger_hash=_hash("b"),
    )

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("tool call"))
    monkeypatch.setattr(
        socket, "create_connection", lambda *args, **kwargs: pytest.fail("network call")
    )
    verified = verify_offline_replay(first_path.parent)

    assert first.manifest_hash == second.manifest_hash == verified.manifest_hash
    assert verified.external_calls_allowed is False


def test_offline_replay_names_the_corrupt_artifact(tmp_path: Path) -> None:
    report_path, evidence = _report(tmp_path)
    manifest_path, _ = seal_offline_replay(
        report_path,
        evidence_root=evidence,
        output_directory=tmp_path / "replay",
        ledger_hash=_hash("b"),
    )
    (manifest_path.parent / "artifacts" / "artifact_setup").write_text(
        "corrupt\n", encoding="utf-8"
    )

    with pytest.raises(OfflineReplayError, match="artifact_setup"):
        verify_offline_replay(manifest_path.parent)
