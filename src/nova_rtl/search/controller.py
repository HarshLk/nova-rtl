"""Bounded deterministic search orchestration over trusted candidate callbacks."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    FiniteFloat,
    HashRef,
    NonNegativeInt,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.optimization import CandidateRecord, TransformOperation
from nova_rtl.contracts.reporting import (
    ConsumedSearchBudgets,
    ExperimentRecord,
    ParetoRecord,
    SearchRequest,
    SearchResult,
)
from nova_rtl.search.dag import CandidateDag
from nova_rtl.search.pareto import ParetoArchive


class SearchControllerError(RuntimeError):
    """A callback violated deterministic search or resource accounting contracts."""


class SearchStopPolicy(StrictContract):
    """Deterministic early-stop rules bound into every SearchRequest."""

    schema_version: Literal[1] = 1
    max_candidates_without_pareto_improvement: int = Field(strict=True, gt=0)
    stop_on_first_feasible: bool
    policy_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.policy_hash != canonical_sha256(self, exclude=frozenset({"policy_hash"})):
            raise ValueError("search stop policy hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> SearchStopPolicy:
        payload = {"schema_version": 1, **values}
        return cls(**payload, policy_hash=canonical_sha256(payload))


class PlannedCandidate(StrictContract):
    """Minimal deterministic unit emitted by a search planner."""

    schema_version: Literal[1] = 1
    proposal_id: EntityId
    opportunity_id: EntityId
    parent_candidate_id: EntityId
    operation: TransformOperation
    transformation_hash: HashRef
    priority: FiniteFloat
    proposal_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.proposal_hash != canonical_sha256(
            self, exclude=frozenset({"proposal_hash", "proposal_id"})
        ):
            raise ValueError("planned candidate hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> PlannedCandidate:
        payload = {"schema_version": 1, **values}
        semantic_payload = {
            key: value for key, value in payload.items() if key != "proposal_id"
        }
        return cls(**payload, proposal_hash=canonical_sha256(semantic_payload))


class PlannerBatch(StrictContract):
    """One opportunity's bounded planner output and exact resource use."""

    proposals: tuple[PlannedCandidate, ...]
    planner_result_ids: tuple[EntityId, ...]
    council_result_ids: tuple[EntityId, ...]
    tokens: NonNegativeInt
    latency_ms: NonNegativeInt

    @field_validator("planner_result_ids", "council_result_ids")
    @classmethod
    def identities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("planner result identities must be unique")
        return value


class EvaluationCost(StrictContract):
    """Cost known before an evaluator is authorized to execute."""

    formal_jobs: NonNegativeInt
    physical_jobs: NonNegativeInt
    tokens: NonNegativeInt
    latency_ms: NonNegativeInt

    @model_validator(mode="after")
    def cascade_is_coherent(self) -> Self:
        if self.physical_jobs > self.formal_jobs:
            raise ValueError("physical job count cannot exceed formal job count")
        return self


class CandidateEvaluation(StrictContract):
    """Authoritative terminal evaluation returned to the controller."""

    candidate_id: EntityId
    experiment_record: ExperimentRecord
    pareto_record: ParetoRecord | None
    artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    consumed_cost: EvaluationCost
    terminal_disposition: str

    @model_validator(mode="after")
    def identities_are_coherent(self) -> Self:
        if self.experiment_record.candidate_id != self.candidate_id:
            raise ValueError("experiment record candidate identity differs")
        if (
            self.pareto_record is not None
            and self.pareto_record.candidate_id != self.candidate_id
        ):
            raise ValueError("Pareto record candidate identity differs")
        artifact_ids = tuple(item.artifact_id for item in self.artifact_refs)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("candidate evaluation artifact identities must be unique")
        return self


class SearchTrace(StrictContract):
    """Canonical replay summary persisted as the primary SearchResult artifact."""

    schema_version: Literal[1] = 1
    search_request_id: EntityId
    ordered_proposal_ids: tuple[EntityId, ...]
    evaluated_candidate_ids: tuple[EntityId, ...]
    feasible_candidate_ids: tuple[EntityId, ...]
    consumed_budgets: ConsumedSearchBudgets
    candidate_dag_hash: HashRef
    pareto_archive_hash: HashRef
    stop_reason_code: str
    trace_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.trace_hash != canonical_sha256(self, exclude=frozenset({"trace_hash"})):
            raise ValueError("search trace hash is not canonical")
        return self


class SearchPlanner(Protocol):
    async def propose(self, request: SearchRequest, opportunity_id: str) -> PlannerBatch: ...


class CandidateMaterializer(Protocol):
    async def materialize(self, proposal: PlannedCandidate) -> CandidateRecord: ...


class CandidateEvaluator(Protocol):
    def estimate(self, proposal: PlannedCandidate) -> EvaluationCost: ...

    async def evaluate(self, candidate: CandidateRecord) -> CandidateEvaluation: ...


