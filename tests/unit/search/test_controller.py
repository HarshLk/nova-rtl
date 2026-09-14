from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.optimization import CandidateRecord
from nova_rtl.contracts.recovery import (
    CandidateFailureFingerprint,
    FailureEvent,
    RecoveryDecision,
    RepairDirective,
)
from nova_rtl.contracts.reporting import ExperimentRecord, ParetoRecord, SearchRequest
from nova_rtl.recovery.execution import apply_recovery
from nova_rtl.search.controller import (
    CandidateEvaluation,
    DeterministicHeuristicPlanner,
    EvaluationCost,
    PlannedCandidate,
    SearchController,
    SearchControllerError,
    SearchStopPolicy,
)
from nova_rtl.search.dag import CandidateDag
from nova_rtl.search.pareto import ParetoArchive


def _hash(character: str) -> str:
    return "sha256:" + character * 64


STOP_POLICY = SearchStopPolicy.build(
    max_candidates_without_pareto_improvement=8,
    stop_on_first_feasible=False,
)


def _request(**overrides: object) -> SearchRequest:
    values: dict[str, object] = {
        "search_request_id": "search_request_001",
        "run_id": "run_search_001",
        "design_contract_hash": _hash("1"),
        "policy_hash": _hash("2"),
        "transform_registry_hash": _hash("3"),
        "ordered_opportunity_ids": ("opportunity_a", "opportunity_b"),
        "planner_mode": "HEURISTIC",
        "required_correctness_class": "PRIMARY_STRICT",
        "candidate_budget": 3,
        "formal_budget": 3,
        "physical_budget": 3,
        "token_budget": 10,
        "latency_budget_ms": 1000,
        "deterministic_seed": 20260808,
        "stop_policy_hash": STOP_POLICY.policy_hash,
        "created_at": datetime(2026, 9, 13, tzinfo=UTC),
    }
    values.update(overrides)
    return SearchRequest.model_validate(values)


def _proposal(index: int, opportunity: str = "opportunity_a") -> PlannedCandidate:
    return PlannedCandidate.build(
        proposal_id=f"proposal_{index:03d}",
        opportunity_id=opportunity,
        parent_candidate_id="baseline",
        operation=(
            "RESTRUCTURE_PRIORITY_MUX" if index % 2 else "BALANCE_BOOLEAN_TREE"
        ),
        transformation_hash=f"sha256:{sha256(f'proposal-{index}'.encode()).hexdigest()}",
        priority=1.0,
    )


class _Materializer:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.calls: list[str] = []

    async def materialize(self, proposal: PlannedCandidate) -> CandidateRecord:
        self.calls.append(proposal.proposal_id)
        suffix = proposal.proposal_id.removeprefix("proposal_")
        snapshot = self.store.put_named_bytes(
            f"rtl-{suffix}".encode(),
            artifact_id=f"artifact_rtl_{suffix}",
            media_type="application/octet-stream",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=None,
        )
        snapshot = snapshot.model_copy(
            update={"created_at": datetime(2026, 9, 13, tzinfo=UTC)}
        )
        patch = self.store.put_named_bytes(
            f"patch-{suffix}".encode(),
            artifact_id=f"artifact_patch_{suffix}",
            media_type="text/x-diff",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=None,
        )
        patch = patch.model_copy(
            update={"created_at": datetime(2026, 9, 13, tzinfo=UTC)}
        )
        return CandidateRecord(
            candidate_id=f"cand_{suffix}",
            run_id="run_search_001",
            parent_candidate_id="baseline",
            lineage_depth=1,
            proposal_id=proposal.proposal_id,
            opportunity_id=proposal.opportunity_id,
            rtl_snapshot_artifact=snapshot,
            patch_artifact=patch,
            source_hash=snapshot.sha256,
            changed_spans=(f"span_{suffix}",),
            transform_fingerprint=f"transform:v1:{suffix}",
            required_correctness_contract="STRICT_SEQ_EQUIV",
            stage_result_ids=(),
            per_view_metrics={},
            proof_result_id=None,
            binding_manifest_id=None,
            clock_inventory_id=None,
            cdc_inventory_id=None,
            hard_gate_summary=None,
            classification="INCONCLUSIVE",
            selection_class="PRIMARY_STRICT",
            terminal_disposition="PENDING_EVALUATION",
            created_at=datetime(2026, 9, 13, tzinfo=UTC),
            recovery_parent_failure_id=None,
        )


