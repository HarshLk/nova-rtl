from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nova_rtl.evidence import signoff
from nova_rtl.evidence.signoff import M3SignoffError, _require_git_ancestor


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_m3_dependency_requires_m2_commit_to_be_an_ancestor(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    tracked = repository / "tracked.txt"
    tracked.write_text("m2\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "m2")
    m2_commit = _git(repository, "rev-parse", "HEAD")
    tracked.write_text("m3\n", encoding="utf-8")
    _git(repository, "commit", "-q", "-am", "m3")
    m3_commit = _git(repository, "rev-parse", "HEAD")

    _require_git_ancestor(repository, m2_commit, m3_commit)
    with pytest.raises(M3SignoffError, match="not an ancestor"):
        _require_git_ancestor(repository, m3_commit, m2_commit)


def test_m3_dependency_requires_independent_frozen_m2_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = tmp_path / "m2-signoff.json"
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

    monkeypatch.setattr(signoff, "verify_m2_dependency_snapshot", verify_dependency)

    report, packet_hash = signoff._m2_dependency(
        packet,
        repository_root=tmp_path,
        current_commit="b" * 40,
    )

    assert report is expected_report
    assert packet_hash == expected_hash
    assert calls == [(packet, tmp_path, "b" * 40)]
