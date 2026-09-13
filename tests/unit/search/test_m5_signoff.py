from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.reporting import ConsumedSearchBudgets, M5GateEvidence
from nova_rtl.search import signoff


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _evidence(content: bytes, *, size_delta: int = 0) -> M5GateEvidence:
    return M5GateEvidence(
        evidence_id="m5_transform_formal_matrix",
        argv=signoff._formal_matrix_argv(),
        output_relative_path="reports/m5-transform-formal-matrix.txt",
        output_hash="sha256:" + sha256(content).hexdigest(),
        output_size_bytes=len(content) + size_delta,
        passed_test_count=82,
    )


def test_formal_matrix_evidence_verifies_exact_preserved_output(tmp_path: Path) -> None:
    content = (
        b"................................................................ [100%]\n"
        b"82 passed in 1.00s\n"
    )
    output = tmp_path / "reports" / "m5-transform-formal-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(content)

    signoff._verify_formal_matrix_evidence(tmp_path, _evidence(content))


def test_formal_matrix_evidence_rejects_changed_bytes(tmp_path: Path) -> None:
    original = b"82 passed in 1.00s\n"
    output = tmp_path / "reports" / "m5-transform-formal-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(b"81 passed in 1.00s\n")

    with pytest.raises(signoff.M5SignoffError, match="identity changed"):
        signoff._verify_formal_matrix_evidence(tmp_path, _evidence(original))


def test_formal_matrix_evidence_rejects_noncanonical_path(tmp_path: Path) -> None:
    content = b"82 passed in 1.00s\n"
    evidence = _evidence(content).model_copy(
        update={"output_relative_path": "../escaped.txt"}
    )

    with pytest.raises(signoff.M5SignoffError, match="path is not canonical"):
        signoff._verify_formal_matrix_evidence(tmp_path, evidence)


def test_search_replay_digest_wraps_ordered_records_in_a_named_boundary() -> None:
    records = (_evidence(b"82 passed\n"), _evidence(b"82 passed again\n"))

    assert signoff._search_replay_digest(records) == canonical_sha256(
        {"experiment_records": tuple(item.model_dump(mode="json") for item in records)}
    )


def test_signoff_input_hash_normalizes_nested_contracts() -> None:
    budgets = ConsumedSearchBudgets(
        candidates=1,
        formal_jobs=1,
        physical_jobs=1,
        tokens=0,
        latency_ms=10,
    )
    evidence = _evidence(b"82 passed\n")
    payload = {"status": "PASS", "consumed_budgets": budgets, "evidence": evidence}

    assert signoff._input_set_hash(payload) == canonical_sha256(
        {
            "consumed_budgets": budgets.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json"),
        }
    )


def test_m5_dependency_snapshot_rejects_a_non_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    tracked = repository / "tracked.txt"
    tracked.write_text("m5\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "m5")
    m5_commit = _git(repository, "rev-parse", "HEAD")
    m5_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    _git(repository, "checkout", "-q", "--orphan", "unrelated")
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-q", "-m", "unrelated")
    unrelated_commit = _git(repository, "rev-parse", "HEAD")
    packet = tmp_path / "m5-signoff.json"
    packet.write_bytes(b"packet")

    class FrozenReport:
        commit_sha = m5_commit
        implementation_tree_hash = m5_tree

    class FrozenContract:
        @staticmethod
        def model_validate_json(_content: bytes) -> FrozenReport:
            return FrozenReport()

    monkeypatch.setattr(signoff, "M5SignoffReport", FrozenContract)

    with pytest.raises(signoff.M5SignoffError, match="not an ancestor"):
        signoff.verify_m5_dependency_snapshot(
            packet,
            m4_packet=tmp_path / "m4-signoff.json",
            m3_packet=tmp_path / "m3-signoff.json",
            repository_root=repository,
            descendant_commit=unrelated_commit,
        )