def _identities() -> dict[str, str]:
    return {
        "analysis_view_set": _hash("1"),
        "constraint_source": _hash("2"),
        "power_activity": _hash("3"),
        "platform_lock": _hash("4"),
        "constraint_binding": _hash("5"),
        "clock_graph": _hash("6"),
        "cdc_inventory": _hash("7"),
        "formal_model": _hash("8"),
    }


def _pareto(candidate: CandidateRecord, setup: float) -> ParetoRecord:
    return ParetoRecord(
        pareto_record_id=f"pareto_{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        correctness_contract="STRICT_SEQ_EQUIV",
        selection_class="PRIMARY_STRICT",
        feasibility_predicate_version="feasibility-v1",
        required_view_metric_ids={"asap7_setup": f"metric_{candidate.candidate_id}"},
        metric_vector={
            "setup_wns_ns": setup,
            "setup_tns_ns": setup * 10.0,
            "hold_wns_ns": 0.01,
            "hold_tns_ns": 0.0,
            "failing_endpoints": 1.0,
            "mapped_area_um2": 100.0,
            "physical_area_um2": 110.0,
            "power_total_uw": 10.0,
            "source_change_lines": 4.0,
            "evaluation_runtime_ms": 100.0,
        },
        dominated_candidate_ids=(),
        objective_policy_rank=1,
        binding_hash=_hash("5"),
        clock_hash=_hash("6"),
        cdc_hash=_hash("7"),
        formal_hash=_hash("8"),
        physical_stage="OPENROAD_ROUTED",
        comparison_identity_hashes=_identities(),
        recorded_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def _experiment(candidate: CandidateRecord) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_record_id=f"experiment_{candidate.candidate_id}",
        run_id=candidate.run_id,
        planner_result_id=None,
        council_result_id=None,
        opportunity_id=candidate.opportunity_id,
        cone_fingerprint="cone:v1:search",
        proposal_id=candidate.proposal_id,
        operation="RESTRUCTURE_PRIORITY_MUX",
        parameters={},
        parent_candidate_id=candidate.parent_candidate_id,
        candidate_id=candidate.candidate_id,
        source_hash=candidate.source_hash,
        patch_hash=candidate.patch_artifact.sha256,
        transform_fingerprint=candidate.transform_fingerprint,
        stage_result_ids=(),
        comparison_identity_hashes={"analysis_view": _hash("1")},
        proof_result_id=None,
        proof_outcome=None,
        counterexample_artifact_id=None,
        before_metrics={},
        after_metrics={},
        failure_event_id=None,
        repair_directive_id=None,
        candidate_failure_fingerprint_id=None,
        recovery_decision_id=None,
        descendant_outcome=None,
        role_ids=(),
        model_configuration_hashes=(),
        prompt_hashes=(),
        context_pack_hashes=(),
        input_tokens=0,
        output_tokens=0,
        planner_latency_ms=0,
        eda_runtime_ms=100,
        terminal_disposition="FEASIBLE_PARETO",
        human_review=None,
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


class _Evaluator:
    def __init__(self, *, cost: EvaluationCost | None = None) -> None:
        self.cost = cost or EvaluationCost(
            formal_jobs=1, physical_jobs=1, tokens=0, latency_ms=100
        )
        self.calls: list[str] = []

    def estimate(self, proposal: PlannedCandidate) -> EvaluationCost:
        return self.cost

    async def evaluate(self, candidate: CandidateRecord) -> CandidateEvaluation:
        self.calls.append(candidate.candidate_id)
        suffix = int(candidate.candidate_id.removeprefix("cand_"))
        return CandidateEvaluation(
            candidate_id=candidate.candidate_id,
            experiment_record=_experiment(candidate),
            pareto_record=_pareto(candidate, -0.20 + suffix / 1000.0),
            artifact_refs=(candidate.rtl_snapshot_artifact, candidate.patch_artifact),
            consumed_cost=self.cost,
            terminal_disposition="FEASIBLE_PARETO",
        )


def _controller(
    tmp_path: Path,
    proposals: tuple[PlannedCandidate, ...],
    evaluator=None,  # type: ignore[no-untyped-def]
    *,
    stop_policy: SearchStopPolicy = STOP_POLICY,
    recovery=None,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = ExperimentLedger(tmp_path / "ledger.sqlite3", artifact_store=store)
    materializer = _Materializer(store)
    planner = DeterministicHeuristicPlanner(
        {"opportunity_a": proposals, "opportunity_b": ()}
    )
    archive = ParetoArchive()
    dag = CandidateDag("baseline")
    controller = SearchController(
        planner=planner,
        materializer=materializer,
        evaluator=evaluator or _Evaluator(),
        archive=archive,
        candidate_dag=dag,
        ledger=ledger,
        artifact_store=store,
        stop_policy=stop_policy,
        recovery=recovery,
    )
    return controller, materializer, archive, dag, ledger


class _RejectedEvaluator(_Evaluator):
    async def evaluate(self, candidate: CandidateRecord) -> CandidateEvaluation:
        evaluation = await super().evaluate(candidate)
        return evaluation.model_copy(
            update={
                "pareto_record": None,
                "terminal_disposition": "REJECTED_CORRECTNESS",
                "experiment_record": evaluation.experiment_record.model_copy(
                    update={"terminal_disposition": "REJECTED_CORRECTNESS"}
                ),
            }
        )


class _RecoveryHandler:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    async def recover(self, candidate, evaluation):  # type: ignore[no-untyped-def]
        failure = FailureEvent.model_construct(
            failure_event_id=f"failure_{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
        )
        directive = RepairDirective.model_construct(
            repair_directive_id=f"directive_{candidate.candidate_id}",
            failure_event_id=failure.failure_event_id,
        )
        semantic = CandidateFailureFingerprint.model_construct(
            candidate_failure_fingerprint_id=f"candidate_failure_{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
        )
        decision = RecoveryDecision.model_construct(
            recovery_decision_id=f"recovery_decision_{candidate.candidate_id}",
            failure_event_id=failure.failure_event_id,
            action="REJECT_CANDIDATE",
        )
        return apply_recovery(
            evaluation.experiment_record,
            failure=failure,
            directive=directive,
            fingerprint=semantic,
            decision=decision,
            artifact_store=self.store,
        )


def test_nonpass_search_persists_and_indexes_recovery_chain(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    handler = _RecoveryHandler(store)
    ledger = ExperimentLedger(tmp_path / "ledger.sqlite3", artifact_store=store)
    controller = SearchController(
        planner=DeterministicHeuristicPlanner(
            {"opportunity_a": (_proposal(1),), "opportunity_b": ()}
        ),
        materializer=_Materializer(store),
        evaluator=_RejectedEvaluator(),
        archive=ParetoArchive(),
        candidate_dag=CandidateDag("baseline"),
        ledger=ledger,
        artifact_store=store,
        stop_policy=STOP_POLICY,
        recovery=handler,
    )

    result = asyncio.run(controller.run(_request()))
    record = tuple(ledger.iter_records("run_search_001"))[0]

    assert result.recovery_decision_ids == ("recovery_decision_cand_001",)
    assert record.failure_event_id == "failure_cand_001"
    assert record.recovery_decision_id == "recovery_decision_cand_001"
    recovery_artifact = next(
        item
        for item in result.artifact_refs
        if item.artifact_id.startswith("artifact_recovery_chain")
    )
    assert store.blob_path(recovery_artifact).read_bytes()


def test_search_never_exceeds_executed_candidate_budget(tmp_path: Path) -> None:
    proposals = tuple(_proposal(index) for index in range(1, 21))
    controller, materializer, _, _, ledger = _controller(tmp_path, proposals)

    result = asyncio.run(
        controller.run(_request(candidate_budget=3, formal_budget=3, physical_budget=3))
    )

    assert result.consumed_budgets.candidates == 3
    assert len(materializer.calls) == 3
    assert len(tuple(ledger.iter_records("run_search_001"))) == 3
    assert result.status == "BUDGET_EXHAUSTED"


def test_formal_and_physical_budgets_are_checked_before_execution(tmp_path: Path) -> None:
    evaluator = _Evaluator(
        cost=EvaluationCost(formal_jobs=1, physical_jobs=1, tokens=0, latency_ms=100)
    )
    controller, materializer, _, _, _ = _controller(
        tmp_path, tuple(_proposal(index) for index in range(1, 6)), evaluator
    )

    result = asyncio.run(
        controller.run(_request(candidate_budget=3, formal_budget=2, physical_budget=1))
    )

    assert result.consumed_budgets.formal_jobs == 1
    assert result.consumed_budgets.physical_jobs == 1
    assert len(evaluator.calls) == 1
    assert len(materializer.calls) == 1


def test_same_seed_produces_same_candidate_and_frontier_order(tmp_path: Path) -> None:
    proposals = tuple(_proposal(index) for index in range(1, 7))
    first, _, first_archive, first_dag, _ = _controller(tmp_path / "first", proposals)
    second, _, second_archive, second_dag, _ = _controller(tmp_path / "second", proposals)

    first_result = asyncio.run(first.run(_request()))
    second_result = asyncio.run(second.run(_request()))

    assert first_result.ordered_candidate_ids == second_result.ordered_candidate_ids
    assert first_result.pareto_candidate_ids == second_result.pareto_candidate_ids
    assert first_archive.snapshot().archive_hash == second_archive.snapshot().archive_hash
    assert first_dag.snapshot().dag_hash == second_dag.snapshot().dag_hash


def test_duplicate_proposal_and_source_identities_are_not_evaluated(
    tmp_path: Path,
) -> None:
    proposal = _proposal(1)
    duplicate_identity = proposal.model_copy(update={"proposal_id": "proposal_duplicate"})
    controller, _, _, dag, _ = _controller(
        tmp_path, (proposal, proposal, duplicate_identity)
    )

    result = asyncio.run(controller.run(_request()))

    assert result.consumed_budgets.candidates == 1
    assert len(dag.snapshot().candidates) == 1


def test_stop_policy_can_end_after_first_feasible_candidate(tmp_path: Path) -> None:
    policy = SearchStopPolicy.build(
        max_candidates_without_pareto_improvement=8,
        stop_on_first_feasible=True,
    )
    controller, _, _, _, _ = _controller(
        tmp_path,
        tuple(_proposal(index) for index in range(1, 6)),
        stop_policy=policy,
    )

    result = asyncio.run(controller.run(_request(stop_policy_hash=policy.policy_hash)))

    assert result.status == "STOPPED_BY_POLICY"
    assert result.stop_reason_code == "FIRST_FEASIBLE_ACCEPTED"
    assert result.consumed_budgets.candidates == 1


def test_request_must_bind_the_controller_stop_policy(tmp_path: Path) -> None:
    controller, _, _, _, _ = _controller(tmp_path, (_proposal(1),))

    with pytest.raises(SearchControllerError, match="stop policy"):
        asyncio.run(controller.run(_request(stop_policy_hash=_hash("f"))))
