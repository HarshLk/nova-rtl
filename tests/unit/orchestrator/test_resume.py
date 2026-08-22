from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import ArtifactRef, StageInputHashes
from nova_rtl.orchestrator.runner import plan_resume
from nova_rtl.orchestrator.state import RunOrchestrator


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def input_hashes() -> StageInputHashes:
    return StageInputHashes(
        rtl_snapshot=hash_ref("1"),
        design_contract=hash_ref("2"),
        constraints=hash_ref("3"),
        constraint_binding=hash_ref("4"),
        analysis_view=hash_ref("5"),
        power_activity=None,
        platform_lock=hash_ref("6"),
        tool_recipe=hash_ref("7"),
        formal_model=None,
        parent_stage_result=None,
        extensions={},
    )


def test_resume_reuses_only_passed_exact_cache_identity() -> None:
    exact = input_hashes()
    cached = SimpleNamespace(status="PASS", input_hashes=exact)

    assert RunOrchestrator.can_reuse(cached, exact)
    changed = exact.model_copy(update={"analysis_view": hash_ref("8")})
    assert not RunOrchestrator.can_reuse(cached, changed)
    assert not RunOrchestrator.can_reuse(
        SimpleNamespace(status="INCONCLUSIVE", input_hashes=exact), exact
    )


def test_interrupted_run_dry_run_lists_exact_reuse_and_rerun_stages() -> None:
    repository = Path(__file__).resolve().parents[3]
    fixture = repository / "tests" / "fixtures" / "runs" / "interrupted"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nova_rtl.orchestrator.runner",
            str(fixture),
            "--resume",
            "--dry-run",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "rerun": ["stage_opensta_setup_001"],
        "reusable": ["stage_fast_synth_001"],
    }


def test_resume_reruns_corrupt_cached_stage_result_artifact(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    source = repository / "tests" / "fixtures" / "runs" / "interrupted"
    copied = tmp_path / "interrupted"
    shutil.copytree(source, copied)
    manifest = json.loads((copied / "resume-manifest.json").read_text())
    reference = ArtifactRef.model_validate(
        manifest["stages"][0]["cached_stage_result_artifact"]
    )
    store = ArtifactStore.open_existing(copied / "artifacts")
    blob = store.blob_path(reference)
    blob.chmod(0o644)
    blob.write_bytes(b"corrupt")

    planned = plan_resume(copied)

    assert "stage_fast_synth_001" in planned["rerun"]
    assert "stage_fast_synth_001" not in planned["reusable"]
