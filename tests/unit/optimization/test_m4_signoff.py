from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.optimization import signoff


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_m4_dependency_uses_independent_frozen_m3_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = tmp_path / "m3-signoff.json"
    packet.write_bytes(b"packet")
    expected_report = object()
    expected_hash = "sha256:" + ("a" * 64)
    calls: list[tuple[Path, Path, str]] = []

    def verify_dependency(
        report_path: Path,
        *,
        repository_root: Path,
        descendant_commit: str,
    ) -> tuple[object, str]:
        calls.append((report_path, repository_root, descendant_commit))
        return expected_report, expected_hash

    monkeypatch.setattr(signoff, "verify_m3_dependency_snapshot", verify_dependency)

    report, packet_hash = signoff._m3_dependency(
        packet,
        repository_root=tmp_path,
        current_commit="b" * 40,
    )

    assert report is expected_report
    assert packet_hash == expected_hash
    assert calls == [(packet, tmp_path, "b" * 40)]


def test_m4_dependency_fails_closed_when_m3_reconstruction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args: object, **_kwargs: object) -> tuple[object, str]:
        raise ValueError("tampered M3 evidence")

    monkeypatch.setattr(signoff, "verify_m3_dependency_snapshot", reject)

    with pytest.raises(signoff.M4SignoffError, match="M3 dependency reconstruction"):
        signoff._m3_dependency(
            tmp_path / "m3-signoff.json",
            repository_root=tmp_path,
            current_commit="b" * 40,
        )


def test_m4_dependency_snapshot_accepts_a_merge_commit_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    tracked = repository / "tracked.txt"
    tracked.write_text("m4\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "m4")
    m4_commit = _git(repository, "rev-parse", "HEAD")
    m4_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    main_branch = _git(repository, "branch", "--show-current")
    _git(repository, "checkout", "-qb", "m5")
    tracked.write_text("m5\n", encoding="utf-8")
    _git(repository, "commit", "-q", "-am", "m5")
    _git(repository, "checkout", "-q", main_branch)
    side = repository / "side.txt"
    side.write_text("side\n", encoding="utf-8")
    _git(repository, "add", "side.txt")
    _git(repository, "commit", "-q", "-m", "side")
    _git(repository, "checkout", "-q", "m5")
    _git(repository, "merge", "-q", "--no-ff", main_branch, "-m", "merge m5")
    merge_commit = _git(repository, "rev-parse", "HEAD")

    packet = tmp_path / "candidate" / "m4-signoff.json"
    packet.parent.mkdir()
    content = b"frozen-m4-packet"
    packet.write_bytes(content)
    m3_packet = tmp_path / "m3-signoff.json"
    m3_packet.write_bytes(b"m3")

    class FrozenReport:
        commit_sha = m4_commit
        implementation_tree_hash = m4_tree
        toolchain_receipt_hash = "sha256:" + "1" * 64

    observed = FrozenReport()

    class FrozenContract:
        @staticmethod
        def model_validate_json(_content: bytes) -> FrozenReport:
            return observed

    monkeypatch.setattr(signoff, "M4SignoffReport", FrozenContract)
    monkeypatch.setattr(signoff, "M4CandidateBundle", object)
    monkeypatch.setattr(signoff, "verify_candidate_bundle", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        signoff,
        "_m3_dependency",
        lambda *_args, **_kwargs: (object(), "sha256:" + "2" * 64),
    )
    monkeypatch.setattr(signoff, "_build_report", lambda *_args, **_kwargs: observed)

    report, packet_hash = signoff.verify_m4_dependency_snapshot(
        packet,
        m3_packet=m3_packet,
        repository_root=repository,
        descendant_commit=merge_commit,
    )

    assert report is observed
    assert packet_hash == "sha256:" + sha256(content).hexdigest()


def test_m4_dependency_snapshot_rejects_a_non_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    tracked = repository / "tracked.txt"
    tracked.write_text("m4\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "m4")
    m4_commit = _git(repository, "rev-parse", "HEAD")
    m4_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    _git(repository, "checkout", "-q", "--orphan", "unrelated")
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-q", "-m", "unrelated")
    unrelated_commit = _git(repository, "rev-parse", "HEAD")
    packet = tmp_path / "m4-signoff.json"
    packet.write_bytes(b"packet")

    class FrozenReport:
        commit_sha = m4_commit
        implementation_tree_hash = m4_tree

    class FrozenContract:
        @staticmethod
        def model_validate_json(_content: bytes) -> FrozenReport:
            return FrozenReport()

    monkeypatch.setattr(signoff, "M4SignoffReport", FrozenContract)

    with pytest.raises(signoff.M4SignoffError, match="not an ancestor"):
        signoff.verify_m4_dependency_snapshot(
            packet,
            m3_packet=tmp_path / "m3-signoff.json",
            repository_root=repository,
            descendant_commit=unrelated_commit,
        )
