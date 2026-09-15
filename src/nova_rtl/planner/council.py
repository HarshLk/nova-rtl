"""Bounded, role-scoped M8 council planning primitives."""

from __future__ import annotations

from pathlib import Path

import yaml

from nova_rtl.contracts.optimization import OptimizationOpportunity
from nova_rtl.contracts.planning import CouncilPolicy, CouncilRoute


class CouncilPolicyError(RuntimeError):
    """The council policy cannot be loaded without expanding authority."""


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


__all__ = ["CouncilPolicyError", "load_council_policy", "route_roles"]
