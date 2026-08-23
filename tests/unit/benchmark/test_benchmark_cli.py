from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.contracts.manifest import ProjectManifest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"


def test_benchmark_generate_cli_emits_canonical_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "generate",
            "--config",
            str(PROFILE_MANIFEST),
            "--profile",
            "tiny",
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["profile"] == "tiny"
    assert payload["expected_master_clocks"] == 5
    assert payload["expected_generated_total"] == 10
    assert payload["snapshot_hash"].startswith("sha256:")
    snapshot_path = output / "benchmark-snapshot.json"
    assert snapshot_path.is_file()
    assert (
        json.loads(snapshot_path.read_text(encoding="utf-8"))["snapshot_hash"]
        == payload["snapshot_hash"]
    )

    project = ProjectManifest.model_validate(
        yaml.safe_load((output / "project.yaml").read_text(encoding="utf-8"))
    )
    assert project.constraints.expected_master_clocks == 5
    assert project.constraints.expected_generated_clocks_per_master == 2
    assert {view.check for view in project.analysis_views if view.required} == {
        "SETUP",
        "HOLD",
    }
