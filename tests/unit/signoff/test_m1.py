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
    schema_files = (
        write_evidence(
            packet,
            "schemas/artifact-ref.v1.schema.json",
            b'{"title":"ArtifactRef"}\n',
        ),
        write_evidence(
            packet,
            "schemas/stage-result.v2.schema.json",
            b'{"title":"StageResult"}\n',
        ),
    )
    digest = schema_manifest_hash(schema_files)
    replay_digest = hash_ref(b"replay-events")

    def command(evidence_id: str, output: bytes) -> M1CommandEvidence:
        return M1CommandEvidence(
            evidence_id=evidence_id,
            argv=("/usr/bin/python3", "-m", "pytest", "-q"),
            exit_code=0,
            output=write_evidence(packet, f"reports/{evidence_id}.txt", output),
        )

    report = M1SignoffReport(
        milestone="M1",
        status="PASS",
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        implementation_commit="1" * 40,
        implementation_tree_hash="2" * 40,
        python_executable="/usr/bin/python3",
        python_executable_sha256=hash_ref(b"python"),
        python_version="3.11.15",
        pytest_version="8.4.2",
        schema_export_digest_first=digest,
        schema_export_digest_second=digest,
        schema_files=schema_files,
        schema_test=command("schema_tests", b"54 passed\n"),
        artifact_corruption_test=command("artifact_corruption_test", b"1 passed\n"),
        replay_test=command("replay_tests", b"3 passed\n"),
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

    report = verify_m1_signoff_packet(packet)

    assert report.status == "PASS"
    assert report.replay_digest == hash_ref(b"replay-events")
    assert len(report.schema_files) == 2


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
        verify_m1_signoff_packet(packet)


def test_m1_report_rejects_nondeterministic_schema_exports(tmp_path: Path) -> None:
    packet = build_packet(tmp_path)
    payload = json.loads((packet / "m1-signoff.json").read_text(encoding="utf-8"))
    payload["schema_export_digest_second"] = hash_ref(b"different")

    with pytest.raises(ValidationError, match="schema exports must be byte-identical"):
        M1SignoffReport.model_validate(payload)


def test_run_m1_signoff_executes_required_gates_and_publishes_atomically(
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

    report_path, generated = run_m1_signoff(
        M1SignoffRequest(
            project_root=project,
            schema_directory=project / "schemas/canonical",
            replay_fixture=project / "tests/fixtures/runs/minimal",
            output_directory=project / "runs/m1-signoff",
            python_executable=Path(sys.executable),
        ),
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert report_path == project / "runs/m1-signoff/m1-signoff.json"
    assert generated.schema_export_digest_first == generated.schema_export_digest_second
    assert generated.replay_first.output.sha256 == generated.replay_second.output.sha256
    assert verify_m1_signoff_packet(report_path.parent, project_root=project) == generated
    assert not tuple(report_path.parent.parent.glob(".m1-signoff-*"))