class DeterministicHeuristicPlanner:
    """Return pre-authorized heuristic proposals in canonical identity order."""

    def __init__(
        self, proposals_by_opportunity: Mapping[str, tuple[PlannedCandidate, ...]]
    ) -> None:
        self._proposals = {
            key: tuple(sorted(value, key=lambda item: item.proposal_id))
            for key, value in sorted(proposals_by_opportunity.items())
        }

    async def propose(self, request: SearchRequest, opportunity_id: str) -> PlannerBatch:
        if request.planner_mode != "HEURISTIC":
            raise SearchControllerError(
                "deterministic heuristic planner requires HEURISTIC mode"
            )
        proposals = self._proposals.get(opportunity_id, ())
        if any(item.opportunity_id != opportunity_id for item in proposals):
            raise SearchControllerError("heuristic proposal is bound to the wrong opportunity")
        return PlannerBatch(
            proposals=proposals,
            planner_result_ids=(),
            council_result_ids=(),
            tokens=0,
            latency_ms=0,
        )


def _tie_key(proposal: PlannedCandidate, seed: int) -> str:
    return sha256(f"{seed}:{proposal.proposal_hash}".encode()).hexdigest()


def _fits(
    request: SearchRequest, used: ConsumedSearchBudgets, cost: EvaluationCost
) -> bool:
    return all(
        (
            used.candidates + 1 <= request.candidate_budget,
            used.formal_jobs + cost.formal_jobs <= request.formal_budget,
            used.physical_jobs + cost.physical_jobs <= request.physical_budget,
            used.tokens + cost.tokens <= request.token_budget,
            used.latency_ms + cost.latency_ms <= request.latency_budget_ms,
        )
    )


