from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import (
    OpportunitySeverity,
    OptimizationOpportunity,
    OptimizationProposal,
    ProposalCorrectness,
    ProposalPrediction,
    ProposalTarget,
    RootCause,
    Transformation,
)
from nova_rtl.contracts.planning import PlannerRequest
from nova_rtl.planner.heuristic import HeuristicPlanner, PlannerIntegrityError
from nova_rtl.transforms.registry import competition_mvp_registry


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _fixture():
    registry = competition_mvp_registry()
    evidence = EvidenceRef(
        evidence_id="source_span_target",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_target",
        snapshot_hash=_hash("a"),
    )
    opportunity = OptimizationOpportunity(
        opportunity_id="opportunity_target",
        parent_candidate_id="baseline",
        target_domain="ingress_domain",
        target_analysis_view_id="asap7_setup",
        affected_analysis_view_ids=("asap7_setup",),
        root_causes=(RootCause(category="DEEP_PRIORITY_CHAIN", confidence=0.9),),
        severity=OpportunitySeverity(
            worst_view_id="asap7_setup",
            worst_slack_ns=-0.4,
            affected_endpoints=2,
            tns_share_percent=10.0,
        ),
        editability="RTL_EDITABLE",
        source_spans=("source_span_target",),
        protected_neighbors=(),
        eligible_transform_families=("RESTRUCTURE_PRIORITY_MUX",),
        proof_contracts=("STRICT_SEQ_EQUIV",),
        evidence_refs=(evidence,),
    )
    proposal = OptimizationProposal(
        proposal_id="proposal_heuristic_target",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        diagnosis_refs=("diagnosis_heuristic",),
        target=ProposalTarget(
            hierarchy="nova_top.u_ingress",
            source_span_id="source_span_target",
            cone_fingerprint="cone:v1:priority_mux",
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
            confidence=0.6,
        ),
        evidence_refs=(evidence,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )
    request = PlannerRequest(
        planner_request_id="planner_request_heuristic",
        run_id="run_planner",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=_hash("c"),
        transform_registry_hash=registry.registry_hash,
        authorized_evidence_ids=("source_span_target",),
        planner_mode="HEURISTIC",
        proposal_limit=1,
        token_budget=1,
        latency_budget_ms=1,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        deterministic_seed=8,
        required_output_schema_name="optimization-proposal",
        required_output_schema_version=2,
    )
    return registry, opportunity, proposal, request


def test_heuristic_planner_returns_canonical_registered_proposal() -> None:
    registry, opportunity, proposal, request = _fixture()
    planner = HeuristicPlanner(
        opportunities=(opportunity,), proposals=(proposal,), registry=registry
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "PASS"
    assert result.planner_mode == "HEURISTIC"
    assert result.proposal_ids == (proposal.proposal_id,)
    assert planner.proposals_for(result) == (proposal,)
    assert result.provider_id is None
    assert result.input_tokens == result.output_tokens == 0


def test_heuristic_planner_fails_closed_on_registry_mismatch() -> None:
    registry, opportunity, proposal, request = _fixture()
    planner = HeuristicPlanner(
        opportunities=(opportunity,), proposals=(proposal,), registry=registry
    )

    with pytest.raises(PlannerIntegrityError, match="registry"):
        asyncio.run(
            planner.propose(
                request.model_copy(update={"transform_registry_hash": _hash("f")})
            )
        )


def test_heuristic_planner_returns_no_safe_proposal_for_protected_target() -> None:
    registry, opportunity, proposal, request = _fixture()
    protected = opportunity.model_copy(
        update={
            "editability": "PROTECTED_OR_UNSAFE",
            "source_spans": (),
            "eligible_transform_families": (),
        }
    )
    planner = HeuristicPlanner(
        opportunities=(protected,), proposals=(proposal,), registry=registry
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "NO_SAFE_PROPOSAL"
    assert result.proposal_ids == ()
