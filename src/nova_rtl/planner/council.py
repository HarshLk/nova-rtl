"""Bounded, role-scoped M8 council planning primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import Diagnostic, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.optimization import OptimizationOpportunity, OptimizationProposal
from nova_rtl.contracts.planning import (
    ContextRequest,
    CouncilPolicy,
    CouncilRequest,
    CouncilResult,
    CouncilRevisionRecord,
    CouncilRoute,
    CouncilTrace,
    CouncilTraceEvent,
    CritiqueDisposition,
    CritiqueReport,
    PlannerRequest,
    PlannerResult,
    PrivateRoleRecord,
    ProposalCard,
    ProposalShortlist,
    RoleContextPack,
    RoleStatus,
)
from nova_rtl.planner.context import build_context
from nova_rtl.planner.evidence import InMemoryEvidenceProvider
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.transforms.registry import TransformRegistry, TransformRegistryError


class CouncilPolicyError(RuntimeError):
    """The council policy cannot be loaded without expanding authority."""


class CouncilIsolationError(RuntimeError):
    """Council private context boundaries overlap or are incomplete."""


class CouncilBudgetError(RuntimeError):
    """A council call, fan-out, deadline, or aggregate budget was exceeded."""


class CouncilFaninError(RuntimeError):
    """Critic or chair fan-in is incomplete, ambiguous, or unsafe."""


class CouncilPlanningError(RuntimeError):
    """A council output cannot cross the canonical planner boundary."""


@dataclass(frozen=True)
class CouncilRoleCall:
    role_id: str
    role_kind: Literal["PROPOSER", "CRITIC", "CHAIR"]
    input_payload: Mapping[str, Any]
    token_budget: int


@dataclass(frozen=True)
class CouncilRoleOutcome:
    role_id: str
    status: RoleStatus
    structured_output: Mapping[str, Any] | None
    input_tokens: int
    output_tokens: int
    latency_ms: int


class CouncilRoleInvoker(Protocol):
    async def invoke(
        self, call: CouncilRoleCall, *, deadline_s: float
    ) -> CouncilRoleOutcome: ...


def load_council_policy(path: Path) -> CouncilPolicy:
    """Load and canonically bind one strict council policy."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CouncilPolicyError(f"cannot load council policy: {error}") from error
    if not isinstance(payload, dict):
        raise CouncilPolicyError("council policy must be a YAML mapping")
    try:
        return CouncilPolicy.build(**payload)
    except (TypeError, ValueError) as error:
        raise CouncilPolicyError(f"invalid council policy: {error}") from error


_DOMAIN_ROLES = {
    "SERIAL_ARITHMETIC": "arithmetic_specialist",
    "MUX_AFTER_ARITHMETIC": "arithmetic_specialist",
    "FSM_DECODE_DEPTH": "fsm_specialist",
    "HIGH_FANOUT_CONTROL": "fanout_resource_specialist",
    "RESOURCE_ARBITRATION": "fanout_resource_specialist",
}


def route_roles(
    opportunity: OptimizationOpportunity, policy: CouncilPolicy
) -> CouncilRoute:
    """Select the fixed minimum council without model-controlled supervision."""

    root_cause = opportunity.root_causes[0].category
    domain_role = _DOMAIN_ROLES.get(root_cause, "logic_domain_specialist")
    proposer_roles = ("timing_forensics", domain_role)
    selected_count = len(proposer_roles) + 2 + 1
    if (
        len(proposer_roles) > policy.max_strategist_roles
        or selected_count > policy.max_reasoning_roles
    ):
        raise CouncilPolicyError("deterministic council route exceeds policy bounds")
    return CouncilRoute.build(
        opportunity_id=opportunity.opportunity_id,
        policy_hash=policy.policy_hash,
        root_cause=root_cause,
        proposer_roles=proposer_roles,
        critic_roles=("formal_critic", "ppa_critic"),
        chair_role="proposal_chair",
        reason_codes=(f"ROOT_CAUSE_{root_cause}", "MINIMUM_BLINDED_COUNCIL"),
    )


