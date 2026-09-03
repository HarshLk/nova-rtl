"""Policy-safe optimization opportunity formation and deterministic ranking."""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.analysis import EvidenceGraphSnapshot
from nova_rtl.contracts.base import (
    EntityId,
    EvidenceRef,
    FiniteFloat,
    HashRef,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.optimization import (
    OpportunitySeverity,
    OptimizationOpportunity,
)
from nova_rtl.evidence.graph import EvidenceGraphDocument
from nova_rtl.evidence.models import EvidenceNode, PathCluster

CAUSE_TO_TRANSFORM = {
    "DEEP_PRIORITY_CHAIN": "RESTRUCTURE_PRIORITY_MUX",
    "FSM_DECODE_DEPTH": "FSM_DECODE_RESTRUCTURE",
    "REPEATED_DECODE": "FACTOR_COMMON_PREDICATE",
    "UNBALANCED_BOOLEAN_TREE": "BALANCE_BOOLEAN_TREE",
}
_NO_RTL_CAUSES = frozenset(
    {"PLACEMENT_OR_WIRE_DOMINATED", "CLOCK_OR_CONSTRAINT_ISSUE"}
)


class OpportunityFormationError(ValueError):
    """Cluster evidence or policy is incomplete or inconsistent."""


class OpportunityPolicy(StrictContract):
    """Deterministic transform availability and source authorization policy."""

    schema_version: Literal[1] = 1
    cause_to_transform: dict[str, str]
    available_transform_families: tuple[str, ...] = Field(min_length=1)
    editable_path_patterns: tuple[str, ...] = Field(min_length=1)
    proof_contracts: tuple[CorrectnessContract, ...] = Field(min_length=1)
    minimum_source_mapping_confidence: float = Field(strict=True, ge=0.0, le=1.0)
    maximum_estimated_proof_cost: float = Field(strict=True, ge=0.0, le=1.0)
    policy_hash: HashRef

    @field_validator(
        "available_transform_families", "editable_path_patterns", "proof_contracts"
    )
    @classmethod
    def tuple_fields_are_canonical(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(
                f"{getattr(info, 'field_name', 'policy values')} must be unique and ordered"
            )
        return value

    @field_validator("cause_to_transform")
    @classmethod
    def transform_mapping_is_canonical(cls, value: dict[str, str]) -> dict[str, str]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def registry_and_hash_are_consistent(self) -> Self:
        if self.cause_to_transform != CAUSE_TO_TRANSFORM:
            raise ValueError("cause-to-transform mapping differs from the registered M3 mapping")
        if not set(self.available_transform_families).issubset(
            set(self.cause_to_transform.values())
        ):
            raise ValueError(
                "available transforms must come from the registered cause-to-transform mapping"
            )
        expected = canonical_sha256(self, exclude=frozenset({"policy_hash"}))
        if self.policy_hash != expected:
            raise ValueError("policy_hash does not match opportunity policy")
        return self


def default_opportunity_policy() -> OpportunityPolicy:
    payload = {
        "schema_version": 1,
        "cause_to_transform": dict(sorted(CAUSE_TO_TRANSFORM.items())),
        "available_transform_families": tuple(sorted(CAUSE_TO_TRANSFORM.values())),
        "editable_path_patterns": ("rtl/subsystems/**", "rtl/workload/**"),
        "proof_contracts": ("STRICT_SEQ_EQUIV",),
        "minimum_source_mapping_confidence": 0.8,
        "maximum_estimated_proof_cost": 1.0,
    }
    return OpportunityPolicy(**payload, policy_hash=canonical_sha256(payload))


class EvidenceGraphView(StrictContract):
    """Verified public graph identity paired with its decompressed document."""

    snapshot: EvidenceGraphSnapshot
    document: EvidenceGraphDocument

    @model_validator(mode="after")
    def counts_and_artifact_roles_match(self) -> Self:
        if dict(Counter(item.kind for item in self.document.nodes)) != self.snapshot.node_counts:
            raise ValueError("evidence graph node counts differ from snapshot")
        if dict(Counter(item.kind for item in self.document.edges)) != self.snapshot.edge_counts:
            raise ValueError("evidence graph edge counts differ from snapshot")
        return self


class RankedOpportunitySet(StrictContract):
    """Schema-valid opportunities plus deterministic controller-owned priorities."""

    schema_version: Literal[1] = 1
    evidence_snapshot_hash: HashRef
    policy_hash: HashRef
    opportunities: tuple[OptimizationOpportunity, ...] = Field(min_length=1)
    priority_scores: dict[EntityId, FiniteFloat]
    ranking_hash: HashRef

    @field_validator("priority_scores")
    @classmethod
    def scores_are_canonical(cls, value: dict[str, float]) -> dict[str, float]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def ranking_is_complete_ordered_and_self_hashed(self) -> Self:
        ids = tuple(item.opportunity_id for item in self.opportunities)
        if len(ids) != len(set(ids)) or set(ids) != set(self.priority_scores):
            raise ValueError("ranked opportunities and score identities must match")
        expected_order = tuple(
            sorted(ids, key=lambda item: (-self.priority_scores[item], item))
        )
        if ids != expected_order:
            raise ValueError("opportunities must be ordered by deterministic priority")
        if any(
            {ref.snapshot_hash for ref in item.evidence_refs}
            != {self.evidence_snapshot_hash}
            for item in self.opportunities
        ):
            raise ValueError("ranked opportunities must bind the evidence snapshot")
        expected = canonical_sha256(self, exclude=frozenset({"ranking_hash"}))
        if self.ranking_hash != expected:
            raise ValueError("ranking_hash does not match ranked opportunities")
        return self


def _node_by_semantic_id(graph: EvidenceGraphView) -> dict[str, EvidenceNode]:
    nodes = {item.semantic_id: item for item in graph.document.nodes}
    if len(nodes) != len(graph.document.nodes):
        raise OpportunityFormationError("evidence graph contains duplicate semantic IDs")
    return nodes


def _ref_for_node(node: EvidenceNode, snapshot_hash: str, kind: str) -> EvidenceRef:
    source = node.evidence_refs[0]
    return EvidenceRef(
        evidence_id=node.semantic_id,
        kind=kind,
        artifact_id=source.artifact_id,
        json_pointer=source.json_pointer,
        snapshot_hash=snapshot_hash,
    )


def _matches_editable_path(node: EvidenceNode, policy: OpportunityPolicy) -> bool:
    relative_path = node.attributes.get("relative_path")
    return isinstance(relative_path, str) and any(
        fnmatch.fnmatchcase(relative_path, pattern)
        for pattern in policy.editable_path_patterns
    )


def form_opportunities(
    cluster: PathCluster,
    graph: EvidenceGraphView,
    policy: OpportunityPolicy,
) -> tuple[OptimizationOpportunity, ...]:
    """Form one safe, evidence-grounded optimization decision for a path cluster."""

    if cluster.evidence_snapshot_hash != graph.snapshot.snapshot_hash:
        raise OpportunityFormationError("cluster and evidence graph snapshot hashes differ")
    nodes = _node_by_semantic_id(graph)
    required_ids = set(cluster.path_ids) | set(cluster.source_span_ids)
    required_ids |= set(cluster.dominant_object_ids) | set(cluster.protected_neighbor_ids)
    missing = required_ids - set(nodes)
    if missing:
        raise OpportunityFormationError(
            f"cluster references unknown graph evidence: {sorted(missing)[0]}"
        )

    span_nodes = tuple(nodes[item] for item in cluster.source_span_ids)
    editable_spans = tuple(
        sorted(
            node.semantic_id
            for node in span_nodes
            if not node.protected and _matches_editable_path(node, policy)
        )
    )
    categories = {item.category for item in cluster.root_causes}
    transforms = tuple(
        sorted(
            {
                policy.cause_to_transform[category]
                for category in categories
                if category in policy.cause_to_transform
                and policy.cause_to_transform[category]
                in policy.available_transform_families
            }
        )
    )
    if cluster.protected_neighbor_ids or "CDC_ADJACENT_UNSAFE_TO_EDIT" in categories:
        editability = "PROTECTED_OR_UNSAFE"
    elif categories & _NO_RTL_CAUSES:
        editability = "NO_RTL_ACTION"
    elif (
        cluster.features.source_mapping_confidence
        < policy.minimum_source_mapping_confidence
        or cluster.features.estimated_proof_cost > policy.maximum_estimated_proof_cost
        or not cluster.source_span_ids
    ):
        editability = "INSUFFICIENT_EVIDENCE"
    elif not editable_spans or not transforms:
        editability = "NO_RTL_ACTION"
    else:
        editability = "RTL_EDITABLE"

    evidence_refs = []
    for semantic_id in sorted(required_ids):
        node = nodes[semantic_id]
        kind = {
            "SOURCE_SPAN": "SOURCE_SPAN",
            "TIMING_PATH": "PATH",
        }.get(node.kind, "CONE")
        evidence_refs.append(
            _ref_for_node(node, graph.snapshot.snapshot_hash, kind)
        )
    opportunity_id = "opportunity_" + canonical_sha256(
        {
            "cluster_hash": cluster.cluster_hash,
            "policy_hash": policy.policy_hash,
        }
    ).removeprefix("sha256:")[:24]
    opportunity = OptimizationOpportunity(
        opportunity_id=opportunity_id,
        parent_candidate_id=cluster.candidate_id,
        target_domain=cluster.target_domain,
        target_analysis_view_id=cluster.worst_analysis_view_id,
        affected_analysis_view_ids=cluster.analysis_view_ids,
        root_causes=cluster.root_causes,
        severity=OpportunitySeverity(
            worst_view_id=cluster.worst_analysis_view_id,
            worst_slack_ns=cluster.features.worst_slack_ns,
            affected_endpoints=cluster.features.affected_endpoints,
            tns_share_percent=cluster.features.tns_share_percent,
        ),
        editability=editability,
        source_spans=editable_spans if editability == "RTL_EDITABLE" else (),
        protected_neighbors=cluster.protected_neighbor_ids,
        eligible_transform_families=transforms if editability == "RTL_EDITABLE" else (),
        proof_contracts=policy.proof_contracts if editability == "RTL_EDITABLE" else (),
        evidence_refs=tuple(sorted(evidence_refs, key=lambda item: item.evidence_id)),
    )
    return (opportunity,)


def _priority_score(opportunity: OptimizationOpportunity, cluster: PathCluster) -> float:
    editability_weight = {
        "RTL_EDITABLE": 50.0,
        "NO_RTL_ACTION": 0.0,
        "INSUFFICIENT_EVIDENCE": -20.0,
        "PROTECTED_OR_UNSAFE": -50.0,
    }[opportunity.editability]
    timing_severity = min(max(-cluster.features.worst_slack_ns, 0.0) * 100.0, 100.0)
    score = (
        editability_weight
        + timing_severity
        + 0.25 * cluster.features.tns_share_percent
        + 2.0 * min(cluster.features.affected_endpoints, 10)
        + min(cluster.features.reconvergence_count, 10)
        - 10.0 * cluster.features.physical_dominance
        - 10.0 * cluster.features.estimated_proof_cost
    )
    return round(score, 6)


def rank_opportunities(
    clusters: Sequence[PathCluster],
    graph: EvidenceGraphView,
    policy: OpportunityPolicy,
) -> RankedOpportunitySet:
    """Form and globally rank opportunities using only deterministic evidence."""

    if not clusters:
        raise OpportunityFormationError("at least one path cluster is required")
    formed = [form_opportunities(cluster, graph, policy)[0] for cluster in clusters]
    cluster_by_opportunity = {
        opportunity.opportunity_id: cluster
        for opportunity, cluster in zip(formed, clusters, strict=True)
    }
    scores = {
        opportunity.opportunity_id: _priority_score(
            opportunity, cluster_by_opportunity[opportunity.opportunity_id]
        )
        for opportunity in formed
    }
    ordered = tuple(
        sorted(formed, key=lambda item: (-scores[item.opportunity_id], item.opportunity_id))
    )
    canonical_scores = dict(sorted(scores.items()))
    payload = {
        "schema_version": 1,
        "evidence_snapshot_hash": graph.snapshot.snapshot_hash,
        "policy_hash": policy.policy_hash,
        "opportunities": tuple(item.model_dump(mode="json") for item in ordered),
        "priority_scores": canonical_scores,
    }
    return RankedOpportunitySet(
        evidence_snapshot_hash=graph.snapshot.snapshot_hash,
        policy_hash=policy.policy_hash,
        opportunities=ordered,
        priority_scores=canonical_scores,
        ranking_hash=canonical_sha256(payload),
    )


__all__ = [
    "CAUSE_TO_TRANSFORM",
    "EvidenceGraphView",
    "OpportunityFormationError",
    "OpportunityPolicy",
    "RankedOpportunitySet",
    "default_opportunity_policy",
    "form_opportunities",
    "rank_opportunities",
]
