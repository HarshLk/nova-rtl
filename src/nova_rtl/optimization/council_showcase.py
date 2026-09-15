"""Deterministic offline M8 council showcase without external credentials."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
from nova_rtl.contracts.planning import ContextRequest, CouncilResult, PlannerRequest
from nova_rtl.optimization.council_evidence import (
    publish_council_evidence,
    verify_council_evidence,
)
from nova_rtl.planner.council import (
    CouncilPlanner,
    CouncilRoleCall,
    CouncilRoleOutcome,
    load_council_policy,
)
from nova_rtl.planner.evidence import EvidenceObject, InMemoryEvidenceProvider
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.planner.interface import load_planner_policy
from nova_rtl.transforms.registry import competition_mvp_registry

_COMPLETED_AT = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _proposal(
    identity: str, operation: str, evidence: EvidenceRef
) -> OptimizationProposal:
    parameters = (
        {"max_branches": 8}
        if operation == "RESTRUCTURE_PRIORITY_MUX"
        else {"operator": "&", "max_operands": 16}
    )
    return OptimizationProposal(
        proposal_id=identity,
        parent_candidate_id="baseline",
        opportunity_id="opportunity_m8_showcase",
        diagnosis_refs=(f"diagnosis_{identity}",),
        target=ProposalTarget(
            hierarchy="nova_top.u_compute",
            source_span_id=evidence.evidence_id,
            cone_fingerprint="cone:v1:m8_showcase",
        ),
        transformation=Transformation(
            operation=operation,
            family="LOGIC_RESTRUCTURING",
            parameters=parameters,
        ),
        preconditions=("NO_PROTECTED_NODE_IN_EDIT_SET",),
        correctness=ProposalCorrectness(
            contract="STRICT_SEQ_EQUIV",
            proof_scope="whole_design",
            reset_model="RESET_ASSUMPTIONS_LOCKED",
        ),
        prediction=ProposalPrediction(
            timing_direction="IMPROVE",
            area_direction="UNKNOWN",
            confidence=0.5,
        ),
        evidence_refs=(evidence,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )


class _ShowcaseInvoker:
    def __init__(
        self, proposals: tuple[OptimizationProposal, ...], evidence: EvidenceRef
    ) -> None:
        self._proposals = proposals
        self._evidence = evidence

    async def invoke(
        self, call: CouncilRoleCall, *, deadline_s: float
    ) -> CouncilRoleOutcome:
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
                            "objection_id": "objection_m8_formal_scope",
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
                        "critique_report_id": f"critique_m8_{call.role_id}_{index}",
                        "critic_role": call.role_id,
                        "critic_class": critic_class,
                        "proposal_card_id": card["proposal_card_id"],
                        "objections": objections,
                    }
                )
            output = {"critiques": critiques}
        else:
            selected = call.input_payload["cards"][0]["proposal_id"]
            output = {
                "dispositions": [
                    {
                        "schema_version": 1,
                        "critique_disposition_id": "disposition_m8_formal_scope",
                        "objection_id": "objection_m8_formal_scope",
                        "action": "ACCEPT",
                        "reason_code": "STRICT_EQUIV_RETAINED",
                        "resulting_proposal_id": selected,
                        "chair_evidence_refs": [self._evidence.model_dump(mode="json")],
                    }
                ],
                "revision_records": [],
                "final_proposal_ids": [selected],
            }
        return CouncilRoleOutcome(
            role_id=call.role_id,
            status="PASS",
            structured_output=output,
            input_tokens=20,
            output_tokens=10,
            latency_ms=2,
        )


def run_council_showcase(output_directory: Path) -> tuple[Path, CouncilResult]:
    """Run and persist the deterministic minimum five-role M8 demonstration."""

    output_directory = output_directory.resolve()
    existing = output_directory / "council-result.json"
    if existing.is_file():
        return existing, verify_council_evidence(output_directory)
    registry = competition_mvp_registry()
    council_policy = load_council_policy(Path("config/policy/council.yaml"))
    planner_policy = load_planner_policy(Path("config/policy/planner.yaml"))
    source_ref = EvidenceRef(
        evidence_id="source_span_m8_showcase",
        kind="SOURCE_SPAN",
        artifact_id="artifact_m8_evidence",
        json_pointer="/nodes/source_span_m8_showcase",
        snapshot_hash=_hash("a"),
    )
    timing_ref = EvidenceRef(
        evidence_id="path_m8_showcase",
        kind="PATH",
        artifact_id="artifact_m8_evidence",
        json_pointer="/paths/path_m8_showcase",
        snapshot_hash=_hash("a"),
    )
    opportunity = OptimizationOpportunity(
        opportunity_id="opportunity_m8_showcase",
        parent_candidate_id="baseline",
        target_domain="compute_domain",
        target_analysis_view_id="asap7_setup",
        affected_analysis_view_ids=("asap7_setup", "asap7_hold"),
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
        eligible_transform_families=(
            "BALANCE_BOOLEAN_TREE",
            "RESTRUCTURE_PRIORITY_MUX",
        ),
        proof_contracts=("STRICT_SEQ_EQUIV",),
        evidence_refs=(source_ref, timing_ref),
    )
    proposals = (
        _proposal("proposal_m8_timing", "RESTRUCTURE_PRIORITY_MUX", source_ref),
        _proposal("proposal_m8_logic", "BALANCE_BOOLEAN_TREE", source_ref),
    )
    request = PlannerRequest(
        planner_request_id="planner_request_m8_showcase",
        run_id="run_m8_showcase",
        parent_candidate_id="baseline",
        opportunity_id=opportunity.opportunity_id,
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=council_policy.policy_hash,
        transform_registry_hash=registry.registry_hash,
        authorized_evidence_ids=(source_ref.evidence_id, timing_ref.evidence_id),
        planner_mode="AGENT_COUNCIL",
        proposal_limit=2,
        token_budget=council_policy.max_aggregate_tokens,
        latency_budget_ms=council_policy.deadline_seconds * 1000,
        deadline=_COMPLETED_AT,
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
            context_request_id=f"context_request_m8_{role}",
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
        invoker=_ShowcaseInvoker(proposals, timing_ref),
        heuristic_fallback=heuristic,
        opportunities=(opportunity,),
        registry=registry,
        artifact_store=ArtifactStore(output_directory / "artifacts"),
        policy=council_policy,
    )
    planner_result = asyncio.run(planner.propose(request))
    result_path = publish_council_evidence(
        output_directory, planner=planner, planner_result=planner_result
    )
    return result_path, planner.council_result


__all__ = ["run_council_showcase"]
