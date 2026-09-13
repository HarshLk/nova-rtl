"""Real M5 deterministic search over M3 authority and M4 candidate evidence."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal, Self

from pydantic import field_validator, model_validator

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.baseline.flow import BaselineRunIndex, load_run_index
from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    MetricSet,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import BenchmarkSnapshot
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.optimization import CandidateRecord, StableUpperString
from nova_rtl.contracts.reporting import ExperimentRecord, ParetoRecord, SearchRequest, SearchResult
from nova_rtl.evaluation.metrics import ComparableMetrics, MetricDelta, compare_metrics
from nova_rtl.evidence.opportunities import RankedOpportunitySet
from nova_rtl.optimization.flow import (
    M4CandidateBundle,
    M4StoredCandidateBundle,
    OptimizationFlowError,
    _load_m3_authority,
    optimize_strict_vertical_slice,
    verify_candidate_bundle,
)
from nova_rtl.search.controller import (
    CandidateEvaluation,
    DeterministicHeuristicPlanner,
    EvaluationCost,
    PlannedCandidate,
    SearchController,
    SearchStopPolicy,
    SearchTrace,
)
from nova_rtl.search.dag import CandidateDag, CandidateDagSnapshot
from nova_rtl.search.pareto import ParetoArchive, ParetoArchiveSnapshot
from nova_rtl.transforms.registry import competition_mvp_registry


class M5SearchFlowError(RuntimeError):
    """Trusted inputs cannot produce or reproduce a deterministic M5 search."""


class M5SearchBundle(StrictContract):
    """Self-contained canonical M5 search, ranking, and replay evidence."""

    schema_version: Literal[1] = 1
    run_id: EntityId
    parent_run_index_hash: HashRef
    implementation_hash: HashRef
    transform_registry_hash: HashRef
    planned_candidates: tuple[PlannedCandidate, ...]
    rejected_proposal_ids: tuple[EntityId, ...]
    proposal_dispositions: dict[EntityId, StableUpperString]
    search_request: SearchRequest
    search_result: SearchResult
    candidate_dag: CandidateDagSnapshot
    pareto_archive: ParetoArchiveSnapshot
    metric_deltas: dict[EntityId, MetricDelta]
    candidate_bundle_relative_paths: dict[EntityId, str]
    candidate_bundle_hashes: dict[EntityId, HashRef]
    valid_negative_candidate_ids: tuple[EntityId, ...]
    status: Literal["PASS"] = "PASS"
    bundle_hash: HashRef

    @field_validator(
        "metric_deltas",
        "candidate_bundle_relative_paths",
        "candidate_bundle_hashes",
        "proposal_dispositions",
    )
    @classmethod
    def maps_are_canonical(cls, value: dict[str, object]) -> dict[str, object]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def identities_are_closed_and_self_hashed(self) -> Self:
        proposal_ids = tuple(item.proposal_id for item in self.planned_candidates)
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("planned proposal identities must be unique")
        if set(self.proposal_dispositions) != set(proposal_ids):
            raise ValueError("every planned proposal requires one terminal disposition")
        expected_rejected = {
            proposal_id
            for proposal_id, disposition in self.proposal_dispositions.items()
            if disposition == "NO_SAFE_SOURCE_MATCH"
        }
        if set(self.rejected_proposal_ids) != expected_rejected:
            raise ValueError("rejected proposal identities and dispositions differ")
        if self.search_request.run_id != self.run_id:
            raise ValueError("search request run identity differs")
        if self.search_result.search_request_id != self.search_request.search_request_id:
            raise ValueError("search result does not bind its request")
        evaluated = set(self.search_result.ordered_candidate_ids)
        if set(self.candidate_dag.candidate_ids) != evaluated:
            raise ValueError("candidate DAG does not cover the exact evaluated set")
        for values, label in (
            (set(self.metric_deltas), "metric deltas"),
            (set(self.candidate_bundle_relative_paths), "candidate bundle paths"),
            (set(self.candidate_bundle_hashes), "candidate bundle hashes"),
        ):
            if values != evaluated:
                raise ValueError(f"{label} do not cover the exact evaluated set")
        if not set(self.valid_negative_candidate_ids).issubset(evaluated):
            raise ValueError("valid negative candidates must be evaluated")
        strict_frontier = self.pareto_archive.frontier_candidate_ids.get(
            "STRICT_SEQ_EQUIV", ()
        )
        if tuple(self.search_result.pareto_candidate_ids) != tuple(strict_frontier):
            raise ValueError("search result and strict Pareto frontier differ")
        if self.bundle_hash != canonical_sha256(self, exclude=frozenset({"bundle_hash"})):
            raise ValueError("M5 search bundle hash is not canonical")
        return self


def _implementation_hash(repository_root: Path) -> str:
    paths = (
        "src/nova_rtl/evaluation/metrics.py",
        "src/nova_rtl/optimization/search_flow.py",
        "src/nova_rtl/search/controller.py",
        "src/nova_rtl/search/pareto.py",
    )
    try:
        identities = {
            path: "sha256:"
            + sha256((repository_root / path).read_bytes()).hexdigest()
            for path in paths
        }
    except OSError as error:
        raise M5SearchFlowError(f"cannot hash M5 implementation: {error}") from error
    return canonical_sha256(identities)


def _read_stage(store: ArtifactStore, reference) -> StageResult:  # type: ignore[no-untyped-def]
    try:
        result = StageResult.model_validate_json(store.open_verified(reference).read())
        for raw in result.raw_artifacts:
            store.open_verified(raw).close()
        return result
    except (ArtifactStoreError, ValueError) as error:
        raise M5SearchFlowError(f"invalid stage result evidence: {error}") from error


def _composite_metrics(road: MetricSet, yosys: MetricSet) -> MetricSet:
    if yosys.mapped_area_um2 is None:
        raise M5SearchFlowError("Yosys result lacks required mapped area")
    payload = road.model_dump(mode="python")
    payload["mapped_area_um2"] = yosys.mapped_area_um2
    reasons = dict(road.missing_metric_reasons)
    reasons.pop("mapped_area_um2", None)
    payload["missing_metric_reasons"] = reasons
    return MetricSet.model_validate(payload)


def _power_activity_hash(index: BaselineRunIndex, store: ArtifactStore) -> str:
    try:
        snapshot = BenchmarkSnapshot.model_validate_json(
            store.open_verified(index.benchmark_snapshot_artifact).read()
        )
    except (ArtifactStoreError, ValueError) as error:
        raise M5SearchFlowError(f"benchmark activity identity is invalid: {error}") from error
    return snapshot.power_workload_hash


def _experiment(bundle: M4StoredCandidateBundle, store: ArtifactStore) -> ExperimentRecord:
    try:
        return ExperimentRecord.model_validate_json(
            store.open_verified(bundle.experiment_record_artifact).read()
        )
    except (ArtifactStoreError, ValueError) as error:
        raise M5SearchFlowError(f"candidate experiment record is invalid: {error}") from error


def _comparison_records(
    *,
    run_directory: Path,
    index: BaselineRunIndex,
    bundle: M4CandidateBundle,
    parent_store: ArtifactStore,
) -> tuple[MetricDelta, ParetoRecord]:
    candidate_run = run_directory / "m4" / "candidate-runs" / bundle.candidate_run_id
    candidate_store = ArtifactStore.open_existing(candidate_run / "artifacts")
    baseline_yosys = _read_stage(
        parent_store, index.stage_result_artifacts["stage_yosys"]
    ).metrics
    candidate_yosys = _read_stage(
        candidate_store, bundle.candidate_stage_result_artifacts["stage_yosys"]
    ).metrics
    baseline_metrics: dict[str, MetricSet] = {}
    candidate_metrics: dict[str, MetricSet] = {}
    metric_ids: dict[str, str] = {}
    for view_id in bundle.feasibility_policy.required_analysis_view_ids:
        stage_id = f"stage_openroad_{view_id}"
        baseline_road = _read_stage(parent_store, index.stage_result_artifacts[stage_id])
        candidate_road = _read_stage(
            candidate_store, bundle.candidate_stage_result_artifacts[stage_id]
        )
        baseline_metrics[view_id] = _composite_metrics(
            baseline_road.metrics, baseline_yosys
        )
        candidate_metrics[view_id] = _composite_metrics(
            candidate_road.metrics, candidate_yosys
        )
        metric_ids[view_id] = stage_id

    experiment = _experiment(bundle, parent_store)
    formal_hash = experiment.comparison_identity_hashes.get("formal_model")
    if formal_hash is None:
        raise M5SearchFlowError("candidate experiment lacks formal model identity")
    identities = {
        "analysis_view_set": bundle.feasibility_policy.baseline_analysis_view_set_hash,
        "constraint_source": bundle.feasibility_policy.baseline_constraint_source_hash,
        "power_activity": _power_activity_hash(index, parent_store),
        "platform_lock": index.platform_lock_hash,
        "constraint_binding": bundle.structural_comparison.binding_semantic_hash,
        "clock_graph": bundle.structural_comparison.clock_semantic_hash,
        "cdc_inventory": bundle.structural_comparison.cdc_semantic_hash,
        "formal_model": formal_hash,
    }
    patch = parent_store.open_verified(bundle.candidate.patch_artifact).read().decode(
        "utf-8", errors="strict"
    )
    changed_lines = sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    )
    common = {
        "correctness_contract": "STRICT_SEQ_EQUIV",
        "selection_class": "PRIMARY_STRICT",
        "required_view_metric_ids": metric_ids,
        "comparison_identity_hashes": identities,
        "binding_hash": bundle.structural_comparison.binding_semantic_hash,
        "clock_hash": bundle.structural_comparison.clock_semantic_hash,
        "cdc_hash": bundle.structural_comparison.cdc_semantic_hash,
        "formal_hash": formal_hash,
        "physical_stage": "OPENROAD_PHYSICAL",
    }
    baseline = ComparableMetrics(
        candidate_id=index.candidate_id,
        per_view_metrics=baseline_metrics,
        source_change_lines=0,
        evaluation_runtime_ms=sum(item.runtime_ms for item in baseline_metrics.values()),
        **common,
    )
    candidate = ComparableMetrics(
        candidate_id=bundle.candidate.candidate_id,
        per_view_metrics=candidate_metrics,
        source_change_lines=changed_lines,
        evaluation_runtime_ms=experiment.eda_runtime_ms,
        **common,
    )
    delta = compare_metrics(baseline, candidate)
    record = ParetoRecord(
        pareto_record_id=f"pareto_{bundle.candidate.candidate_id}",
        candidate_id=bundle.candidate.candidate_id,
        correctness_contract="STRICT_SEQ_EQUIV",
        selection_class="PRIMARY_STRICT",
        feasibility_predicate_version="feasibility-v1",
        required_view_metric_ids=metric_ids,
        metric_vector=delta.candidate_vector,
        dominated_candidate_ids=(),
        objective_policy_rank=1,
        binding_hash=bundle.structural_comparison.binding_semantic_hash,
        clock_hash=bundle.structural_comparison.clock_semantic_hash,
        cdc_hash=bundle.structural_comparison.cdc_semantic_hash,
        formal_hash=formal_hash,
        physical_stage="OPENROAD_PHYSICAL",
        comparison_identity_hashes=identities,
        recorded_at=bundle.candidate.created_at,
    )
    return delta, record


class _Materializer:
    def __init__(self, candidates: dict[str, CandidateRecord]) -> None:
        self._candidates = candidates

    async def materialize(self, proposal: PlannedCandidate) -> CandidateRecord | None:
        return self._candidates.get(proposal.proposal_id)


class _Evaluator:
    def __init__(
        self,
        *,
        bundles: dict[str, M4CandidateBundle],
        comparisons: dict[str, tuple[MetricDelta, ParetoRecord]],
        parent_store: ArtifactStore,
    ) -> None:
        self._bundles = bundles
        self._comparisons = comparisons
        self._store = parent_store

    def estimate(self, proposal: PlannedCandidate) -> EvaluationCost:
        attempted = proposal.proposal_id in self._bundles
        latency_ms = (
            _experiment(self._bundles[proposal.proposal_id], self._store).eda_runtime_ms
            if attempted
            else 0
        )
        return EvaluationCost(
            formal_jobs=1 if attempted else 0,
            physical_jobs=1 if attempted else 0,
            tokens=0,
            latency_ms=latency_ms,
        )

    async def evaluate(self, candidate: CandidateRecord) -> CandidateEvaluation:
        bundle = self._bundles[candidate.proposal_id]
        _, pareto = self._comparisons[candidate.candidate_id]
        experiment = _experiment(bundle, self._store)
        return CandidateEvaluation(
            candidate_id=candidate.candidate_id,
            experiment_record=experiment,
            pareto_record=pareto,
            artifact_refs=(
                bundle.candidate.rtl_snapshot_artifact,
                bundle.candidate.patch_artifact,
                bundle.proposal_artifact,
                bundle.experiment_record_artifact,
            ),
            consumed_cost=self.estimate(
                next(
                    item
                    for item in self._planned
                    if item.proposal_id == candidate.proposal_id
                )
            ),
            terminal_disposition=candidate.terminal_disposition,
        )

    def bind_plans(self, plans: tuple[PlannedCandidate, ...]) -> None:
        self._planned = plans


def _planned_candidates(
    *,
    ranked: RankedOpportunitySet,
    operations: tuple[str, ...],
    registry_hash: str,
    parent_candidate_id: str,
    m4_bundle: M4CandidateBundle | None,
) -> tuple[PlannedCandidate, ...]:
    selected: list[PlannedCandidate] = []
    for operation in operations:
        opportunity = next(
            (
                item
                for item in ranked.opportunities
                if item.editability == "RTL_EDITABLE"
                and operation in item.eligible_transform_families
            ),
            None,
        )
        if operation == "RESTRUCTURE_PRIORITY_MUX" and m4_bundle is not None:
            opportunity = next(
                item
                for item in ranked.opportunities
                if item.opportunity_id == m4_bundle.candidate.opportunity_id
            )
        if opportunity is None:
            continue
        transformation_hash = canonical_sha256(
            {
                "registry_hash": registry_hash,
                "operation": operation,
                "opportunity_id": opportunity.opportunity_id,
            }
        )
        proposal_id = (
            m4_bundle.candidate.proposal_id
            if operation == "RESTRUCTURE_PRIORITY_MUX" and m4_bundle is not None
            else "proposal_m5_" + transformation_hash.removeprefix("sha256:")[:24]
        )
        selected.append(
            PlannedCandidate.build(
                proposal_id=proposal_id,
                opportunity_id=opportunity.opportunity_id,
                parent_candidate_id=parent_candidate_id,
                operation=operation,
                transformation_hash=transformation_hash,
                priority=float(ranked.priority_scores[opportunity.opportunity_id]),
            )
        )
    return tuple(sorted(selected, key=lambda item: (item.opportunity_id, item.operation)))


def _normalize_operations(operations: Sequence[str]) -> tuple[str, ...]:
    registry = competition_mvp_registry()
    normalized = tuple(sorted(set(operations)))
    if not normalized:
        raise M5SearchFlowError("M5 search requires at least one transform operation")
    unknown = tuple(item for item in normalized if item not in registry.operations)
    if unknown:
        raise M5SearchFlowError(f"unregistered transform operation: {unknown[0]}")
    return normalized


def run_deterministic_search(
    run_directory: Path,
    *,
    repository_root: Path,
    operations: Sequence[str],
    max_candidates: int,
    formal_budget: int,
    physical_budget: int,
    token_budget: int,
    latency_budget_ms: int,
    deterministic_seed: int,
    stagnation_window: int,
) -> tuple[Path, M5SearchBundle]:
    """Execute one bounded search or verify and reuse its immutable result."""

    run_directory = run_directory.resolve()
    repository_root = repository_root.resolve()
    index = load_run_index(run_directory)
    if index.status != "PASS":
        raise M5SearchFlowError("M5 search requires a completed trustworthy baseline")
    normalized = _normalize_operations(operations)
    registry = competition_mvp_registry()
    implementation_hash = _implementation_hash(repository_root)
    stop_policy = SearchStopPolicy.build(
        max_candidates_without_pareto_improvement=stagnation_window,
        stop_on_first_feasible=False,
    )
    if formal_budget > max_candidates or physical_budget > formal_budget:
        raise M5SearchFlowError("search budgets must satisfy physical <= formal <= candidate")

    m4_path: Path | None = None
    m4_bundle: M4CandidateBundle | None = None
    if "RESTRUCTURE_PRIORITY_MUX" in normalized:
        if min(max_candidates, formal_budget, physical_budget) < 1:
            raise M5SearchFlowError("priority candidate requires candidate/formal/physical budget")
        try:
            m4_path, stored = optimize_strict_vertical_slice(
                run_directory, repository_root=repository_root
            )
        except (OptimizationFlowError, OSError, ValueError) as error:
            raise M5SearchFlowError(f"real strict candidate failed: {error}") from error
        if not isinstance(stored, M4CandidateBundle) or stored.status != "PASS":
            raise M5SearchFlowError("real strict candidate did not complete every M4 gate")
        m4_bundle = stored

    ranked, _, _, _ = _load_m3_authority(index, run_directory)
    planned = _planned_candidates(
        ranked=ranked,
        operations=normalized,
        registry_hash=registry.registry_hash,
        parent_candidate_id=index.candidate_id,
        m4_bundle=m4_bundle,
    )
    if not planned:
        raise M5SearchFlowError("no M3-ranked opportunity matches the requested transforms")
    identity = canonical_sha256(
        {
            "run_index_hash": index.index_hash,
            "implementation_hash": implementation_hash,
            "registry_hash": registry.registry_hash,
            "operations": normalized,
            "budgets": {
                "candidates": max_candidates,
                "formal": formal_budget,
                "physical": physical_budget,
                "tokens": token_budget,
                "latency_ms": latency_budget_ms,
            },
            "seed": deterministic_seed,
            "stop_policy_hash": stop_policy.policy_hash,
        }
    )
    search_id = "search_" + identity.removeprefix("sha256:")[:24]
    output = run_directory / "m5" / "searches" / search_id
    bundle_path = output / "search-bundle.json"
    if bundle_path.is_file():
        return bundle_path, verify_deterministic_search(
            bundle_path, repository_root=repository_root
        )

    parent_store = ArtifactStore(run_directory / "artifacts")
    bundles_by_proposal: dict[str, M4CandidateBundle] = {}
    candidates_by_proposal: dict[str, CandidateRecord] = {}
    comparisons: dict[str, tuple[MetricDelta, ParetoRecord]] = {}
    relative_paths: dict[str, str] = {}
    bundle_hashes: dict[str, str] = {}
    if m4_bundle is not None and m4_path is not None:
        proposal_id = m4_bundle.candidate.proposal_id
        bundles_by_proposal[proposal_id] = m4_bundle
        candidates_by_proposal[proposal_id] = m4_bundle.candidate
        comparisons[m4_bundle.candidate.candidate_id] = _comparison_records(
            run_directory=run_directory,
            index=index,
            bundle=m4_bundle,
            parent_store=parent_store,
        )
        relative_paths[m4_bundle.candidate.candidate_id] = str(
            m4_path.resolve().relative_to(run_directory)
        )
        bundle_hashes[m4_bundle.candidate.candidate_id] = m4_bundle.bundle_hash

    opportunity_ids = tuple(
        dict.fromkeys(item.opportunity_id for item in planned)
    )
    request = SearchRequest(
        search_request_id=f"search_request_{search_id.removeprefix('search_')}",
        run_id=index.run_id,
        design_contract_hash=index.design_contract_hash,
        policy_hash=ranked.policy_hash,
        transform_registry_hash=registry.registry_hash,
        ordered_opportunity_ids=opportunity_ids,
        planner_mode="HEURISTIC",
        required_correctness_class="PRIMARY_STRICT",
        candidate_budget=max_candidates,
        formal_budget=formal_budget,
        physical_budget=physical_budget,
        token_budget=token_budget,
        latency_budget_ms=latency_budget_ms,
        deterministic_seed=deterministic_seed,
        stop_policy_hash=stop_policy.policy_hash,
        created_at=index.updated_at,
    )
    grouped = {
        opportunity_id: tuple(
            item for item in planned if item.opportunity_id == opportunity_id
        )
        for opportunity_id in opportunity_ids
    }
    evaluator = _Evaluator(
        bundles=bundles_by_proposal,
        comparisons=comparisons,
        parent_store=parent_store,
    )
    evaluator.bind_plans(planned)
    archive = ParetoArchive()
    dag = CandidateDag(index.candidate_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{search_id}.", dir=output.parent))
    try:
        ledger = ExperimentLedger(
            staging / "experiment-ledger.sqlite3", artifact_store=parent_store
        )
        result = asyncio.run(
            SearchController(
                planner=DeterministicHeuristicPlanner(grouped),
                materializer=_Materializer(candidates_by_proposal),
                evaluator=evaluator,
                archive=archive,
                candidate_dag=dag,
                ledger=ledger,
                artifact_store=parent_store,
                stop_policy=stop_policy,
            ).run(request)
        )
        if not result.ordered_candidate_ids or result.selected_candidate_id is None:
            raise M5SearchFlowError("bounded search produced no verified strict candidate")
        trace_ref = next(
            item
            for item in result.artifact_refs
            if item.artifact_id.startswith("artifact_search_trace_")
        )
        trace = SearchTrace.model_validate_json(
            parent_store.open_verified(trace_ref).read()
        )
        valid_negative = tuple(
            item.candidate.candidate_id
            for item in (m4_bundle,)
            if item is not None
            if item.candidate.classification == "VALID_NEGATIVE_RESULT"
        )
        payload = {
            "schema_version": 1,
            "run_id": index.run_id,
            "parent_run_index_hash": index.index_hash,
            "implementation_hash": implementation_hash,
            "transform_registry_hash": registry.registry_hash,
            "planned_candidates": planned,
            "rejected_proposal_ids": trace.rejected_proposal_ids,
            "proposal_dispositions": trace.proposal_dispositions,
            "search_request": request,
            "search_result": result,
            "candidate_dag": dag.snapshot(),
            "pareto_archive": archive.snapshot(),
            "metric_deltas": {
                candidate_id: comparisons[candidate_id][0]
                for candidate_id in result.ordered_candidate_ids
            },
            "candidate_bundle_relative_paths": relative_paths,
            "candidate_bundle_hashes": bundle_hashes,
            "valid_negative_candidate_ids": valid_negative,
            "status": "PASS",
        }
        provisional = M5SearchBundle.model_construct(
            **payload, bundle_hash="sha256:" + "0" * 64
        )
        bundle = M5SearchBundle(
            **payload,
            bundle_hash=canonical_sha256(
                provisional, exclude=frozenset({"bundle_hash"})
            ),
        )
        staging_bundle = staging / "search-bundle.json"
        staging_bundle.write_bytes(canonical_json_bytes(bundle) + b"\n")
        verify_deterministic_search(staging_bundle, repository_root=repository_root)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return bundle_path, verify_deterministic_search(
        bundle_path, repository_root=repository_root
    )


def verify_deterministic_search(
    bundle_path: Path, *, repository_root: Path
) -> M5SearchBundle:
    """Independently reconstruct M5 search state without invoking an EDA tool."""

    try:
        bundle = M5SearchBundle.model_validate_json(bundle_path.read_bytes())
    except (OSError, ValueError) as error:
        raise M5SearchFlowError(f"M5 search bundle is invalid: {error}") from error
    run_directory = bundle_path.resolve().parents[3]
    index = load_run_index(run_directory)
    if index.run_id != bundle.run_id or index.index_hash != bundle.parent_run_index_hash:
        raise M5SearchFlowError("M5 search parent run identity changed")
    if _implementation_hash(repository_root.resolve()) != bundle.implementation_hash:
        raise M5SearchFlowError("M5 search implementation identity changed")
    registry = competition_mvp_registry()
    if registry.registry_hash != bundle.transform_registry_hash:
        raise M5SearchFlowError("M5 transform registry identity changed")
    parent_store = ArtifactStore.open_existing(run_directory / "artifacts")
    for reference in bundle.search_result.artifact_refs:
        parent_store.open_verified(reference).close()
    trace_refs = tuple(
        item
        for item in bundle.search_result.artifact_refs
        if item.artifact_id.startswith("artifact_search_trace_")
    )
    if len(trace_refs) != 1:
        raise M5SearchFlowError("M5 search result lacks one canonical trace")
    try:
        trace = SearchTrace.model_validate_json(
            parent_store.open_verified(trace_refs[0]).read()
        )
    except (ArtifactStoreError, ValueError) as error:
        raise M5SearchFlowError(f"M5 search trace is invalid: {error}") from error
    if (
        trace.search_request_id != bundle.search_request.search_request_id
        or trace.ordered_proposal_ids
        != tuple(
            item.proposal_id
            for item in sorted(
                bundle.planned_candidates,
                key=lambda item: (
                    -item.priority,
                    sha256(
                        f"{bundle.search_request.deterministic_seed}:"
                        f"{item.proposal_hash}".encode()
                    ).hexdigest(),
                    item.proposal_id,
                ),
            )
        )
        or trace.rejected_proposal_ids != bundle.rejected_proposal_ids
        or trace.proposal_dispositions != bundle.proposal_dispositions
        or trace.evaluated_candidate_ids
        != bundle.search_result.ordered_candidate_ids
        or trace.feasible_candidate_ids
        != bundle.search_result.feasible_candidate_ids
        or trace.consumed_budgets != bundle.search_result.consumed_budgets
        or trace.candidate_dag_hash != bundle.candidate_dag.dag_hash
        or trace.pareto_archive_hash != bundle.pareto_archive.archive_hash
        or trace.stop_reason_code != bundle.search_result.stop_reason_code
    ):
        raise M5SearchFlowError("M5 search trace reconstruction changed")

    dag = CandidateDag(index.candidate_id)
    archive = ParetoArchive()
    rebuilt_deltas: dict[str, MetricDelta] = {}
    rebuilt_paths: dict[str, str] = {}
    rebuilt_hashes: dict[str, str] = {}
    for candidate_id in bundle.search_result.ordered_candidate_ids:
        relative = Path(bundle.candidate_bundle_relative_paths[candidate_id])
        if relative.is_absolute() or ".." in relative.parts:
            raise M5SearchFlowError("candidate bundle path escapes the run directory")
        path = run_directory / relative
        try:
            stored = verify_candidate_bundle(path, repository_root=repository_root)
        except (OptimizationFlowError, OSError, ValueError) as error:
            raise M5SearchFlowError(f"candidate bundle verification failed: {error}") from error
        if not isinstance(stored, M4CandidateBundle):
            raise M5SearchFlowError("M5 frontier candidate is not a completed M4 bundle")
        if stored.candidate.candidate_id != candidate_id:
            raise M5SearchFlowError("candidate bundle identity changed")
        if stored.bundle_hash != bundle.candidate_bundle_hashes[candidate_id]:
            raise M5SearchFlowError("candidate bundle hash changed")
        dag.add(stored.candidate)
        delta, record = _comparison_records(
            run_directory=run_directory,
            index=index,
            bundle=stored,
            parent_store=parent_store,
        )
        archive.add(record)
        rebuilt_deltas[candidate_id] = delta
        rebuilt_paths[candidate_id] = str(relative)
        rebuilt_hashes[candidate_id] = stored.bundle_hash
    if dag.snapshot() != bundle.candidate_dag:
        raise M5SearchFlowError("candidate DAG reconstruction changed")
    if archive.snapshot() != bundle.pareto_archive:
        raise M5SearchFlowError("Pareto archive reconstruction changed")
    if rebuilt_deltas != bundle.metric_deltas:
        raise M5SearchFlowError("metric comparison reconstruction changed")
    if rebuilt_paths != bundle.candidate_bundle_relative_paths:
        raise M5SearchFlowError("candidate path reconstruction changed")
    if rebuilt_hashes != bundle.candidate_bundle_hashes:
        raise M5SearchFlowError("candidate hash reconstruction changed")
    ledger = ExperimentLedger.open_existing(
        bundle_path.parent / "experiment-ledger.sqlite3", artifact_store=parent_store
    )
    if tuple(item.candidate_id for item in ledger.iter_records(bundle.run_id)) != (
        bundle.search_result.ordered_candidate_ids
    ):
        raise M5SearchFlowError("M5 experiment ledger reconstruction changed")
    return bundle


__all__ = [
    "M5SearchBundle",
    "M5SearchFlowError",
    "run_deterministic_search",
    "verify_deterministic_search",
]
