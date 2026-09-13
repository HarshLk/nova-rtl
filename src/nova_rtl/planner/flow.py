"""M6 planning over an immutable M5 search and M3 opportunity authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import field_validator, model_validator

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.flow import load_run_index
from nova_rtl.contracts.base import HashRef, StrictContract, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.optimization import (
    OptimizationOpportunity,
    OptimizationProposal,
    ProposalCorrectness,
    ProposalPrediction,
    ProposalTarget,
    Transformation,
)
from nova_rtl.contracts.planning import (
    ContextRequest,
    PlannerRequest,
    PlannerResult,
    ProviderResult,
    RoleContextPack,
)
from nova_rtl.evidence.opportunities import RankedOpportunitySet
from nova_rtl.optimization.flow import _load_m3_authority
from nova_rtl.optimization.search_flow import M5SearchBundle, verify_deterministic_search
from nova_rtl.planner.evidence import (
    EvidenceObject,
    InMemoryEvidenceProvider,
    RetrievalAuditRecord,
)
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.planner.interface import PlannerPolicy, load_planner_policy
from nova_rtl.planner.provider import (
    RecordedProviderResponse,
    RecordedStructuredModelProvider,
)
from nova_rtl.planner.search import planned_candidate_from_proposal
from nova_rtl.planner.single_agent import SingleAgentPlanner
from nova_rtl.search.controller import PlannedCandidate
from nova_rtl.transforms.registry import TransformRegistry, competition_mvp_registry


class M6PlannerFlowError(RuntimeError):
    """M6 cannot plan over the supplied immutable authorities."""


class M6PlannerRun(StrictContract):
    """Internal complete evidence for one bounded M6 planning pass."""

    schema_version: Literal[1] = 1
    run_id: str
    m5_search_bundle_hash: HashRef
    planner_policy_hash: HashRef
    provider_response_hash: HashRef | None
    planner_requests: tuple[PlannerRequest, ...]
    context_requests: tuple[ContextRequest, ...]
    context_packs: tuple[RoleContextPack, ...]
    provider_results: tuple[ProviderResult, ...]
    planner_results: tuple[PlannerResult, ...]
    proposals: tuple[OptimizationProposal, ...]
    normalized_plans: tuple[PlannedCandidate, ...]
    retrieval_audit: tuple[RetrievalAuditRecord, ...]
    fallback_count: int
    status: Literal["PASS"] = "PASS"
    bundle_hash: HashRef

    @field_validator("fallback_count")
    @classmethod
    def fallback_count_is_nonnegative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("fallback count cannot be negative")
        return value

    @model_validator(mode="after")
    def authority_is_closed_and_self_hashed(self) -> Self:
        request_ids = tuple(item.planner_request_id for item in self.planner_requests)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("planner request identities must be unique")
        if {item.planner_request_id for item in self.context_requests} != set(request_ids):
            raise ValueError("context requests do not cover planner requests")
        if {item.context_request_id for item in self.context_packs} != {
            item.context_request_id for item in self.context_requests
        }:
            raise ValueError("context packs do not cover context requests")
        result_proposals = {
            proposal_id
            for result in self.planner_results
            for proposal_id in result.proposal_ids
        }
        if result_proposals != {item.proposal_id for item in self.proposals}:
            raise ValueError("planner results do not resolve the exact proposal set")
        if {item.proposal_id for item in self.normalized_plans} != result_proposals:
            raise ValueError("normalized execution plans differ from planner proposals")
        if self.fallback_count != sum(item.fallback_used for item in self.planner_results):
            raise ValueError("fallback count differs from planner results")
        if self.bundle_hash != canonical_sha256(self, exclude=frozenset({"bundle_hash"})):
            raise ValueError("M6 planner run hash is not canonical")
        return self


def _default_parameters(operation: str) -> dict[str, Any]:
    return {
        "BALANCE_BOOLEAN_TREE": {"operator": "&", "max_operands": 16},
        "FACTOR_COMMON_PREDICATE": {"max_terms": 16, "max_fanout": 32},
        "FSM_DECODE_RESTRUCTURE": {"max_states": 16},
        "RESTRUCTURE_PRIORITY_MUX": {"max_branches": 8},
    }[operation]


def _proposal_from_plan(
    plan: PlannedCandidate,
    opportunity: OptimizationOpportunity,
    registry: TransformRegistry,
) -> OptimizationProposal:
    descriptor = registry.get_descriptor(plan.operation)
    source_span = opportunity.source_spans[0]
    evidence_ref = next(
        item for item in opportunity.evidence_refs if item.evidence_id == source_span
    )
    cone_digest = sha256(
        f"{opportunity.opportunity_id}:{source_span}".encode()
    ).hexdigest()[:24]
    return OptimizationProposal(
        proposal_id=plan.proposal_id,
        parent_candidate_id=plan.parent_candidate_id,
        opportunity_id=plan.opportunity_id,
        diagnosis_refs=(f"diagnosis_{plan.proposal_id[-24:]}",),
        target=ProposalTarget(
            hierarchy=f"nova_top.{opportunity.target_domain}",
            source_span_id=source_span,
            cone_fingerprint=f"cone:v1:{cone_digest}",
        ),
        transformation=Transformation(
            operation=plan.operation,
            family=descriptor.family,
            parameters=_default_parameters(plan.operation),
        ),
        preconditions=("NO_PROTECTED_NODE_IN_EDIT_SET",),
        correctness=ProposalCorrectness(
            contract=descriptor.correctness_contract,
            proof_scope="whole_design",
            reset_model="RESET_ASSUMPTIONS_LOCKED",
        ),
        prediction=ProposalPrediction(
            timing_direction="IMPROVE",
            area_direction="UNKNOWN",
            confidence=0.5,
        ),
        evidence_refs=(evidence_ref,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )


def _load_recorded_attempts(
    path: Path | None,
) -> tuple[HashRef | None, Mapping[str, Any]]:
    if path is None:
        return None, {}
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, ValueError) as error:
        raise M6PlannerFlowError(f"recorded provider response is invalid: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("opportunities"), dict):
        raise M6PlannerFlowError(
            "recorded provider response requires an opportunities mapping"
        )
    return "sha256:" + sha256(content).hexdigest(), payload["opportunities"]


def _responses_for(
    opportunity_id: str,
    recorded: Mapping[str, Any],
    completed_at: datetime,
) -> tuple[RecordedProviderResponse, ...]:
    attempts = recorded.get(opportunity_id)
    if attempts is None:
        return (
            RecordedProviderResponse(
                status="PROVIDER_ERROR",
                structured_output=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                error_code="PROVIDER_UNAVAILABLE",
                completed_at=completed_at,
            ),
        )
    if not isinstance(attempts, list) or not attempts:
        raise M6PlannerFlowError("recorded opportunity attempts must be a nonempty list")
    values = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise M6PlannerFlowError("recorded provider attempt must be an object")
        values.append(
            RecordedProviderResponse(
                status=attempt.get("status", "PASS"),
                structured_output=attempt.get("structured_output"),
                input_tokens=attempt.get("input_tokens", 0),
                output_tokens=attempt.get("output_tokens", 0),
                latency_ms=attempt.get("latency_ms", 0),
                error_code=attempt.get("error_code"),
                completed_at=completed_at,
            )
        )
    return tuple(values)


def _execute_planners(
    *,
    bundle: M5SearchBundle,
    ranked: RankedOpportunitySet,
    store: ArtifactStore,
    policy: PlannerPolicy,
    provider_response_hash: HashRef | None,
    recorded: Mapping[str, Any],
    completed_at: datetime,
) -> M6PlannerRun:
    registry = competition_mvp_registry()
    opportunities = {item.opportunity_id: item for item in ranked.opportunities}
    grouped_plans = {
        opportunity_id: tuple(
            item
            for item in bundle.planned_candidates
            if item.opportunity_id == opportunity_id
        )
        for opportunity_id in bundle.search_request.ordered_opportunity_ids
    }
    requests: list[PlannerRequest] = []
    context_requests: list[ContextRequest] = []
    context_packs: list[RoleContextPack] = []
    provider_results: list[ProviderResult] = []
    planner_results: list[PlannerResult] = []
    proposals: list[OptimizationProposal] = []
    normalized: list[PlannedCandidate] = []
    audit: list[RetrievalAuditRecord] = []
    for opportunity_id in bundle.search_request.ordered_opportunity_ids:
        opportunity = opportunities[opportunity_id]
        plans = grouped_plans[opportunity_id]
        heuristic_proposals = tuple(
            _proposal_from_plan(item, opportunity, registry) for item in plans
        )
        authority_ids = tuple(sorted(item.evidence_id for item in opportunity.evidence_refs))
        planner_request = PlannerRequest(
            planner_request_id=f"planner_request_{opportunity_id[-24:]}",
            run_id=bundle.run_id,
            parent_candidate_id=opportunity.parent_candidate_id,
            opportunity_id=opportunity_id,
            evidence_snapshot_hash=ranked.evidence_snapshot_hash,
            design_contract_hash=bundle.search_request.design_contract_hash,
            policy_hash=policy.policy_hash,
            transform_registry_hash=registry.registry_hash,
            authorized_evidence_ids=authority_ids,
            planner_mode="SINGLE_AGENT",
            proposal_limit=min(policy.max_proposals, max(1, len(plans))),
            token_budget=policy.max_provider_input_tokens,
            latency_budget_ms=policy.deadline_seconds * 1000,
            deadline=completed_at,
            deterministic_seed=bundle.search_request.deterministic_seed,
            required_output_schema_name="optimization-proposal",
            required_output_schema_version=2,
        )
        envelope = {
            "run_id": bundle.run_id,
            "parent_candidate_id": opportunity.parent_candidate_id,
            "opportunity_id": opportunity_id,
            "evidence_snapshot_hash": ranked.evidence_snapshot_hash,
            "design_contract_hash": bundle.search_request.design_contract_hash,
            "policy_hash": policy.policy_hash,
            "transform_registry_hash": registry.registry_hash,
            "required_analysis_view_ids": list(opportunity.affected_analysis_view_ids),
            "target_analysis_view_id": opportunity.target_analysis_view_id,
            "protected_structure_ids": list(opportunity.protected_neighbors),
            "allowed_transform_operations": list(opportunity.eligible_transform_families),
            "allowed_correctness_contracts": list(opportunity.proof_contracts),
            "advisory_only": True,
        }
        private_ids = tuple(
            dict.fromkeys(
                item.target.source_span_id for item in heuristic_proposals
            )
        )
        context_request = ContextRequest(
            context_request_id=f"context_request_{opportunity_id[-24:]}",
            planner_request_id=planner_request.planner_request_id,
            role="logic_restructuring",
            common_envelope_hash=canonical_sha256(envelope),
            private_evidence_ids=private_ids,
            retrieval_allowlist=authority_ids,
            snapshot_hash=ranked.evidence_snapshot_hash,
            token_budget=policy.max_context_tokens,
            redaction_policy_hash=canonical_sha256(
                {"excluded_keys": ["api_key", "raw_repository_path"]}
            ),
        )
        evidence = tuple(
            EvidenceObject.build(
                evidence_ref=item,
                classification=(
                    "RESTRICTED_RTL" if item.kind == "SOURCE_SPAN" else "INTERNAL"
                ),
                payload={
                    "semantic_id": item.evidence_id,
                    "kind": item.kind,
                    "target_domain": opportunity.target_domain,
                },
            )
            for item in opportunity.evidence_refs
        )
        evidence_provider = InMemoryEvidenceProvider(
            evidence,
            grants={"logic_restructuring": authority_ids},
            policy=policy,
        )
        provider = RecordedStructuredModelProvider(
            planner_request_id=planner_request.planner_request_id,
            context_request_id=context_request.context_request_id,
            provider_id="recorded_provider",
            model_id="recorded-m6-v1",
            provider_configuration={"mode": "offline", "temperature": 0},
            requested_schema_name="optimization-proposal",
            requested_schema_version=2,
            responses=_responses_for(opportunity_id, recorded, completed_at),
            artifact_store=store,
            policy=policy,
        )
        heuristic = HeuristicPlanner(
            opportunities=(opportunity,), proposals=heuristic_proposals, registry=registry
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
        result = asyncio.run(planner.propose(planner_request))
        resolved = planner.proposals_for(result)
        requests.append(planner_request)
        context_requests.append(context_request)
        assert planner.context_pack is not None
        context_packs.append(planner.context_pack)
        provider_results.extend(planner.provider_results)
        planner_results.append(result)
        proposals.extend(resolved)
        normalized.extend(
            planned_candidate_from_proposal(
                item, priority=float(ranked.priority_scores[opportunity_id])
            )
            for item in resolved
        )
        audit.extend(evidence_provider.audit_records)

    payload = {
        "schema_version": 1,
        "run_id": bundle.run_id,
        "m5_search_bundle_hash": bundle.bundle_hash,
        "planner_policy_hash": policy.policy_hash,
        "provider_response_hash": provider_response_hash,
        "planner_requests": tuple(requests),
        "context_requests": tuple(context_requests),
        "context_packs": tuple(context_packs),
        "provider_results": tuple(provider_results),
        "planner_results": tuple(planner_results),
        "proposals": tuple(proposals),
        "normalized_plans": tuple(normalized),
        "retrieval_audit": tuple(audit),
        "fallback_count": sum(item.fallback_used for item in planner_results),
        "status": "PASS",
    }
    provisional = M6PlannerRun.model_construct(**payload, bundle_hash="sha256:" + "0" * 64)
    return M6PlannerRun(
        **payload,
        bundle_hash=canonical_sha256(provisional, exclude=frozenset({"bundle_hash"})),
    )


def run_single_agent_planning(
    search_bundle_path: Path,
    *,
    repository_root: Path,
    provider_response: Path | None = None,
) -> tuple[Path, M6PlannerRun]:
    """Run recorded or outage-fallback planning over an existing M5 search."""

    bundle = verify_deterministic_search(
        search_bundle_path.resolve(), repository_root=repository_root.resolve()
    )
    run_directory = search_bundle_path.resolve().parents[3]
    index = load_run_index(run_directory)
    ranked, _, _, _ = _load_m3_authority(index, run_directory)
    policy = load_planner_policy(repository_root / "config/policy/planner.yaml")
    response_hash, recorded = _load_recorded_attempts(provider_response)
    identity = canonical_sha256(
        {
            "m5_search_bundle_hash": bundle.bundle_hash,
            "planner_policy_hash": policy.policy_hash,
            "provider_response_hash": response_hash,
            "mode": "SINGLE_AGENT",
        }
    )
    output = run_directory / "m6" / "planner-runs" / f"planner_run_{identity[-24:]}"
    path = output / "planner-run.json"
    if path.is_file():
        try:
            return path, M6PlannerRun.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise M6PlannerFlowError(f"cached M6 planner run is invalid: {error}") from error
    store = ArtifactStore(run_directory / "artifacts")
    run = _execute_planners(
        bundle=bundle,
        ranked=ranked,
        store=store,
        policy=policy,
        provider_response_hash=response_hash,
        recorded=recorded,
        completed_at=index.updated_at,
    )
    output.mkdir(parents=True, exist_ok=False)
    path.write_bytes(canonical_json_bytes(run) + b"\n")
    return path, run


__all__ = [
    "M6PlannerFlowError",
    "M6PlannerRun",
    "run_single_agent_planning",
]