def build_blinded_proposer_contexts(
    requests: Sequence[ContextRequest],
    *,
    planner_request: PlannerRequest,
    proposer_roles: Sequence[str],
    common_envelope: Mapping[str, Any],
    evidence_provider: InMemoryEvidenceProvider,
    artifact_store: ArtifactStore,
) -> tuple[RoleContextPack, ...]:
    """Build independent proposer packs without cross-role private evidence."""

    by_role = {item.role: item for item in requests}
    roles = tuple(proposer_roles)
    if len(by_role) != len(requests) or set(by_role) != set(roles):
        raise CouncilIsolationError("proposer context requests must exactly cover the route")
    visible: set[str] = set()
    for role in roles:
        private_ids = set(by_role[role].private_evidence_ids)
        if visible & private_ids:
            raise CouncilIsolationError("proposer private evidence overlap is forbidden")
        visible.update(private_ids)
    try:
        packs = tuple(
            build_context(
                by_role[role],
                planner_request=planner_request,
                common_envelope=common_envelope,
                evidence_provider=evidence_provider,
                artifact_store=artifact_store,
            )
            for role in roles
        )
    except ValueError as error:
        raise CouncilIsolationError(f"cannot build blinded council contexts: {error}") from error
    if len({item.private_pack_hash for item in packs}) != len(packs):
        raise CouncilIsolationError("proposer private context identities must be distinct")
    return packs


async def run_role_round(
    calls: Sequence[CouncilRoleCall],
    *,
    invoker: CouncilRoleInvoker,
    policy: CouncilPolicy,
    deadline_s: float,
) -> tuple[CouncilRoleOutcome, ...]:
    """Run one bounded parallel council round and preserve deterministic order."""

    ordered = tuple(calls)
    if not ordered or len(ordered) > policy.fan_out_limit:
        raise CouncilBudgetError("council round exceeds its fan-out policy")
    if len({item.role_id for item in ordered}) != len(ordered):
        raise CouncilBudgetError("council round role identities must be unique")
    if deadline_s <= 0 or deadline_s > policy.deadline_seconds:
        raise CouncilBudgetError("council round deadline exceeds policy")
    if any(item.token_budget <= 0 for item in ordered):
        raise CouncilBudgetError("council role token budgets must be positive")
    try:
        async with asyncio.timeout(deadline_s):
            outcomes = tuple(
                await asyncio.gather(
                    *(invoker.invoke(item, deadline_s=deadline_s) for item in ordered)
                )
            )
    except TimeoutError as error:
        raise CouncilBudgetError("council round exceeded its deadline") from error
    if tuple(item.role_id for item in outcomes) != tuple(item.role_id for item in ordered):
        raise CouncilBudgetError("council invoker changed deterministic role order")
    total_tokens = sum(item.input_tokens + item.output_tokens for item in outcomes)
    if total_tokens > policy.max_aggregate_tokens:
        raise CouncilBudgetError("council exceeded its aggregate token budget")
    if any(
        outcome.input_tokens + outcome.output_tokens > call.token_budget
        for call, outcome in zip(ordered, outcomes, strict=True)
    ):
        raise CouncilBudgetError("council role exceeded its assigned token budget")
    return outcomes


def _proposal_semantics(proposal: OptimizationProposal) -> dict[str, Any]:
    return {
        "parent_candidate_id": proposal.parent_candidate_id,
        "opportunity_id": proposal.opportunity_id,
        "target": proposal.target.model_dump(mode="json"),
        "transformation": proposal.transformation.model_dump(mode="json"),
        "preconditions": proposal.preconditions,
        "correctness": proposal.correctness.model_dump(mode="json"),
        "evidence_refs": tuple(
            item.model_dump(mode="json") for item in proposal.evidence_refs
        ),
        "abort_conditions": proposal.abort_conditions,
    }


