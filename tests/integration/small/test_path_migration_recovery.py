from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.recovery.showcase import (
    create_path_migration_showcase,
    verify_path_migration_showcase,
)


def test_path_migration_uses_a_distinct_target_and_replays(tmp_path: Path) -> None:
    path, first = create_path_migration_showcase(
        output_directory=tmp_path,
        run_id="run_path_migration",
        candidate_id="candidate_local_improvement",
        parent_candidate_id="baseline",
        source_hash="sha256:" + "1" * 64,
        search_bundle_hash="sha256:" + "2" * 64,
    )
    second = verify_path_migration_showcase(path)

    assert first.status == "PASS"
    assert first.failure.failure_family == "CRITICAL_PATH_MIGRATION"
    assert first.decision.action == "OPPORTUNITY_REANALYSIS"
    assert first.next_target_cone_fingerprint != first.fingerprint.target_cone_fingerprint
    assert first.next_operation_family != first.fingerprint.operation_family
    assert first.report_hash == second.report_hash


def test_failure_inspect_resolves_and_verifies_showcase(tmp_path: Path) -> None:
    path, report = create_path_migration_showcase(
        output_directory=tmp_path / "run" / "recovery",
        run_id="run_path_migration",
        candidate_id="candidate_local_improvement",
        parent_candidate_id="baseline",
        source_hash="sha256:" + "1" * 64,
        search_bundle_hash="sha256:" + "2" * 64,
    )
    result = CliRunner().invoke(
        app,
        [
            "failure",
            "inspect",
            report.failure.failure_event_id,
            "--runs-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert path.is_file()
    assert result.exit_code == 0, result.output
    assert '"status":"PASS"' in result.output
    assert '"action":"OPPORTUNITY_REANALYSIS"' in result.output
