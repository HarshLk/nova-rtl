from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nova_rtl.contracts.schema_export import (
    SCHEMA_REGISTRY,
    SchemaDriftError,
    check_all_schemas,
    export_all_schemas,
)

EXPECTED_SCHEMA_FILES = {
    "analysis-view-contract.v1.schema.json",
    "artifact-ref.v1.schema.json",
    "benchmark-constraint-contract.v1.schema.json",
    "benchmark-config.v1.schema.json",
    "benchmark-formal-manifest.v1.schema.json",
    "benchmark-power-workload.v1.schema.json",
    "benchmark-snapshot.v1.schema.json",
    "benchmark-validation.v1.schema.json",
    "benchmark-validation-evidence.v1.schema.json",
    "calibration-report.v1.schema.json",
    "candidate-failure-fingerprint.v1.schema.json",
    "candidate-record.v1.schema.json",
    "cdc-inventory.v1.schema.json",
    "clock-inventory.v1.schema.json",
    "composition-closure-manifest.v1.schema.json",
    "constraint-binding-manifest.v1.schema.json",
    "context-request.v1.schema.json",
    "council-request.v1.schema.json",
    "council-result.v1.schema.json",
    "council-trace.v1.schema.json",
    "critical-path-record.v1.schema.json",
    "critique-disposition.v1.schema.json",
    "critique-report.v1.schema.json",
    "design-contract.v1.schema.json",
    "diagnosis-report.v1.schema.json",
    "diagnostic.v1.schema.json",
    "doctor-check.v1.schema.json",
    "doctor-report.v1.schema.json",
    "evidence-graph-snapshot.v1.schema.json",
    "evidence-ref.v1.schema.json",
    "experiment-record.v1.schema.json",
    "failure-event.v1.schema.json",
    "formal-model-contract.v1.schema.json",
    "frequency-sweep-contract.v1.schema.json",
    "gate-event.v1.schema.json",
    "metric-set.v1.schema.json",
    "m2-signoff-report.v1.schema.json",
    "m3-signoff-report.v1.schema.json",
    "m4-signoff-report.v2.schema.json",
    "m4-view-comparison.v1.schema.json",
    "m5-gate-evidence.v1.schema.json",
    "m5-signoff-report.v1.schema.json",
    "m6-gate-evidence.v1.schema.json",
    "m6-signoff-report.v1.schema.json",
    "mapped-structural-effect.v1.schema.json",
    "optimization-opportunity.v1.schema.json",
    "optimization-proposal.v2.schema.json",
    "pareto-record.v1.schema.json",
    "planner-request.v1.schema.json",
    "planner-result.v1.schema.json",
    "platform-lock.v1.schema.json",
    "power-activity-contract.v1.schema.json",
    "prepared-command.v1.schema.json",
    "project-manifest.v2.schema.json",
    "proof-result.v1.schema.json",
    "proposal-shortlist.v1.schema.json",
    "provider-result.v1.schema.json",
    "raw-tool-result.v1.schema.json",
    "recovery-advice.v1.schema.json",
    "recovery-decision.v1.schema.json",
    "recovery-request.v1.schema.json",
    "recovery-route-plan.v1.schema.json",
    "repair-directive.v1.schema.json",
    "report-bundle.v1.schema.json",
    "role-context-pack.v1.schema.json",
    "run-event.v1.schema.json",
    "search-request.v1.schema.json",
    "search-result.v1.schema.json",
    "stage-input-hashes.v1.schema.json",
    "stage-result.v2.schema.json",
    "tool-fingerprint.v1.schema.json",
    "tool-job.v1.schema.json",
    "transform-recommendation.v1.schema.json",
}


def snapshot_bytes(output_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(output_dir.glob("*.json"))}


def test_registry_exports_every_canonical_contract_with_versioned_names(
    tmp_path: Path,
) -> None:
    paths = export_all_schemas(tmp_path)

    assert {path.name for path in paths} == EXPECTED_SCHEMA_FILES
    assert set(SCHEMA_REGISTRY) == {
        filename.rsplit(".v", maxsplit=1)[0] for filename in EXPECTED_SCHEMA_FILES
    }
    assert all(path.read_bytes().endswith(b"\n") for path in paths)
    assert all(json.loads(path.read_text(encoding="utf-8")) for path in paths)


def test_schema_export_is_byte_deterministic_and_check_detects_drift(tmp_path: Path) -> None:
    export_all_schemas(tmp_path)
    first = snapshot_bytes(tmp_path)

    export_all_schemas(tmp_path)
    assert snapshot_bytes(tmp_path) == first
    assert check_all_schemas(tmp_path) == tuple(sorted(tmp_path.glob("*.schema.json")))

    drifted = tmp_path / "stage-result.v2.schema.json"
    drifted.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SchemaDriftError, match="stage-result.v2.schema.json"):
        check_all_schemas(tmp_path)


def test_schema_check_rejects_missing_and_unregistered_schema_files(tmp_path: Path) -> None:
    export_all_schemas(tmp_path)
    (tmp_path / "run-event.v1.schema.json").unlink()
    with pytest.raises(SchemaDriftError, match="missing"):
        check_all_schemas(tmp_path)

    export_all_schemas(tmp_path)
    (tmp_path / "stale.v99.schema.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SchemaDriftError, match="unexpected"):
        check_all_schemas(tmp_path)


def test_schema_export_module_cli_supports_write_and_check(tmp_path: Path) -> None:
    write_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nova_rtl.contracts.schema_export",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert write_result.returncode == 0, write_result.stderr
    assert f"exported {len(EXPECTED_SCHEMA_FILES)} schemas" in write_result.stdout

    check_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nova_rtl.contracts.schema_export",
            str(tmp_path),
            "--check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check_result.returncode == 0, check_result.stderr
    assert "schema check passed" in check_result.stdout