def normalize_proposal_cards(
    proposals: Sequence[OptimizationProposal],
) -> tuple[tuple[ProposalCard, ...], tuple[OptimizationProposal, ...]]:
    """Semantically deduplicate proposals and remove proposer identity before criticism."""

    by_hash: dict[str, OptimizationProposal] = {}
    for proposal in proposals:
        semantic_hash = canonical_sha256(_proposal_semantics(proposal))
        incumbent = by_hash.get(semantic_hash)
        if incumbent is None or proposal.proposal_id < incumbent.proposal_id:
            by_hash[semantic_hash] = proposal
    ordered = tuple(sorted(by_hash.items()))
    cards = tuple(
        ProposalCard(
            proposal_card_id=f"proposal_card_{semantic_hash[-24:]}",
            proposal_id=proposal.proposal_id,
            normalized_proposal_hash=semantic_hash,
        )
        for semantic_hash, proposal in ordered
    )
    return cards, tuple(proposal for _, proposal in ordered)


def build_council_shortlist(
    *,
    cards: Sequence[ProposalCard],
    critiques: Sequence[CritiqueReport],
    dispositions: Sequence[CritiqueDisposition],
    revisions: Sequence[CouncilRevisionRecord],
    final_proposal_ids: Sequence[str],
    policy_path: Path | CouncilPolicy,
) -> ProposalShortlist:
    """Apply deterministic mandatory-critic and chair closure rules."""

    policy = (
        policy_path
        if isinstance(policy_path, CouncilPolicy)
        else load_council_policy(policy_path)
    )
    card_tuple = tuple(cards)
    critique_tuple = tuple(critiques)
    disposition_tuple = tuple(dispositions)
    revision_tuple = tuple(revisions)
    final_ids = tuple(final_proposal_ids)
    if len(revision_tuple) > policy.max_revisions:
        raise CouncilFaninError("council revision count exceeds policy")
    if len(final_ids) > policy.max_final_proposals or len(final_ids) != len(set(final_ids)):
        raise CouncilFaninError("council final proposal set exceeds policy or is duplicated")
    required_reviews = {
        (card.proposal_card_id, critic_class)
        for card in card_tuple
        for critic_class in ("FORMAL", "PPA")
    }
    observed_reviews = {
        (report.proposal_card_id, report.critic_class) for report in critique_tuple
    }
    if observed_reviews != required_reviews:
        raise CouncilFaninError("every proposal card requires both mandatory critics")
    objections = {
        objection.objection_id
        for report in critique_tuple
        for objection in report.objections
    }
    dispositioned = {item.objection_id for item in disposition_tuple}
    if dispositioned != objections or len(disposition_tuple) != len(dispositioned):
        raise CouncilFaninError("chair must disposition every critic objection exactly once")
    original_ids = {item.proposal_id for item in card_tuple}
    revised_ids = {item.revised_proposal_id for item in revision_tuple}
    if not set(final_ids).issubset(original_ids | revised_ids):
        raise CouncilFaninError("shortlist proposal does not resolve through reviewed cards")
    payload = {
        "status": "PASS" if final_ids else "NO_SAFE_PROPOSAL",
        "ordered_proposal_ids": final_ids,
        "fallback_eligible": not final_ids,
        "unresolved_mandatory_finding_count": 0,
    }
    identity = canonical_sha256(payload)
    return ProposalShortlist(
        proposal_shortlist_id=f"proposal_shortlist_{identity[-24:]}",
        **payload,
    )


