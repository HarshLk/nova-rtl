"""Adapter from canonical planner results to deterministic M5 search units."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.optimization import OptimizationProposal
from nova_rtl.contracts.planning import PlannerRequest, PlannerResult
from nova_rtl.contracts.reporting import SearchRequest
from nova_rtl.planner.interface import Planner
from nova_rtl.search.controller import PlannedCandidate, PlannerBatch, SearchControllerError


class ProposalResolvingPlanner(Planner, Protocol):
    def proposals_for(
        self, result: PlannerResult
    ) -> tuple[OptimizationProposal, ...]: ...


def planned_candidate_from_proposal(
    proposal: OptimizationProposal, *, priority: float
) -> PlannedCandidate:
    """Normalize advisory provenance away from deterministic execution identity."""

    transformation_hash = canonical_sha256(
        {
            "parent_candidate_id": proposal.parent_candidate_id,
            "opportunity_id": proposal.opportunity_id,
            "target": proposal.target.model_dump(mode="json"),
            "transformation": proposal.transformation.model_dump(mode="json"),
            "correctness": proposal.correctness.model_dump(mode="json"),
        }
    )
    return PlannedCandidate.build(
        proposal_id=proposal.proposal_id,
        opportunity_id=proposal.opportunity_id,
        parent_candidate_id=proposal.parent_candidate_id,
        operation=proposal.transformation.operation,
        transformation_hash=transformation_hash,
        priority=priority,
    )


class CanonicalPlannerAdapter:
    """Invoke canonical planners while satisfying the M5 SearchPlanner protocol."""

    def __init__(
        self,
        *,
        planners: Mapping[str, ProposalResolvingPlanner],
        planner_requests: Mapping[str, PlannerRequest],
        priorities: Mapping[str, float],
    ) -> None:
        keys = set(planners)
        if keys != set(planner_requests) or keys != set(priorities):
            raise ValueError("planner adapter maps must cover the same opportunities")
        self._planners = dict(planners)
        self._requests = dict(planner_requests)
        self._priorities = dict(priorities)
        self._results: list[PlannerResult] = []

    @property
    def planner_results(self) -> tuple[PlannerResult, ...]:
        return tuple(self._results)

    async def propose(
        self, request: SearchRequest, opportunity_id: str
    ) -> PlannerBatch:
        try:
            planner = self._planners[opportunity_id]
            planner_request = self._requests[opportunity_id]
        except KeyError as error:
            raise SearchControllerError(
                "search requested an unknown planner opportunity"
            ) from error
        expected = {
            "run_id": request.run_id,
            "opportunity_id": opportunity_id,
            "design_contract_hash": request.design_contract_hash,
            "policy_hash": request.policy_hash,
            "transform_registry_hash": request.transform_registry_hash,
            "planner_mode": request.planner_mode,
        }
        if any(getattr(planner_request, key) != value for key, value in expected.items()):
            raise SearchControllerError("canonical planner request differs from search authority")
        result = await planner.propose(planner_request)
        if (
            result.run_id != request.run_id
            or result.opportunity_id != opportunity_id
            or result.planner_mode != request.planner_mode
        ):
            raise SearchControllerError("canonical planner returned mismatched lineage")
        self._results.append(result)
        proposals = planner.proposals_for(result)
        if tuple(item.proposal_id for item in proposals) != result.proposal_ids:
            raise SearchControllerError("planner proposal artifacts differ from PlannerResult")
        normalized = tuple(
            planned_candidate_from_proposal(
                item, priority=float(self._priorities[opportunity_id])
            )
            for item in proposals
        )
        return PlannerBatch(
            proposals=normalized,
            planner_result_ids=(result.planner_result_id,),
            council_result_ids=(
                (result.council_result_id,) if result.council_result_id else ()
            ),
            tokens=result.input_tokens + result.output_tokens,
            latency_ms=result.latency_ms,
        )


__all__ = [
    "CanonicalPlannerAdapter",
    "ProposalResolvingPlanner",
    "planned_candidate_from_proposal",
]
