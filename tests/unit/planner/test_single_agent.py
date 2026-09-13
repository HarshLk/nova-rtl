from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import EvidenceRef, canonical_sha256
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
from nova_rtl.contracts.planning import ContextRequest, PlannerRequest
from nova_rtl.planner.evidence import EvidenceObject, InMemoryEvidenceProvider
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.planner.interface import load_planner_policy
from nova_rtl.planner.provider import (
    RecordedProviderResponse,
    RecordedStructuredModelProvider,
)
from nova_rtl.planner.single_agent import SingleAgentPlanner
from nova_rtl.transforms.registry import competition_mvp_registry


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _domain():
    registry = competition_mvp_registry()
    ref = EvidenceRef(
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
        evidence_refs=(ref,),
    )
    proposal = OptimizationProposal(
        proposal_id="proposal_single_agent",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        diagnosis_refs=("diagnosis_model",),
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
        evidence_refs=(ref,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )
    return registry, ref, opportunity, proposal


def _planner(tmp_path: Path, responses: tuple[RecordedProviderResponse, ...]):
    registry, ref, opportunity, proposal = _domain()
    policy = load_planner_policy(Path("config/policy/planner.yaml"))
    store = ArtifactStore(tmp_path / "artifacts")
    request = PlannerRequest(
        planner_request_id="planner_request_single",
        run_id="run_single",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=policy.policy_hash,
        transform_registry_hash=registry.registry_hash,
        authorized_evidence_ids=("source_span_target",),
        planner_mode="SINGLE_AGENT",
        proposal_limit=1,
        token_budget=4000,
        latency_budget_ms=60000,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        deterministic_seed=9,
        required_output_schema_name="optimization-proposal",
        required_output_schema_version=2,
    )
    envelope = {
        "run_id": request.run_id,
        "parent_candidate_id": request.parent_candidate_id,
        "opportunity_id": request.opportunity_id,
        "evidence_snapshot_hash": request.evidence_snapshot_hash,
        "design_contract_hash": request.design_contract_hash,
        "policy_hash": request.policy_hash,
        "transform_registry_hash": request.transform_registry_hash,
        "required_analysis_view_ids": ["asap7_setup", "asap7_hold"],
        "target_analysis_view_id": "asap7_setup",
        "protected_structure_ids": ["clock_divider_bank", "async_fifo"],
        "allowed_transform_operations": ["RESTRUCTURE_PRIORITY_MUX"],
        "allowed_correctness_contracts": ["STRICT_SEQ_EQUIV"],
        "advisory_only": True,
    }
    context_request = ContextRequest(
        context_request_id="context_request_single",
        planner_request_id=request.planner_request_id,
        role="logic_restructuring",
        common_envelope_hash=canonical_sha256(envelope),
        private_evidence_ids=("source_span_target",),
        retrieval_allowlist=("source_span_target",),
        snapshot_hash=_hash("a"),
        token_budget=2000,
        redaction_policy_hash=canonical_sha256(
            {"excluded_keys": ["api_key", "raw_repository_path"]}
        ),
    )
    evidence = EvidenceObject.build(
        evidence_ref=ref,
        classification="RESTRICTED_RTL",
        payload={"text": "if (sel0) y = a; else if (sel1) y = b; else y = c;"},
    )
    evidence_provider = InMemoryEvidenceProvider(
        (evidence,),
        grants={"logic_restructuring": ("source_span_target",)},
        policy=policy,
    )
    provider = RecordedStructuredModelProvider(
        planner_request_id=request.planner_request_id,
        context_request_id=context_request.context_request_id,
        provider_id="recorded_provider",
        model_id="recorded-m6-v1",
        provider_configuration={"mode": "offline", "temperature": 0},
        requested_schema_name="optimization-proposal",
        requested_schema_version=2,
        responses=responses,
        artifact_store=store,
        policy=policy,
    )
    heuristic_proposal = proposal.model_copy(
        update={"proposal_id": "proposal_heuristic_fallback"}
    )
    heuristic = HeuristicPlanner(
        opportunities=(opportunity,),
        proposals=(heuristic_proposal,),
        registry=registry,
    )
    planner = SingleAgentPlanner(
        context_request=context_request,
        common_envelope=envelope,
        evidence_provider=evidence_provider,
        provider=provider,
        heuristic_fallback=heuristic,
        opportunities=(opportunity,),
        registry=registry,
        artifact_store=store,
        policy=policy,
    )
    return planner, provider, request, proposal


def _response(output: dict | None, *, status: str = "PASS", error: str | None = None):
    return RecordedProviderResponse(
        status=status,
        structured_output=output,
        input_tokens=100,
        output_tokens=50 if output is not None else 0,
        latency_ms=20,
        error_code=error,
        completed_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_single_agent_accepts_a_registered_grounded_proposal(tmp_path: Path) -> None:
    _, _, _, proposal = _planner(tmp_path, (_response({"proposals": []}),))
    planner, provider, request, _ = _planner(
        tmp_path / "valid", (_response({"proposals": [proposal.model_dump(mode="json")]}),)
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "PASS"
    assert result.fallback_used is False
    assert result.proposal_ids == (proposal.proposal_id,)
    assert planner.proposals_for(result) == (proposal,)
    assert provider.call_count == 1


def test_single_agent_allows_exactly_one_schema_only_repair(tmp_path: Path) -> None:
    _, _, _, proposal = _planner(tmp_path, (_response({"proposals": []}),))
    planner, provider, request, _ = _planner(
        tmp_path / "repair",
        (
            _response({"wrong_field": []}),
            _response({"proposals": [proposal.model_dump(mode="json")]}),
        ),
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "PASS"
    assert result.fallback_used is False
    assert provider.call_count == 2
    assert len(planner.provider_results) == 2


def test_semantic_rejection_does_not_prompt_repair_and_falls_back(tmp_path: Path) -> None:
    _, _, _, proposal = _planner(tmp_path, (_response({"proposals": []}),))
    protected_ref = EvidenceRef(
        evidence_id="source_span_protected",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_protected",
        snapshot_hash=_hash("a"),
    )
    unsafe = proposal.model_copy(
        update={
            "target": proposal.target.model_copy(
                update={"source_span_id": "source_span_protected"}
            ),
            "evidence_refs": (protected_ref,),
        }
    )
    planner, provider, request, _ = _planner(
        tmp_path / "semantic",
        (_response({"proposals": [unsafe.model_dump(mode="json")]}),),
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "PASS"
    assert result.fallback_used is True
    assert result.proposal_ids == ("proposal_heuristic_fallback",)
    assert result.upstream_provider_result_id == planner.provider_results[-1].provider_result_id
    assert provider.call_count == 1


def test_provider_outage_is_visible_and_uses_heuristic_fallback(tmp_path: Path) -> None:
    planner, provider, request, _ = _planner(
        tmp_path,
        (
            _response(
                None,
                status="PROVIDER_ERROR",
                error="PROVIDER_UNAVAILABLE",
            ),
        ),
    )

    result = asyncio.run(planner.propose(request))

    assert result.status == "PASS"
    assert result.fallback_used is True
    assert result.provider_id == "recorded_provider"
    assert result.rejected_output_diagnostics[0].code == "PLANNER_PROVIDER_FAILURE"
    assert provider.call_count == 1
