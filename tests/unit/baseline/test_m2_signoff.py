from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline import signoff
from nova_rtl.benchmark.calibrate import build_calibration_report
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.contracts.base import ArtifactRef, canonical_json_bytes
from nova_rtl.contracts.benchmark import CalibrationSample, MappedCellTarget
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


def test_m2_calibration_rejects_a_missing_selected_stage_result(tmp_path: Path) -> None:
    config = load_benchmark_config(PROJECT_ROOT / "benchmark/generator/benchmark.yaml", "full")
    snapshot = generate_benchmark(config, tmp_path / "full")
    calibration = tmp_path / "calibration"
    ArtifactStore(calibration / "artifacts")
    lock = PlatformLock.model_validate(
        yaml.safe_load(
            (PROJECT_ROOT / "config/platform/platform.lock.yaml").read_text(encoding="utf-8")
        )
    )
    yosys = next(item for item in lock.tool_fingerprints if item.tool_id == "yosys")
    opensta = next(item for item in lock.tool_fingerprints if item.tool_id == "opensta")
    missing = ArtifactRef(
        artifact_id="stage_missing_stage_result",
        uri="artifact://sha256/" + ("a" * 64),
        sha256="sha256:" + ("a" * 64),
        media_type="application/json",
        size_bytes=1,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        producer_stage_result_id="stage_missing",
        classification="INTERNAL",
    )
    sample = CalibrationSample(
        workload_scale=config.workload_scale,
        mapped_cell_count=50_000,
        config_hash=snapshot.config_hash,
        source_hash=snapshot.source_hash,
        snapshot_hash=snapshot.snapshot_hash,
        recipe_hash="sha256:" + ("b" * 64),
        tool_fingerprint=yosys,
        stage_result_artifact=missing,
    )
    report = build_calibration_report(
        MappedCellTarget(minimum=45_000, maximum=55_000),
        (sample,),
    )
    (calibration / "calibration-report.json").write_bytes(canonical_json_bytes(report))
    (calibration / "calibration-validation.json").write_text("{}", encoding="utf-8")

    with pytest.raises(signoff.M2SignoffError, match="artifact"):
        signoff._calibration_evidence(calibration, snapshot, yosys, opensta)


def test_m2_snapshot_must_match_the_current_committed_generator(tmp_path: Path) -> None:
    config = load_benchmark_config(PROJECT_ROOT / "benchmark/generator/benchmark.yaml", "full")
    snapshot = generate_benchmark(config, tmp_path / "full")

    signoff._require_benchmark_source_identity(PROJECT_ROOT, snapshot, config.workload_scale)

    changed = snapshot.model_copy(update={"template_hash": "sha256:" + ("0" * 64)})
    with pytest.raises(signoff.M2SignoffError, match="committed benchmark generator"):
        signoff._require_benchmark_source_identity(PROJECT_ROOT, changed, config.workload_scale)

    changed_full_identity = snapshot.model_copy(update={"snapshot_hash": "sha256:" + ("1" * 64)})
    with pytest.raises(signoff.M2SignoffError, match="committed benchmark generator"):
        signoff._require_benchmark_source_identity(
            PROJECT_ROOT, changed_full_identity, config.workload_scale
        )


def test_m2_rejects_an_m1_commit_that_is_not_an_ancestor(tmp_path: Path) -> None:
    project = _git_repository(tmp_path)
    ancestor = _commit_file(project, "ancestor\n", "ancestor")
    subprocess.run(["git", "-C", str(project), "checkout", "-qb", "m2"], check=True)
    current = _commit_file(project, "m2\n", "m2")
    subprocess.run(
        ["git", "-C", str(project), "checkout", "-qb", "unrelated", ancestor],
        check=True,
    )
    unrelated = _commit_file(project, "unrelated\n", "unrelated")

    signoff._require_git_ancestor(project, ancestor, current)
    with pytest.raises(signoff.M2SignoffError, match="not an ancestor"):
        signoff._require_git_ancestor(project, unrelated, current)


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


def test_failed_prepublication_verification_preserves_previous_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    run = tmp_path / "run"
    calibration = tmp_path / "calibration"
    m1_packet = tmp_path / "m1"
    for directory in (repository, run, calibration, m1_packet):
        directory.mkdir()
    destination = run / "m2-signoff.json"
    destination.write_bytes(b"previous-packet\n")
    report = object()

    monkeypatch.setattr(signoff, "_clean_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(signoff, "_implementation_tree", lambda _root, _commit: "b" * 40)
    monkeypatch.setattr(
        signoff,
        "_verified_m1_dependency",
        lambda _root, _packet, _commit: (object(), "sha256:" + ("c" * 64)),
    )
    monkeypatch.setattr(signoff, "_build_report", lambda *_args: report)

    def reject_before_publication(*_args: object, **_kwargs: object) -> object:
        raise signoff.M2SignoffError("recomputed evidence changed")

    monkeypatch.setattr(signoff, "_verify_observed_report", reject_before_publication)

    with pytest.raises(signoff.M2SignoffError, match="recomputed evidence changed"):
        signoff.run_m2_signoff(
            run,
            calibration,
            m1_packet,
            repository_root=repository,
        )

    assert destination.read_bytes() == b"previous-packet\n"
