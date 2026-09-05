from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
