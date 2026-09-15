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
from nova_rtl.contracts.reporting import SearchRequest
from nova_rtl.optimization.council_evidence import (
    publish_council_evidence,
    verify_council_evidence,
)
from nova_rtl.optimization.council_showcase import run_council_showcase
from nova_rtl.planner.council import (
    CouncilPlanner,
    CouncilRoleCall,
    CouncilRoleOutcome,
    load_council_policy,
)
from nova_rtl.planner.evidence import EvidenceObject, InMemoryEvidenceProvider
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.planner.interface import load_planner_policy
from nova_rtl.planner.search import CanonicalPlannerAdapter
from nova_rtl.transforms.registry import competition_mvp_registry


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _proposal(identity: str, operation: str, evidence: EvidenceRef) -> OptimizationProposal:
    family = {
        "BALANCE_BOOLEAN_TREE": "LOGIC_RESTRUCTURING",
        "RESTRUCTURE_PRIORITY_MUX": "LOGIC_RESTRUCTURING",
    }[operation]
    return OptimizationProposal(
        proposal_id=identity,
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        diagnosis_refs=(f"diagnosis_{identity}",),
        target=ProposalTarget(
            hierarchy="nova_top.u_compute",
            source_span_id=evidence.evidence_id,
            cone_fingerprint="cone:v1:target",
        ),
        transformation=Transformation(
            operation=operation,
            family=family,
            parameters=(
                {"max_branches": 8}
                if operation == "RESTRUCTURE_PRIORITY_MUX"
                else {"operator": "&", "max_operands": 16}
            ),
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


class _CouncilInvoker:
    def __init__(
        self,
        proposals: tuple[OptimizationProposal, ...],
        evidence: EvidenceRef,
        *,
        fail_role: str | None = None,
    ):
        self._proposals = proposals
        self._evidence = evidence
        self._fail_role = fail_role

    async def invoke(
        self, call: CouncilRoleCall, *, deadline_s: float
    ) -> CouncilRoleOutcome:
        if call.role_id == self._fail_role:
            return CouncilRoleOutcome(
                role_id=call.role_id,
                status="ERROR",
                structured_output=None,
                input_tokens=1,
                output_tokens=0,
                latency_ms=1,
            )
        if call.role_kind == "PROPOSER":
            index = 0 if call.role_id == "timing_forensics" else 1
            output = {"proposals": [self._proposals[index].model_dump(mode="json")]}
        elif call.role_kind == "CRITIC":
            critic_class = "FORMAL" if call.role_id == "formal_critic" else "PPA"
            critiques = []
            for index, card in enumerate(call.input_payload["cards"]):
                objections = []
                if critic_class == "FORMAL" and index == 0:
                    objections.append(
                        {
                            "objection_id": "objection_formal_scope",
                            "severity": "ADVISORY",
                            "category": "FORMAL_SCOPE_RISK",
                            "message": "retain whole-design strict equivalence",
                            "evidence_refs": [self._evidence.model_dump(mode="json")],
                            "requested_disposition": "ACCEPT",
                        }
                    )
                critiques.append(
                    {
                        "schema_version": 1,
                        "critique_report_id": f"critique_{call.role_id}_{index}",
                        "critic_role": call.role_id,
                        "critic_class": critic_class,
                        "proposal_card_id": card["proposal_card_id"],
                        "objections": objections,
                    }
                )
            output = {"critiques": critiques}
        else:
            first = call.input_payload["cards"][0]["proposal_id"]
            output = {
                "dispositions": [
                    {
                        "schema_version": 1,
                        "critique_disposition_id": "disposition_formal_scope",
                        "objection_id": "objection_formal_scope",
                        "action": "ACCEPT",
                        "reason_code": "STRICT_EQUIV_RETAINED",
                        "resulting_proposal_id": first,
                        "chair_evidence_refs": [self._evidence.model_dump(mode="json")],
                    }
                ],
                "revision_records": [],
                "final_proposal_ids": [first],
            }
        return CouncilRoleOutcome(
            role_id=call.role_id,
            status="PASS",
            structured_output=output,
            input_tokens=20,
            output_tokens=10,
            latency_ms=2,
        )


def test_bounded_council_flows_through_canonical_search_boundary(tmp_path: Path) -> None:
    registry = competition_mvp_registry()
    council_policy = load_council_policy(Path("config/policy/council.yaml"))
    planner_policy = load_planner_policy(Path("config/policy/planner.yaml"))
    source_ref = EvidenceRef(
        evidence_id="source_span_target",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_target",
        snapshot_hash=_hash("a"),
    )
    timing_ref = EvidenceRef(
        evidence_id="path_target",
        kind="PATH",
        artifact_id="artifact_graph",
        json_pointer="/paths/path_target",
        snapshot_hash=_hash("a"),
    )
    opportunity = OptimizationOpportunity(
        opportunity_id="opportunity_target",
        parent_candidate_id="baseline",
        target_domain="compute_domain",
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
        source_spans=(source_ref.evidence_id,),
        protected_neighbors=(),
        eligible_transform_families=("BALANCE_BOOLEAN_TREE", "RESTRUCTURE_PRIORITY_MUX"),
        proof_contracts=("STRICT_SEQ_EQUIV",),
        evidence_refs=(source_ref, timing_ref),
    )
    proposals = (
        _proposal("proposal_timing", "RESTRUCTURE_PRIORITY_MUX", source_ref),
        _proposal("proposal_logic", "BALANCE_BOOLEAN_TREE", source_ref),
    )
    request = PlannerRequest(
        planner_request_id="planner_request_council",
        run_id="run_council",
        parent_candidate_id="baseline",
        opportunity_id=opportunity.opportunity_id,
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=council_policy.policy_hash,
        transform_registry_hash=registry.registry_hash,
        authorized_evidence_ids=(source_ref.evidence_id, timing_ref.evidence_id),
        planner_mode="AGENT_COUNCIL",
        proposal_limit=2,
        token_budget=30_000,
        latency_budget_ms=120_000,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        deterministic_seed=8,
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
        "allowed_transform_operations": [
            "BALANCE_BOOLEAN_TREE",
            "RESTRUCTURE_PRIORITY_MUX",
        ],
        "allowed_correctness_contracts": ["STRICT_SEQ_EQUIV"],
        "advisory_only": True,
    }
    contexts = tuple(
        ContextRequest(
            context_request_id=f"context_request_{role}",
            planner_request_id=request.planner_request_id,
            role=role,
            common_envelope_hash=canonical_sha256(envelope),
            private_evidence_ids=(evidence_id,),
            retrieval_allowlist=(evidence_id,),
            snapshot_hash=request.evidence_snapshot_hash,
            token_budget=5_000,
            redaction_policy_hash=_hash("d"),
        )
        for role, evidence_id in (
            ("timing_forensics", timing_ref.evidence_id),
            ("logic_domain_specialist", source_ref.evidence_id),
        )
    )
    objects = tuple(
        EvidenceObject.build(
            evidence_ref=ref,
            classification="RESTRICTED_RTL",
            payload={"summary": ref.evidence_id},
        )
        for ref in (source_ref, timing_ref)
    )
    evidence = InMemoryEvidenceProvider(
        objects,
        grants={
            "timing_forensics": (timing_ref.evidence_id,),
            "logic_domain_specialist": (source_ref.evidence_id,),
        },
        policy=planner_policy,
    )
    heuristic = HeuristicPlanner(
        opportunities=(opportunity,), proposals=proposals, registry=registry
    )
    planner = CouncilPlanner(
        context_requests=contexts,
        common_envelope=envelope,
        evidence_provider=evidence,
        invoker=_CouncilInvoker(proposals, timing_ref),
        heuristic_fallback=heuristic,
        opportunities=(opportunity,),
        registry=registry,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        policy=council_policy,
    )
    search_request = SearchRequest(
        search_request_id="search_request_council",
        run_id=request.run_id,
        design_contract_hash=request.design_contract_hash,
        policy_hash=request.policy_hash,
        transform_registry_hash=request.transform_registry_hash,
        planner_mode="AGENT_COUNCIL",
        ordered_opportunity_ids=(opportunity.opportunity_id,),
        required_correctness_class="PRIMARY_STRICT",
        candidate_budget=2,
        formal_budget=2,
        physical_budget=1,
        token_budget=30_000,
        latency_budget_ms=120_000,
        deterministic_seed=8,
        stop_policy_hash=_hash("e"),
        created_at=datetime.now(UTC),
    )
    adapter = CanonicalPlannerAdapter(
        planners={opportunity.opportunity_id: planner},
        planner_requests={opportunity.opportunity_id: request},
        priorities={opportunity.opportunity_id: 1.0},
    )

    batch = asyncio.run(adapter.propose(search_request, opportunity.opportunity_id))

    assert len(batch.proposals) == 1
    assert batch.council_result_ids == (planner.council_result.council_result_id,)
    assert planner.council_result.status == "PASS"
    assert len(planner.council_result.selected_role_ids) == 5
    assert len(planner.council_result.critique_reports) == 4
    assert planner.council_result.critique_dispositions[0].objection_id == (
        "objection_formal_scope"
    )
    assert planner.council_trace.trace_completeness_percent == 100.0

    evidence_directory = tmp_path / "deliberation"
    result_path = publish_council_evidence(
        evidence_directory, planner=planner, planner_result=adapter.planner_results[0]
    )
    first_bytes = result_path.read_bytes()
    repeated_path = publish_council_evidence(
        evidence_directory, planner=planner, planner_result=adapter.planner_results[0]
    )
    assert repeated_path.read_bytes() == first_bytes
    assert verify_council_evidence(evidence_directory) == planner.council_result

    failing_planner = CouncilPlanner(
        context_requests=contexts,
        common_envelope=envelope,
        evidence_provider=evidence,
        invoker=_CouncilInvoker(proposals, timing_ref, fail_role="formal_critic"),
        heuristic_fallback=heuristic,
        opportunities=(opportunity,),
        registry=registry,
        artifact_store=ArtifactStore(tmp_path / "fallback-artifacts"),
        policy=council_policy,
    )
    fallback = asyncio.run(failing_planner.propose(request))

    assert fallback.status == "PARTIAL"
    assert fallback.fallback_used is True
    assert fallback.upstream_provider_result_id == failing_planner.council_result.council_result_id
    assert fallback.proposal_ids
    assert failing_planner.council_result.status == "PARTIAL"
    assert failing_planner.council_result.final_ordered_proposal_ids == ()


def test_recorded_council_showcase_is_deterministic_and_replayable(
    tmp_path: Path,
) -> None:
    first_path, first = run_council_showcase(tmp_path / "showcase-first")
    first_bytes = first_path.read_bytes()
    second_path, second = run_council_showcase(tmp_path / "showcase-second")

    assert first == second
    assert first_bytes == second_path.read_bytes()
    assert first.status == "PASS"
    assert len(first.selected_role_ids) == 5
    assert len(first.critique_dispositions) >= 1
    assert verify_council_evidence(first_path.parent) == first
