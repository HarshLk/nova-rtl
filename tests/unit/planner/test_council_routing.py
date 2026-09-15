from pathlib import Path

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import (
    OpportunitySeverity,
    OptimizationOpportunity,
    RootCause,
)
from nova_rtl.planner.council import load_council_policy, route_roles


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _opportunity(root_cause: str) -> OptimizationOpportunity:
    evidence = EvidenceRef(
        evidence_id="source_span_target",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_target",
        snapshot_hash=_hash("a"),
    )
    return OptimizationOpportunity(
        opportunity_id=f"opportunity_{root_cause.lower()}",
        parent_candidate_id="baseline",
        target_domain="compute_domain",
        target_analysis_view_id="asap7_setup",
        affected_analysis_view_ids=("asap7_setup",),
        root_causes=(RootCause(category=root_cause, confidence=0.9),),
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


def test_route_roles_selects_one_domain_specialist_deterministically() -> None:
    policy = load_council_policy(Path("config/policy/council.yaml"))

    routes = {
        cause: route_roles(_opportunity(cause), policy)
        for cause in (
            "DEEP_PRIORITY_CHAIN",
            "SERIAL_ARITHMETIC",
            "FSM_DECODE_DEPTH",
            "HIGH_FANOUT_CONTROL",
        )
    }

    assert routes["DEEP_PRIORITY_CHAIN"].proposer_roles == (
        "timing_forensics",
        "logic_domain_specialist",
    )
    assert routes["SERIAL_ARITHMETIC"].proposer_roles[-1] == "arithmetic_specialist"
    assert routes["FSM_DECODE_DEPTH"].proposer_roles[-1] == "fsm_specialist"
    assert routes["HIGH_FANOUT_CONTROL"].proposer_roles[-1] == "fanout_resource_specialist"
    assert all(
        route.critic_roles == ("formal_critic", "ppa_critic")
        for route in routes.values()
    )
    assert all(route.chair_role == "proposal_chair" for route in routes.values())
    assert all(
        len({*route.proposer_roles, *route.critic_roles, route.chair_role}) == 5
        for route in routes.values()
    )


def test_route_identity_changes_only_with_material_route_input() -> None:
    policy = load_council_policy(Path("config/policy/council.yaml"))
    opportunity = _opportunity("SERIAL_ARITHMETIC")

    first = route_roles(opportunity, policy)
    repeat = route_roles(opportunity, policy)
    changed = route_roles(_opportunity("FSM_DECODE_DEPTH"), policy)

    assert first == repeat
    assert first.route_hash != changed.route_hash
