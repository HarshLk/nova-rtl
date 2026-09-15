"""Bounded, role-scoped M8 council planning primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.optimization import OptimizationOpportunity, OptimizationProposal
from nova_rtl.contracts.planning import (
    ContextRequest,
    CouncilPolicy,
    CouncilRevisionRecord,
    CouncilRoute,
    CritiqueDisposition,
    CritiqueReport,
    PlannerRequest,
    ProposalCard,
    ProposalShortlist,
    RoleContextPack,
    RoleStatus,
)
from nova_rtl.planner.context import build_context
from nova_rtl.planner.evidence import InMemoryEvidenceProvider


class CouncilPolicyError(RuntimeError):
    """The council policy cannot be loaded without expanding authority."""


class CouncilIsolationError(RuntimeError):
    """Council private context boundaries overlap or are incomplete."""


class CouncilBudgetError(RuntimeError):
    """A council call, fan-out, deadline, or aggregate budget was exceeded."""


class CouncilFaninError(RuntimeError):
    """Critic or chair fan-in is incomplete, ambiguous, or unsafe."""


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
    policy_path: Path,
) -> ProposalShortlist:
    """Apply deterministic mandatory-critic and chair closure rules."""

    policy = load_council_policy(policy_path)
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


__all__ = [
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