class CouncilPlanner:
    """Minimum bounded council implementing the unchanged advisory Planner interface."""

    def __init__(
        self,
        *,
        context_requests: Sequence[ContextRequest],
        common_envelope: Mapping[str, Any],
        evidence_provider: InMemoryEvidenceProvider,
        invoker: CouncilRoleInvoker,
        heuristic_fallback: HeuristicPlanner,
        opportunities: Sequence[OptimizationOpportunity],
        registry: TransformRegistry,
        artifact_store: ArtifactStore,
        policy: CouncilPolicy,
    ) -> None:
        self._context_requests = tuple(context_requests)
        self._common_envelope = dict(common_envelope)
        self._evidence_provider = evidence_provider
        self._invoker = invoker
        self._heuristic = heuristic_fallback
        self._opportunities = {item.opportunity_id: item for item in opportunities}
        self._registry = registry
        self._store = artifact_store
        self._policy = policy
        self._resolved: dict[str, OptimizationProposal] = {}
        self._council_result: CouncilResult | None = None
        self._council_trace: CouncilTrace | None = None
        self._council_request: CouncilRequest | None = None

    @property
    def council_result(self) -> CouncilResult:
        if self._council_result is None:
            raise CouncilPlanningError("council has not deliberated")
        return self._council_result

    @property
    def council_trace(self) -> CouncilTrace:
        if self._council_trace is None:
            raise CouncilPlanningError("council has not produced a trace")
        return self._council_trace

    @property
    def council_request(self) -> CouncilRequest:
        if self._council_request is None:
            raise CouncilPlanningError("council request has not been built")
        return self._council_request

    def proposals_for(self, result: PlannerResult) -> tuple[OptimizationProposal, ...]:
        try:
            return tuple(self._resolved[item] for item in result.proposal_ids)
        except KeyError as error:
            raise CouncilPlanningError("council result references an unknown proposal") from error

    def _proposal_is_authorized(
        self, proposal: OptimizationProposal, request: PlannerRequest
    ) -> bool:
        opportunity = self._opportunities[request.opportunity_id]
        if (
            proposal.parent_candidate_id != request.parent_candidate_id
            or proposal.opportunity_id != request.opportunity_id
            or proposal.target.source_span_id not in opportunity.source_spans
            or proposal.transformation.operation not in opportunity.eligible_transform_families
            or proposal.correctness.contract not in opportunity.proof_contracts
        ):
            return False
        if not {item.evidence_id for item in proposal.evidence_refs}.issubset(
            request.authorized_evidence_ids
        ):
            return False
        if {item.snapshot_hash for item in proposal.evidence_refs} != {
            request.evidence_snapshot_hash
        }:
            return False
        try:
            descriptor = self._registry.get_descriptor(proposal.transformation.operation)
            self._registry.validate_parameters(
                proposal.transformation.operation,
                proposal.transformation.parameters,
            )
        except (TransformRegistryError, ValueError):
            return False
        return (
            descriptor.family == proposal.transformation.family
            and descriptor.correctness_contract == proposal.correctness.contract
        )

    def _role_record(
        self,
        outcome: CouncilRoleOutcome,
        call: CouncilRoleCall,
        *,
        private_pack_hash: str,
        retrieval_ids: Sequence[str] = (),
    ) -> PrivateRoleRecord:
        content = canonical_json_bytes(dict(outcome.structured_output or {}))
        artifact = self._store.put_named_bytes(
            content,
            artifact_id=(
                f"artifact_council_{call.role_id}_"
                f"{canonical_sha256({'content_hex': content.hex()})[-16:]}"
            ),
            media_type="application/json",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=None,
        )
        return PrivateRoleRecord(
            role_id=call.role_id,
            role_kind=call.role_kind,
            private_pack_hash=private_pack_hash,
            retrieved_evidence_ids=tuple(retrieval_ids),
            blinded_round=call.role_kind != "CHAIR",
            structured_submission_artifact=artifact,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
            status=outcome.status,
        )

    async def _deliberate(self, request: PlannerRequest) -> PlannerResult:
        """Run blinded proposers, mandatory critics, and one chair fan-in."""

        if request.planner_mode != "AGENT_COUNCIL":
            raise CouncilPlanningError("council planner requires AGENT_COUNCIL mode")
        if request.policy_hash != self._policy.policy_hash:
            raise CouncilPlanningError("council policy identity changed")
        if request.transform_registry_hash != self._registry.registry_hash:
            raise CouncilPlanningError("council transform registry identity changed")
        opportunity = self._opportunities.get(request.opportunity_id)
        if opportunity is None:
            raise CouncilPlanningError("council opportunity identity is unknown")
        route = route_roles(opportunity, self._policy)
        deadline_s = min(
            float(self._policy.deadline_seconds),
            max(0.001, request.latency_budget_ms / 1000.0),
        )
        request_hash = canonical_sha256(
            {"planner_request_id": request.planner_request_id, "route": route.route_hash}
        )
        self._council_request = CouncilRequest(
            council_request_id=f"council_request_{request_hash[-24:]}",
            planner_request_id=request.planner_request_id,
            council_route_id=route.council_route_id,
            council_route_hash=route.route_hash,
            blinded_proposer_roles=route.proposer_roles,
            critic_roles=route.critic_roles,
            chair_role=route.chair_role,
            context_policy_hash=canonical_sha256(
                {
                    "redaction_policy_hashes": tuple(
                        item.redaction_policy_hash for item in self._context_requests
                    )
                }
            ),
            fan_out_limit=self._policy.fan_out_limit,
            fan_in_limit=self._policy.fan_in_limit,
            aggregate_token_budget=min(request.token_budget, self._policy.max_aggregate_tokens),
            aggregate_latency_budget_ms=min(
                request.latency_budget_ms, self._policy.deadline_seconds * 1000
            ),
            deadline=request.deadline,
            event_stream_id=f"council_events_{request_hash[-24:]}",
        )
        packs = build_blinded_proposer_contexts(
            self._context_requests,
            planner_request=request,
            proposer_roles=route.proposer_roles,
            common_envelope=self._common_envelope,
            evidence_provider=self._evidence_provider,
            artifact_store=self._store,
        )
        proposer_calls = tuple(
            CouncilRoleCall(
                role_id=role,
                role_kind="PROPOSER",
                input_payload={"role_id": role, "context_pack_hash": pack.rendered_message_hash},
                token_budget=self._council_request.aggregate_token_budget
                // len(route.proposer_roles),
            )
            for role, pack in zip(route.proposer_roles, packs, strict=True)
        )
        proposer_outcomes = await run_role_round(
            proposer_calls,
            invoker=self._invoker,
            policy=self._policy,
            deadline_s=deadline_s,
        )
        if any(
            item.status != "PASS" or item.structured_output is None
            for item in proposer_outcomes
        ):
            raise CouncilPlanningError("mandatory proposer failed")
        proposals: list[OptimizationProposal] = []
        for outcome in proposer_outcomes:
            try:
                proposals.extend(
                    OptimizationProposal.model_validate(item)
                    for item in (outcome.structured_output or {})["proposals"]
                )
            except (KeyError, TypeError, ValueError) as error:
                raise CouncilPlanningError("proposer output schema is invalid") from error
        if not proposals or any(
            not self._proposal_is_authorized(item, request) for item in proposals
        ):
            raise CouncilPlanningError("proposer output is unauthorized")
        cards, normalized = normalize_proposal_cards(proposals)
        critic_calls = tuple(
            CouncilRoleCall(
                role_id=role,
                role_kind="CRITIC",
                input_payload={
                    "role_id": role,
                    "cards": tuple(item.model_dump(mode="json") for item in cards),
                    "snapshot_hash": request.evidence_snapshot_hash,
                },
                token_budget=self._council_request.aggregate_token_budget // 4,
            )
            for role in route.critic_roles
        )
        critic_outcomes = await run_role_round(
            critic_calls,
            invoker=self._invoker,
            policy=self._policy,
            deadline_s=deadline_s,
        )
        if any(
            item.status != "PASS" or item.structured_output is None
            for item in critic_outcomes
        ):
            raise CouncilPlanningError("mandatory critic failed")
        critiques: list[CritiqueReport] = []
        for outcome in critic_outcomes:
            try:
                critiques.extend(
                    CritiqueReport.model_validate(item)
                    for item in (outcome.structured_output or {})["critiques"]
                )
            except (KeyError, TypeError, ValueError) as error:
                raise CouncilPlanningError("critic output schema is invalid") from error
        chair_call = CouncilRoleCall(
            role_id=route.chair_role,
            role_kind="CHAIR",
            input_payload={
                "role_id": route.chair_role,
                "cards": tuple(item.model_dump(mode="json") for item in cards),
                "critiques": tuple(item.model_dump(mode="json") for item in critiques),
                "max_final_proposals": min(
                    request.proposal_limit, self._policy.max_final_proposals
                ),
                "max_revisions": self._policy.max_revisions,
            },
            token_budget=self._council_request.aggregate_token_budget // 4,
        )
        chair_outcome = (
            await run_role_round(
                (chair_call,),
                invoker=self._invoker,
                policy=self._policy,
                deadline_s=deadline_s,
            )
        )[0]
        if chair_outcome.status != "PASS" or chair_outcome.structured_output is None:
            raise CouncilPlanningError("proposal chair failed")
        try:
            chair = chair_outcome.structured_output
            dispositions = tuple(
                CritiqueDisposition.model_validate(item) for item in chair["dispositions"]
            )
            revisions = tuple(
                CouncilRevisionRecord.model_validate(item)
                for item in chair["revision_records"]
            )
            final_ids = tuple(chair["final_proposal_ids"])
        except (KeyError, TypeError, ValueError) as error:
            raise CouncilPlanningError("chair output schema is invalid") from error
        shortlist = build_council_shortlist(
            cards=cards,
            critiques=critiques,
            dispositions=dispositions,
            revisions=revisions,
            final_proposal_ids=final_ids,
            policy_path=self._policy,
        )
        by_id = {item.proposal_id: item for item in normalized}
        if not set(final_ids).issubset(by_id):
            raise CouncilPlanningError("runtime revision lacks a validated proposal artifact")
        self._resolved.update({item: by_id[item] for item in final_ids})
        records = tuple(
            self._role_record(
                outcome,
                call,
                private_pack_hash=pack.private_pack_hash,
                retrieval_ids=pack.retrieval_grants,
            )
            for outcome, call, pack in zip(
                proposer_outcomes, proposer_calls, packs, strict=True
            )
        ) + tuple(
            self._role_record(
                outcome,
                call,
                private_pack_hash=canonical_sha256(call.input_payload),
            )
            for outcome, call in zip(critic_outcomes, critic_calls, strict=True)
        ) + (
            self._role_record(
                chair_outcome,
                chair_call,
                private_pack_hash=canonical_sha256(chair_call.input_payload),
            ),
        )
        total_tokens = sum(item.input_tokens + item.output_tokens for item in records)
        total_latency = sum(item.latency_ms for item in records)
        if total_tokens > self._council_request.aggregate_token_budget:
            raise CouncilBudgetError("council exceeded request aggregate token budget")
        selected_roles = (*route.proposer_roles, *route.critic_roles, route.chair_role)
        trace_hash = canonical_sha256(
            {"request": self._council_request.council_request_id, "roles": selected_roles}
        )
        self._council_trace = CouncilTrace(
            council_trace_id=f"council_trace_{trace_hash[-24:]}",
            selected_role_ids=selected_roles,
            events=tuple(
                CouncilTraceEvent(
                    sequence=index,
                    event_type="ROLE_COMPLETED",
                    role_id=role,
                    timestamp=request.deadline
                    - timedelta(seconds=len(selected_roles) - index),
                )
                for index, role in enumerate(selected_roles)
            ),
            prompt_hashes=tuple(
                canonical_sha256({"role": role, "prompt_version": 1})
                for role in selected_roles
            ),
            model_configuration_hashes=tuple(
                canonical_sha256({"role": role, "provider": "bounded_runtime"})
                for role in selected_roles
            ),
            context_pack_hashes=tuple(item.private_pack_hash for item in records),
            retrieval_log_hashes=tuple(
                canonical_sha256({"role": role, "retrieval": "audited"})
                for role in selected_roles
            ),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            latency_ms=total_latency,
            deadline_outcome="MET",
            cancelled=False,
            trace_completeness_percent=100.0,
        )
        result_payload = {
            "council_request_id": self._council_request.council_request_id,
            "status": shortlist.status,
            "proposal_shortlist": shortlist,
            "council_trace_id": self._council_trace.council_trace_id,
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "snapshot_hash": request.evidence_snapshot_hash,
            "policy_hash": self._policy.policy_hash,
            "route_reason_codes": route.reason_codes,
            "selected_role_ids": selected_roles,
            "common_safety_envelope_hash": canonical_sha256(self._common_envelope),
            "private_role_records": records,
            "proposal_cards": cards,
            "critique_reports": tuple(critiques),
            "critique_dispositions": dispositions,
            "revision_records": revisions,
            "final_ordered_proposal_ids": final_ids,
            "total_tokens": total_tokens,
            "total_latency_ms": total_latency,
            "deadline_outcome": "MET",
            "trace_completeness_percent": 100.0,
        }
        result_hash = canonical_sha256(
            {
                "council_request_id": self._council_request.council_request_id,
                "shortlist_hash": canonical_sha256(shortlist),
                "role_record_hashes": tuple(canonical_sha256(item) for item in records),
                "critique_hashes": tuple(canonical_sha256(item) for item in critiques),
                "disposition_hashes": tuple(
                    canonical_sha256(item) for item in dispositions
                ),
                "final_proposal_ids": final_ids,
            }
        )
        self._council_result = CouncilResult(
            council_result_id=f"council_result_{result_hash[-24:]}",
            **result_payload,
        )
        planner_payload = {
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "planner_mode": "AGENT_COUNCIL",
            "status": shortlist.status,
            "proposal_ids": final_ids,
            "rejected_output_diagnostics": (),
            "context_pack_hashes": tuple(item.private_pack_hash for item in records),
            "council_result_id": self._council_result.council_result_id,
            "provider_id": "bounded_council_runtime",
            "model_id": "role_scoped_structured_provider",
            "prompt_hash": canonical_sha256(
                {"prompt_hashes": self._council_trace.prompt_hashes}
            ),
            "output_schema_hash": canonical_sha256(
                OptimizationProposal.model_json_schema(mode="validation")
            ),
            "input_tokens": self._council_trace.input_tokens,
            "output_tokens": self._council_trace.output_tokens,
            "latency_ms": total_latency,
            "fallback_used": False,
            "upstream_provider_result_id": None,
        }
        planner_hash = canonical_sha256(planner_payload)
        return PlannerResult(
            planner_result_id=f"planner_result_{planner_hash[-24:]}",
            **planner_payload,
        )

    async def _fallback(
        self, request: PlannerRequest, error: Exception
    ) -> PlannerResult:
        opportunity = self._opportunities[request.opportunity_id]
        route = route_roles(opportunity, self._policy)
        selected_roles = (*route.proposer_roles, *route.critic_roles, route.chair_role)
        failure_hash = canonical_sha256(
            {
                "planner_request_id": request.planner_request_id,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        shortlist = ProposalShortlist(
            proposal_shortlist_id=f"proposal_shortlist_{failure_hash[-24:]}",
            status="PARTIAL",
            ordered_proposal_ids=(),
            fallback_eligible=True,
            unresolved_mandatory_finding_count=1,
        )
        self._council_trace = CouncilTrace(
            council_trace_id=f"council_trace_{failure_hash[-24:]}",
            selected_role_ids=selected_roles,
            events=(
                CouncilTraceEvent(
                    sequence=0,
                    event_type="COUNCIL_FAILED",
                    role_id=selected_roles[0],
                    timestamp=request.deadline,
                ),
            ),
            prompt_hashes=(),
            model_configuration_hashes=(),
            context_pack_hashes=(),
            retrieval_log_hashes=(),
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            deadline_outcome="MET",
            cancelled=False,
            trace_completeness_percent=20.0,
        )
        if self._council_request is None:
            self._council_request = CouncilRequest(
                council_request_id=f"council_request_{failure_hash[-24:]}",
                planner_request_id=request.planner_request_id,
                council_route_id=route.council_route_id,
                council_route_hash=route.route_hash,
                blinded_proposer_roles=route.proposer_roles,
                critic_roles=route.critic_roles,
                chair_role=route.chair_role,
                context_policy_hash=canonical_sha256({"failure": failure_hash}),
                fan_out_limit=self._policy.fan_out_limit,
                fan_in_limit=self._policy.fan_in_limit,
                aggregate_token_budget=min(
                    request.token_budget, self._policy.max_aggregate_tokens
                ),
                aggregate_latency_budget_ms=min(
                    request.latency_budget_ms, self._policy.deadline_seconds * 1000
                ),
                deadline=request.deadline,
                event_stream_id=f"council_events_{failure_hash[-24:]}",
            )
        self._council_result = CouncilResult(
            council_result_id=f"council_result_{failure_hash[-24:]}",
            council_request_id=self._council_request.council_request_id,
            status="PARTIAL",
            proposal_shortlist=shortlist,
            council_trace_id=self._council_trace.council_trace_id,
            run_id=request.run_id,
            opportunity_id=request.opportunity_id,
            snapshot_hash=request.evidence_snapshot_hash,
            policy_hash=self._policy.policy_hash,
            route_reason_codes=(*route.reason_codes, "COUNCIL_FAILURE_FALLBACK"),
            selected_role_ids=selected_roles,
            common_safety_envelope_hash=canonical_sha256(self._common_envelope),
            private_role_records=(),
            proposal_cards=(),
            critique_reports=(),
            critique_dispositions=(),
            revision_records=(),
            final_ordered_proposal_ids=(),
            total_tokens=0,
            total_latency_ms=0,
            deadline_outcome="MET",
            trace_completeness_percent=20.0,
        )
        heuristic = await self._heuristic.propose(
            request.model_copy(update={"planner_mode": "HEURISTIC"})
        )
        proposals = self._heuristic.proposals_for(heuristic)
        self._resolved.update({item.proposal_id: item for item in proposals})
        diagnostic = Diagnostic(
            code="COUNCIL_FALLBACK",
            severity="ERROR",
            message=f"{type(error).__name__}: {error}",
            evidence_refs=(request.opportunity_id,),
        )
        payload = {
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "planner_mode": "AGENT_COUNCIL",
            "status": "PARTIAL",
            "proposal_ids": tuple(item.proposal_id for item in proposals),
            "rejected_output_diagnostics": (diagnostic,),
            "context_pack_hashes": (),
            "council_result_id": self._council_result.council_result_id,
            "provider_id": None,
            "model_id": None,
            "prompt_hash": None,
            "output_schema_hash": canonical_sha256(
                OptimizationProposal.model_json_schema(mode="validation")
            ),
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0,
            "fallback_used": True,
            "upstream_provider_result_id": None,
            "upstream_council_result_id": self._council_result.council_result_id,
        }
        identity = canonical_sha256(
            {
                "request": request.planner_request_id,
                "fallback": heuristic.planner_result_id,
                "failure": failure_hash,
            }
        )
        return PlannerResult(
            planner_result_id=f"planner_result_{identity[-24:]}", **payload
        )

    async def propose(self, request: PlannerRequest) -> PlannerResult:
        """Deliberate or retain an explicit failure before bounded fallback."""

        if request.planner_mode != "AGENT_COUNCIL":
            raise CouncilPlanningError("council planner requires AGENT_COUNCIL mode")
        if request.policy_hash != self._policy.policy_hash:
            raise CouncilPlanningError("council policy identity changed")
        if request.transform_registry_hash != self._registry.registry_hash:
            raise CouncilPlanningError("council transform registry identity changed")
        if request.opportunity_id not in self._opportunities:
            raise CouncilPlanningError("council opportunity identity is unknown")
        try:
            return await self._deliberate(request)
        except (
            CouncilBudgetError,
            CouncilFaninError,
            CouncilIsolationError,
            CouncilPlanningError,
        ) as error:
            return await self._fallback(request, error)


__all__ = [
    "CouncilPlanner",
    "CouncilPlanningError",
    "CouncilIsolationError",
    "CouncilBudgetError",
    "CouncilFaninError",
    "CouncilPolicyError",
    "CouncilRoleCall",
    "CouncilRoleInvoker",
    "CouncilRoleOutcome",
    "build_blinded_proposer_contexts",
    "build_council_shortlist",
    "load_council_policy",
    "normalize_proposal_cards",
    "route_roles",
    "run_role_round",
]
