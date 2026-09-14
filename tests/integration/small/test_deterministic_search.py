from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.optimization.search_flow import (
    run_deterministic_search,
    verify_deterministic_search,
)
from nova_rtl.recovery.search import recover_search_bundle, verify_search_recovery

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_deterministic_small_search_is_replayable(completed_run_path: Path) -> None:
    operations = (
        "BALANCE_BOOLEAN_TREE",
        "FACTOR_COMMON_PREDICATE",
        "FSM_DECODE_RESTRUCTURE",
        "RESTRUCTURE_PRIORITY_MUX",
    )
    first_path, first = run_deterministic_search(
        completed_run_path,
        repository_root=PROJECT_ROOT,
        operations=operations,
        max_candidates=8,
        formal_budget=8,
        physical_budget=4,
        token_budget=1,
        latency_budget_ms=3_600_000,
        deterministic_seed=20260808,
        stagnation_window=8,
    )
    second_path, second = run_deterministic_search(
        completed_run_path,
        repository_root=PROJECT_ROOT,
        operations=operations,
        max_candidates=8,
        formal_budget=8,
        physical_budget=4,
        token_budget=1,
        latency_budget_ms=3_600_000,
        deterministic_seed=20260808,
        stagnation_window=8,
    )
    verified = verify_deterministic_search(first_path, repository_root=PROJECT_ROOT)

    assert first.status == "PASS"
    assert first_path == second_path
    assert first.bundle_hash == second.bundle_hash == verified.bundle_hash
    assert len(first.planned_candidates) >= 2
    assert first.search_result.consumed_budgets.candidates <= 8
    assert first.search_result.consumed_budgets.formal_jobs <= 8
    assert first.search_result.consumed_budgets.physical_jobs <= 4
    assert first.search_result.ordered_candidate_ids
    assert first.valid_negative_candidate_ids
    assert first.candidate_dag.dag_hash == second.candidate_dag.dag_hash
    assert first.pareto_archive.archive_hash == second.pareto_archive.archive_hash

    recovery_path, recovery = recover_search_bundle(
        first_path, repository_root=PROJECT_ROOT
    )
    replayed_recovery = verify_search_recovery(
        recovery_path, repository_root=PROJECT_ROOT
    )
    assert recovery.failure.candidate_id in first.valid_negative_candidate_ids
    assert recovery.search_bundle_hash == first.bundle_hash
    assert recovery.recovery_outcome == "ROUTED"
    assert recovery.report_hash == replayed_recovery.report_hash
