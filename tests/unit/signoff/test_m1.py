from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.base import canonical_json_bytes
from nova_rtl.contracts.platform import SignoffEvidenceFile
from nova_rtl.signoff.m1 import (
    M1CommandEvidence,
    M1SignoffError,
    M1SignoffReport,
    M1SignoffRequest,
    _git_query_bytes,
    _publish_staging_no_replace,
    _require_output_boundary,
    m1_signoff_invocation_hash,
    run_m1_signoff,
    verify_m1_signoff_packet,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def hash_ref(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def write_evidence(root: Path, relative_path: str, data: bytes) -> SignoffEvidenceFile:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_ref(data),
        size_bytes=len(data),
    )


def schema_manifest_hash(schema_files: tuple[SignoffEvidenceFile, ...]) -> str:
    payload = [item.model_dump(mode="json") for item in schema_files]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hash_ref(encoded)


def build_packet(tmp_path: Path) -> Path:
    packet = tmp_path / "m1-packet"
    packet.mkdir()
    commit = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked_schemas = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(PROJECT_ROOT),
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "--",
            "schemas/canonical",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    schema_files = tuple(
        write_evidence(
            packet,
            f"schemas/{Path(path).name}",
            subprocess.run(
                [
                    "git",
                    "--no-replace-objects",
                    "-C",
                    str(PROJECT_ROOT),
                    "show",
                    f"{commit}:{path}",
                ],
                check=True,
                capture_output=True,
            ).stdout,
        )
        for path in sorted(path for path in tracked_schemas if path.endswith(".schema.json"))
    )
    digest = schema_manifest_hash(schema_files)
    replay_digest = hash_ref(b"replay-events")
    python = "/usr/bin/python3"
    command_argv = {
        "schema_tests": (
            python,
            "-P",
            "-m",
            "pytest",
            "tests/unit/contracts",
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        "artifact_corruption_test": (
            python,
            "-P",
            "-m",
            "pytest",
            "tests/unit/artifacts/test_store.py::"
            "test_open_verified_fails_closed_for_corruption_and_missing_blob",
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        "replay_tests": (
            python,
            "-P",
            "-m",
            "pytest",
            "tests/unit/artifacts/test_replay.py::"
            "test_replay_uses_sequence_and_makes_no_model_or_tool_calls",
            "tests/unit/artifacts/test_replay.py::"
            "test_replay_digest_is_deterministic_for_the_verified_event_stream",
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        "m1_regression_tests": (
            python,
            "-P",
            "-m",
            "pytest",
            "tests/unit/contracts",
            "tests/unit/artifacts",
            "tests/unit/orchestrator",
            "tests/unit/analysis_views",
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        "replay_first": (
            python,
            "-P",
            "-m",
            "nova_rtl.artifacts.replay",
            "tests/fixtures/runs/minimal",
            "--digest",
        ),
        "replay_second": (
            python,
            "-P",
            "-m",
            "nova_rtl.artifacts.replay",
            "tests/fixtures/runs/minimal",
            "--digest",
        ),
    }

    def command(evidence_id: str, output: bytes) -> M1CommandEvidence:
        return M1CommandEvidence(
            evidence_id=evidence_id,
            argv=command_argv[evidence_id],
            exit_code=0,
            output=write_evidence(packet, f"reports/{evidence_id}.txt", output),
        )

    tree_hash = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(PROJECT_ROOT),
            "rev-parse",
            "HEAD^{tree}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    report = M1SignoffReport(
        milestone="M1",
        status="PASS",
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        implementation_commit=commit,
        implementation_tree_hash=tree_hash,
        python_executable=python,
        python_executable_sha256=hash_ref(b"python"),
        python_version="3.11.15",
        pytest_version="8.4.2",
        schema_export_digest_first=digest,
        schema_export_digest_second=digest,
        schema_files=schema_files,
        schema_test=command("schema_tests", f"{len(schema_files)} passed\n".encode()),
        artifact_corruption_test=command("artifact_corruption_test", b"1 passed\n"),
        replay_test=command("replay_tests", b"2 passed\n"),
        m1_regression_test=command("m1_regression_tests", b"170 passed\n"),
        replay_first=command("replay_first", f"{replay_digest}\n".encode()),
        replay_second=command("replay_second", f"{replay_digest}\n".encode()),
        replay_digest=replay_digest,
        signoff_invocation_hash=hash_ref(b"placeholder"),
    )
    report = report.model_copy(
        update={"signoff_invocation_hash": m1_signoff_invocation_hash(report)}
    )
    (packet / "m1-signoff.json").write_bytes(canonical_json_bytes(report) + b"\n")
    return packet


def test_verify_m1_packet_accepts_exact_commit_bound_evidence(tmp_path: Path) -> None:
    packet = build_packet(tmp_path)

    report = verify_m1_signoff_packet(packet, project_root=PROJECT_ROOT)

    assert report.status == "PASS"
    assert report.replay_digest == hash_ref(b"replay-events")
    assert {
        "schemas/artifact-ref.v1.schema.json",
        "schemas/run-event.v1.schema.json",
        "schemas/stage-result.v2.schema.json",
    } <= {item.relative_path for item in report.schema_files}


@pytest.mark.parametrize("mutation", ["artifact", "unexpected", "commit"])
def test_verify_m1_packet_fails_closed_for_any_identity_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    packet = build_packet(tmp_path)
    if mutation == "artifact":
        (packet / "reports/schema_tests.txt").write_text("forged\n", encoding="utf-8")
    elif mutation == "unexpected":
        (packet / "unexpected.txt").write_text("not indexed\n", encoding="utf-8")
    else:
        report_path = packet / "m1-signoff.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["implementation_commit"] = "3" * 40
        report_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(M1SignoffError):
        verify_m1_signoff_packet(packet, project_root=PROJECT_ROOT)


def test_m1_report_rejects_nondeterministic_schema_exports(tmp_path: Path) -> None:
    packet = build_packet(tmp_path)
    payload = json.loads((packet / "m1-signoff.json").read_text(encoding="utf-8"))
    payload["schema_export_digest_second"] = hash_ref(b"different")

    with pytest.raises(ValidationError, match="schema exports must be byte-identical"):
        M1SignoffReport.model_validate(payload)


def test_run_m1_signoff_rejects_an_unrelated_clean_repository(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(PROJECT_ROOT / "schemas/canonical", project / "schemas/canonical")
    shutil.copytree(
        PROJECT_ROOT / "tests/fixtures/runs/minimal",
        project / "tests/fixtures/runs/minimal",
    )
    test_files = {
        "tests/unit/contracts/test_contracts.py": "def test_contracts(): assert True\n",
        "tests/unit/artifacts/test_store.py": (
            "def test_open_verified_fails_closed_for_corruption_and_missing_blob(): "
            "assert True\n"
        ),
        "tests/unit/artifacts/test_replay.py": (
            "def test_replay_uses_sequence_and_makes_no_model_or_tool_calls(): assert True\n"
            "def test_replay_digest_is_deterministic_for_the_verified_event_stream(): "
            "assert True\n"
        ),
        "tests/unit/orchestrator/test_state.py": "def test_state(): assert True\n",
        "tests/unit/analysis_views/test_aggregation.py": "def test_views(): assert True\n",
    }
    for relative_path, content in test_files.items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (project / ".gitignore").write_text(
        "/runs/\n.pytest_cache/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(project), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "fixture"], check=True)

    with pytest.raises(M1SignoffError, match="loaded NOVA package"):
        run_m1_signoff(
            M1SignoffRequest(
                project_root=project,
                schema_directory=project / "schemas/canonical",
                replay_fixture=project / "tests/fixtures/runs/minimal",
                output_directory=project / "runs/m1-signoff",
                python_executable=Path(sys.executable),
            ),
            generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        )


def test_verify_m1_packet_rejects_changed_required_command(tmp_path: Path) -> None:
    packet = build_packet(tmp_path)
    report_path = packet / "m1-signoff.json"
    report = M1SignoffReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    changed_command = report.schema_test.model_copy(
        update={"argv": (*report.schema_test.argv, "--disable-warnings")}
    )
    changed_report = report.model_copy(update={"schema_test": changed_command})
    changed_report = changed_report.model_copy(
        update={"signoff_invocation_hash": m1_signoff_invocation_hash(changed_report)}
    )
    report_path.write_bytes(canonical_json_bytes(changed_report) + b"\n")

    with pytest.raises(M1SignoffError, match="command specification"):
        verify_m1_signoff_packet(packet, project_root=PROJECT_ROOT)


def test_run_m1_signoff_rejects_output_outside_project_runs(tmp_path: Path) -> None:
    with pytest.raises(M1SignoffError, match="project runs directory"):
        run_m1_signoff(
            M1SignoffRequest(
                project_root=PROJECT_ROOT,
                schema_directory=PROJECT_ROOT / "schemas/canonical",
                replay_fixture=PROJECT_ROOT / "tests/fixtures/runs/minimal",
                output_directory=tmp_path / "outside",
                python_executable=Path(sys.executable),
            )
        )


def test_atomic_publication_never_replaces_a_concurrent_destination(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "evidence.txt").write_text("trusted\n", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(M1SignoffError, match="already exists"):
        _publish_staging_no_replace(staging, destination)

    assert destination.is_dir()
    assert not tuple(destination.iterdir())
    assert staging.is_dir()


def test_git_queries_ignore_repository_replacement_objects(tmp_path: Path) -> None:
    project = tmp_path / "replace-repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(project), "config", "user.email", "test@example.com"],
        check=True,
    )
    tracked = project / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "original"], check=True)
    original = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("replacement\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "commit", "-qam", "replacement"], check=True)
    replacement = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(project), "replace", original, replacement], check=True)

    assert _git_query_bytes(project, "show", f"{original}:tracked.txt") == b"original\n"


def test_output_boundary_creates_missing_ignored_runs_directory(tmp_path: Path) -> None:
    project = tmp_path / "fresh-clone"
    project.mkdir()
    (project / ".gitignore").write_text("/runs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)

    destination = project / "runs/m1-signoff-test"
    _require_output_boundary(project.resolve(), destination)

    assert destination.parent.is_dir()
    assert subprocess.run(
        ["git", "-C", str(project), "check-ignore", "-q", str(destination)],
        check=False,
    ).returncode == 0
