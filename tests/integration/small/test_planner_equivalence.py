from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import (
    OptimizationProposal,
    ProposalCorrectness,
    ProposalPrediction,
    ProposalTarget,
    Transformation,
)
from nova_rtl.contracts.planning import PlannerRequest, PlannerResult
from nova_rtl.contracts.reporting import SearchRequest
from nova_rtl.planner.search import CanonicalPlannerAdapter, planned_candidate_from_proposal


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _proposal(proposal_id: str) -> OptimizationProposal:
    ref = EvidenceRef(
        evidence_id="source_span_target",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_target",
        snapshot_hash=_hash("a"),
    )
    return OptimizationProposal(
        proposal_id=proposal_id,
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        diagnosis_refs=("diagnosis_target",),
        target=ProposalTarget(
            hierarchy="nova_top.u_ingress",
            source_span_id="source_span_target",
            cone_fingerprint="cone:v1:target",
        ),
        transformation=Transformation(
            operation="RESTRUCTURE_PRIORITY_MUX",
            family="LOGIC_RESTRUCTURING",
            parameters={"max_branches": 8},
        ),
        preconditions=("NO_PROTECTED_NODE_IN_EDIT_SET",),
        correctness=ProposalCorrectness(
            contract="STRICT_SEQ_EQUIV",
            proof_scope="whole_design",
            reset_model="RESET_ASSUMPTIONS_LOCKED",
        ),
        prediction=ProposalPrediction(
            timing_direction="IMPROVE",
            area_direction="NEUTRAL",
            confidence=0.5,
        ),
        evidence_refs=(ref,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )


def _planner_request(mode: str) -> PlannerRequest:
    return PlannerRequest(
        planner_request_id=f"planner_request_{mode.lower()}",
        run_id="run_equivalence",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=_hash("c"),
        transform_registry_hash=_hash("d"),
        authorized_evidence_ids=("source_span_target",),
        planner_mode=mode,
        proposal_limit=1,
        token_budget=1000,
        latency_budget_ms=1000,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        deterministic_seed=4,
        required_output_schema_name="optimization-proposal",
        required_output_schema_version=2,
    )


class _Planner:
    def __init__(self, request: PlannerRequest, proposal: OptimizationProposal):
        self.request = request
        self.proposal = proposal

    async def propose(self, request: PlannerRequest) -> PlannerResult:
        assert request == self.request
        return PlannerResult(
            planner_result_id=f"planner_result_{request.planner_mode.lower()}",
            run_id=request.run_id,
            opportunity_id=request.opportunity_id,
            planner_mode=request.planner_mode,
            status="PASS",
            proposal_ids=(self.proposal.proposal_id,),
            rejected_output_diagnostics=(),
            context_pack_hashes=(),
            council_result_id=None,
            provider_id=None,
            model_id=None,
            prompt_hash=None,
            output_schema_hash=_hash("e"),
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            fallback_used=False,
            upstream_provider_result_id=None,
        )

    def proposals_for(self, result: PlannerResult):
        assert result.proposal_ids == (self.proposal.proposal_id,)
        return (self.proposal,)


def _search_request(mode: str) -> SearchRequest:
    return SearchRequest(
        search_request_id=f"search_request_{mode.lower()}",
        run_id="run_equivalence",
        design_contract_hash=_hash("b"),
        policy_hash=_hash("c"),
        transform_registry_hash=_hash("d"),
        ordered_opportunity_ids=("opportunity_target",),
        planner_mode=mode,
        required_correctness_class="PRIMARY_STRICT",
        candidate_budget=1,
        formal_budget=1,
        physical_budget=1,
        token_budget=1000,
        latency_budget_ms=1000,
        deterministic_seed=4,
        stop_policy_hash=_hash("f"),
        created_at=datetime.now(UTC),
    )


def test_same_normalized_proposal_has_same_execution_identity_across_planners() -> None:
    heuristic = _proposal("proposal_heuristic")
    single = _proposal("proposal_single_agent")

    heuristic_plan = planned_candidate_from_proposal(heuristic, priority=10.0)
    single_plan = planned_candidate_from_proposal(single, priority=10.0)

    assert heuristic_plan.transformation_hash == single_plan.transformation_hash
    assert heuristic_plan.proposal_hash == single_plan.proposal_hash


def test_canonical_adapter_preserves_planner_result_and_budget() -> None:
    request = _planner_request("SINGLE_AGENT")
    proposal = _proposal("proposal_single_agent")
    adapter = CanonicalPlannerAdapter(
        planners={request.opportunity_id: _Planner(request, proposal)},
        planner_requests={request.opportunity_id: request},
        priorities={request.opportunity_id: 10.0},
    )

    batch = asyncio.run(adapter.propose(_search_request("SINGLE_AGENT"), request.opportunity_id))

    assert batch.planner_result_ids == ("planner_result_single_agent",)
    assert batch.proposals[0].proposal_id == proposal.proposal_id
    assert batch.tokens == 0
