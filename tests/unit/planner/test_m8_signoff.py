from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from nova_rtl.contracts.planning import M8GateEvidence
from nova_rtl.optimization import council_signoff


def _gate(content: bytes, *, size_delta: int = 0) -> M8GateEvidence:
    return M8GateEvidence(
        evidence_id="m8_council_safety_matrix",
        argv=council_signoff._council_matrix_argv(),
        output_relative_path="reports/m8-council-safety-matrix.txt",
        output_hash="sha256:" + sha256(content).hexdigest(),
        output_size_bytes=len(content) + size_delta,
        passed_test_count=30,
    )


def test_council_matrix_evidence_preserves_exact_normalized_output(tmp_path: Path) -> None:
    raw = b".............................. [100%]\n30 passed in 2.91s\n"
    content = council_signoff._normalize_gate_output(raw)
    output = tmp_path / "reports/m8-council-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(content)

    council_signoff._verify_gate_evidence(tmp_path, _gate(content))
    assert b"30 passed in <elapsed>" in content


def test_council_matrix_rejects_changed_output(tmp_path: Path) -> None:
    content = b"30 passed in <elapsed>\n"
    output = tmp_path / "reports/m8-council-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(b"29 passed in <elapsed>\n")

    with pytest.raises(council_signoff.M8SignoffError, match="identity changed"):
        council_signoff._verify_gate_evidence(tmp_path, _gate(content))


def test_m8_dependency_verifies_the_recorded_ancestor_not_current_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = tmp_path / "m8-signoff.json"
    packet.write_bytes(b"signed-m8")
    observed = SimpleNamespace(
        commit_sha="a" * 40,
        implementation_tree_hash="b" * 40,
        council_implementation_hash="sha256:" + "d" * 64,
        council_matrix_evidence=object(),
    )
    monkeypatch.setattr(
        council_signoff.M8SignoffReport,
        "model_validate_json",
        lambda _content: observed,
    )
    calls: list[tuple[str, ...]] = []

    def git(_root: Path, *arguments: str) -> str:
        calls.append(arguments)
        return "b" * 40 if arguments[0] == "rev-parse" else ""

    monkeypatch.setattr(council_signoff, "_git_output", git)
    monkeypatch.setattr(council_signoff, "_verify_gate_evidence", lambda *_args: None)
    monkeypatch.setattr(council_signoff, "_build_report", lambda *_args, **_kwargs: observed)

    rebuilt, packet_hash = council_signoff.verify_m8_dependency_snapshot(
        packet,
        council_directory=tmp_path,
        m7_packet=tmp_path / "m7.json",
        path_migration_report=tmp_path / "migration.json",
        m6_packet=tmp_path / "m6.json",
        m5_packet=tmp_path / "m5.json",
        m4_packet=tmp_path / "m4.json",
        m3_packet=tmp_path / "m3.json",
        repository_root=tmp_path,
        descendant_commit="c" * 40,
    )

    assert rebuilt is observed
    assert packet_hash == "sha256:" + sha256(b"signed-m8").hexdigest()
    assert ("merge-base", "--is-ancestor", "a" * 40, "c" * 40) in calls
