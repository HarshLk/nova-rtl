from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from nova_rtl.baseline import signoff
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.contracts.platform import PlatformLock

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _commit_file(project: Path, content: str, message: str) -> str:
    tracked = project / "tracked.txt"
    tracked.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", message], check=True)
    return subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repository(tmp_path: Path) -> Path:
    project = tmp_path / "repository"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(project), "config", "user.email", "test@example.com"],
        check=True,
    )
    return project


def test_m2_git_queries_ignore_repository_replacement_objects(tmp_path: Path) -> None:
    project = _git_repository(tmp_path)
    original = _commit_file(project, "original\n", "original")
    replacement = _commit_file(project, "replacement\n", "replacement")
    subprocess.run(["git", "-C", str(project), "replace", original, replacement], check=True)

    assert signoff._git_output(project, "show", f"{original}:tracked.txt") == "original"


def test_m2_checkpoint_rejects_an_unrelated_clean_repository(tmp_path: Path) -> None:
    project = _git_repository(tmp_path)
    _commit_file(project, "unrelated\n", "unrelated")

    with pytest.raises(signoff.M2SignoffError, match="requested NOVA repository"):
        signoff._clean_commit(project)


def test_m2_calibration_must_be_the_run_snapshot_parent(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    project_root = calibration / "repeat-selected"
    project_root.mkdir(parents=True)

    signoff._require_calibration_run_identity(calibration, project_root)

    with pytest.raises(signoff.M2SignoffError, match="produced the analyzed snapshot"):
        signoff._require_calibration_run_identity(tmp_path / "other", project_root)


def test_m2_calibration_yosys_must_match_the_verified_tool() -> None:
    lock = PlatformLock.model_validate(
        yaml.safe_load(
            (PROJECT_ROOT / "config/platform/platform.lock.yaml").read_text(encoding="utf-8")
        )
    )
    expected = next(item for item in lock.tool_fingerprints if item.tool_id == "yosys")
    changed = expected.model_copy(update={"build_hash": "sha256:" + ("0" * 64)})

    signoff._require_calibration_tool_identity(expected, expected)

    with pytest.raises(signoff.M2SignoffError, match="calibration Yosys identity"):
        signoff._require_calibration_tool_identity(changed, expected)


def test_m2_snapshot_must_match_the_current_committed_generator(tmp_path: Path) -> None:
    config = load_benchmark_config(
        PROJECT_ROOT / "benchmark/generator/benchmark.yaml", "full"
    )
    snapshot = generate_benchmark(config, tmp_path / "full")

    signoff._require_benchmark_source_identity(PROJECT_ROOT, snapshot, config.workload_scale)

    changed = snapshot.model_copy(update={"template_hash": "sha256:" + ("0" * 64)})
    with pytest.raises(signoff.M2SignoffError, match="committed benchmark generator"):
        signoff._require_benchmark_source_identity(
            PROJECT_ROOT, changed, config.workload_scale
        )


def test_interrupted_m2_report_publication_preserves_previous_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "m2-signoff.json"
    destination.write_bytes(b"previous-packet\n")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError(f"interrupted {source} -> {target}")

    monkeypatch.setattr(signoff.os, "replace", fail_replace)

    with pytest.raises(signoff.M2SignoffError, match="publish M2 sign-off report"):
        signoff._publish_report_atomic(destination, b"new-packet\n")

    assert destination.read_bytes() == b"previous-packet\n"
    assert tuple(tmp_path.iterdir()) == (destination,)