class SearchController:
    """Plan, deduplicate, materialize, evaluate, ledger, rank, and stop."""

    def __init__(
        self,
        *,
        planner: SearchPlanner,
        materializer: CandidateMaterializer,
        evaluator: CandidateEvaluator,
        archive: ParetoArchive,
        candidate_dag: CandidateDag,
        ledger: ExperimentLedger,
        artifact_store: ArtifactStore,
        stop_policy: SearchStopPolicy,
    ) -> None:
        self._planner = planner
        self._materializer = materializer
        self._evaluator = evaluator
        self._archive = archive
        self._candidate_dag = candidate_dag
        self._ledger = ledger
        self._artifact_store = artifact_store
        self._stop_policy = stop_policy

    async def run(self, request: SearchRequest) -> SearchResult:
        if request.stop_policy_hash != self._stop_policy.policy_hash:
            raise SearchControllerError("search request does not bind the active stop policy")
        proposals: list[PlannedCandidate] = []
        planner_ids: list[str] = []
        council_ids: list[str] = []
        planner_tokens = 0
        planner_latency = 0
        for opportunity_id in request.ordered_opportunity_ids:
            batch = await self._planner.propose(request, opportunity_id)
            planner_tokens += batch.tokens
            planner_latency += batch.latency_ms
            if (
                planner_tokens > request.token_budget
                or planner_latency > request.latency_budget_ms
            ):
                raise SearchControllerError("planner exceeded its declared search budget")
            proposals.extend(batch.proposals)
            planner_ids.extend(batch.planner_result_ids)
            council_ids.extend(batch.council_result_ids)

        ordered = sorted(
            proposals,
            key=lambda item: (
                -item.priority,
                _tie_key(item, request.deterministic_seed),
                item.proposal_id,
            ),
        )
        unique: list[PlannedCandidate] = []
        proposal_hashes: set[str] = set()
        for proposal in ordered:
            if proposal.proposal_hash not in proposal_hashes:
                proposal_hashes.add(proposal.proposal_hash)
                unique.append(proposal)

        used = ConsumedSearchBudgets(
            candidates=0,
            formal_jobs=0,
            physical_jobs=0,
            tokens=planner_tokens,
            latency_ms=planner_latency,
        )
        evaluated_ids: list[str] = []
        feasible_ids: list[str] = []
        source_hashes: set[str] = set()
        patch_hashes: set[str] = set()
        artifacts: dict[str, ArtifactRef] = {}
        budget_exhausted = False
        stopped_by_policy = False
        policy_stop_reason = ""
        candidates_without_improvement = 0

        for proposal in unique:
            estimated = self._evaluator.estimate(proposal)
            if not _fits(request, used, estimated):
                budget_exhausted = True
                break
            candidate = await self._materializer.materialize(proposal)
            if candidate.proposal_id != proposal.proposal_id:
                raise SearchControllerError("materialized candidate does not bind its proposal")
            if (
                candidate.source_hash in source_hashes
                or candidate.patch_artifact.sha256 in patch_hashes
            ):
                continue
            self._candidate_dag.add(candidate)
            source_hashes.add(candidate.source_hash)
            patch_hashes.add(candidate.patch_artifact.sha256)

            evaluation = await self._evaluator.evaluate(candidate)
            if evaluation.candidate_id != candidate.candidate_id:
                raise SearchControllerError("evaluator returned the wrong candidate identity")
            if evaluation.consumed_cost != estimated:
                raise SearchControllerError(
                    "evaluator cost differs from its pre-execution estimate"
                )
            self._ledger.append_record(evaluation.experiment_record)
            if evaluation.pareto_record is not None:
                prior_frontier = tuple(
                    item.candidate_id
                    for item in self._archive.frontier(
                        evaluation.pareto_record.correctness_contract
                    )
                )
                self._archive.add(evaluation.pareto_record)
                feasible_ids.append(candidate.candidate_id)
                current_frontier = tuple(
                    item.candidate_id
                    for item in self._archive.frontier(
                        evaluation.pareto_record.correctness_contract
                    )
                )
                if current_frontier != prior_frontier:
                    candidates_without_improvement = 0
                else:
                    candidates_without_improvement += 1
            else:
                candidates_without_improvement += 1
            for ref in evaluation.artifact_refs:
                previous = artifacts.get(ref.artifact_id)
                if previous is not None and previous != ref:
                    raise SearchControllerError(
                        "artifact identity resolves to conflicting evidence"
                    )
                artifacts[ref.artifact_id] = ref
            evaluated_ids.append(candidate.candidate_id)
            used = ConsumedSearchBudgets(
                candidates=used.candidates + 1,
                formal_jobs=used.formal_jobs + estimated.formal_jobs,
                physical_jobs=used.physical_jobs + estimated.physical_jobs,
                tokens=used.tokens + estimated.tokens,
                latency_ms=used.latency_ms + estimated.latency_ms,
            )
            if evaluation.pareto_record is not None and self._stop_policy.stop_on_first_feasible:
                stopped_by_policy = True
                policy_stop_reason = "FIRST_FEASIBLE_ACCEPTED"
                break
            if (
                candidates_without_improvement
                >= self._stop_policy.max_candidates_without_pareto_improvement
            ):
                stopped_by_policy = True
                policy_stop_reason = "PARETO_STAGNATION"
                break

        contract = {
            "PRIMARY_STRICT": "STRICT_SEQ_EQUIV",
            "SECONDARY_RETIMED": "RETIMING_EQUIV",
            "EXPLORATORY_LATENCY_AWARE": "LATENCY_AWARE",
        }[request.required_correctness_class]
        frontier = self._archive.frontier(contract)  # type: ignore[arg-type]
        frontier_ids = tuple(item.candidate_id for item in frontier)
        selected = (
            min(
                frontier,
                key=lambda item: (item.objective_policy_rank, item.candidate_id),
            ).candidate_id
            if frontier
            else None
        )
        if stopped_by_policy:
            status = "STOPPED_BY_POLICY"
            stop_reason = policy_stop_reason
        elif budget_exhausted:
            status = "BUDGET_EXHAUSTED"
            stop_reason = "SEARCH_BUDGET_EXHAUSTED"
        elif selected is None:
            status = "NO_FEASIBLE_CANDIDATE"
            stop_reason = "NO_FEASIBLE_CANDIDATE"
        else:
            status = "COMPLETED"
            stop_reason = "OPPORTUNITIES_EXHAUSTED"

        trace_payload = {
            "schema_version": 1,
            "search_request_id": request.search_request_id,
            "ordered_proposal_ids": tuple(item.proposal_id for item in unique),
            "evaluated_candidate_ids": tuple(evaluated_ids),
            "feasible_candidate_ids": tuple(feasible_ids),
            "consumed_budgets": used.model_dump(mode="json"),
            "candidate_dag_hash": self._candidate_dag.snapshot().dag_hash,
            "pareto_archive_hash": self._archive.snapshot().archive_hash,
            "stop_reason_code": stop_reason,
        }
        trace = SearchTrace(**trace_payload, trace_hash=canonical_sha256(trace_payload))
        trace_ref = self._artifact_store.put_named_bytes(
            canonical_json_bytes(trace),
            artifact_id=f"artifact_search_trace_{trace.trace_hash[-16:]}",
            media_type="application/json",
            classification="INTERNAL",
            producer_stage_result_id=None,
        )
        artifacts[trace_ref.artifact_id] = trace_ref
        result_id = f"search_result_{trace.trace_hash[-16:]}"
        return SearchResult(
            search_result_id=result_id,
            search_request_id=request.search_request_id,
            status=status,  # type: ignore[arg-type]
            ordered_candidate_ids=tuple(evaluated_ids),
            feasible_candidate_ids=tuple(feasible_ids),
            pareto_candidate_ids=frontier_ids,
            selected_candidate_id=selected,
            stop_reason_code=stop_reason,
            consumed_budgets=used,
            planner_result_ids=tuple(dict.fromkeys(planner_ids)),
            council_result_ids=tuple(dict.fromkeys(council_ids)),
            recovery_decision_ids=(),
            event_sequence_range=(0, len(evaluated_ids)),
            artifact_refs=tuple(artifacts[key] for key in sorted(artifacts)),
            completed_at=request.created_at,
        )


__all__ = [
    "CandidateEvaluation",
    "CandidateEvaluator",
    "CandidateMaterializer",
    "DeterministicHeuristicPlanner",
    "EvaluationCost",
    "PlannedCandidate",
    "PlannerBatch",
    "SearchController",
    "SearchControllerError",
    "SearchPlanner",
    "SearchStopPolicy",
    "SearchTrace",
]
