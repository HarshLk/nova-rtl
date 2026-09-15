"""Bounded, role-scoped M8 council planning primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.optimization import OptimizationOpportunity
from nova_rtl.contracts.planning import (
    ContextRequest,
    CouncilPolicy,
    CouncilRoute,
    PlannerRequest,
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


__all__ = [
    "CouncilIsolationError",
    "CouncilBudgetError",
    "CouncilPolicyError",
    "CouncilRoleCall",
    "CouncilRoleInvoker",
    "CouncilRoleOutcome",
    "build_blinded_proposer_contexts",
    "load_council_policy",
    "route_roles",
    "run_role_round",
]
