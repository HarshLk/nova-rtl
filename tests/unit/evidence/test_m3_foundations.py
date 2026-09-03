"""Contract tests for M3's internal evidence identity boundary."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nova_rtl.evidence.models import (
    AnalysisViewEvidenceIdentity,
    EvidenceEdge,
    EvidenceInputIdentity,
    EvidenceNode,
    PathCluster,
    PathFeatureVector,
    SourceSpanRecord,
    build_evidence_input_identity,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def payload_hash(payload: dict[str, object], hash_field: str) -> str:
    encoded = json.dumps(
        {key: value for key, value in payload.items() if key != hash_field},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def view_identity(view_id: str, digit: str) -> AnalysisViewEvidenceIdentity:
    return AnalysisViewEvidenceIdentity(
        analysis_view_id=view_id,
        analysis_view_hash=hash_ref(digit),
        opensta_stage_result_hash=hash_ref(digit),
    )


def input_identity(
    views: tuple[AnalysisViewEvidenceIdentity, ...],
) -> EvidenceInputIdentity:
    return build_evidence_input_identity(
        candidate_id="baseline",
        rtl_snapshot_hash=hash_ref("1"),
        design_contract_hash=hash_ref("2"),
        constraint_binding_hash=hash_ref("3"),
        platform_lock_hash=hash_ref("4"),
        synthesis_structure_hash=hash_ref("5"),
        clock_inventory_hash=hash_ref("6"),
        cdc_inventory_hash=hash_ref("7"),
        protection_policy_hash=hash_ref("8"),
        analysis_views=views,
    )


def evidence_ref(evidence_id: str, kind: str, digit: str) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "artifact_id": f"artifact_{evidence_id}",
        "json_pointer": f"/records/{evidence_id}",
        "snapshot_hash": hash_ref(digit),
    }


def feature_payload() -> dict[str, object]:
    return {
        "worst_slack_ns": -0.42,
        "tns_share_percent": 37.5,
        "affected_endpoints": 8,
        "cell_delay_fraction": 0.72,
        "net_delay_fraction": 0.28,
        "logic_depth": 14,
        "max_fanout": 11,
        "mux_depth": 6,
        "boolean_depth": 2,
        "comparator_depth": 1,
        "arithmetic_depth": 0,
        "repeated_predicate_count": 4,
        "reconvergence_count": 1,
        "physical_dominance": 0.2,
        "source_mapping_confidence": 0.95,
        "protection_distance": 3,
        "estimated_proof_cost": 0.35,
    }


def cluster_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "cluster_id": "path_cluster_001",
        "candidate_id": "baseline",
        "evidence_snapshot_hash": hash_ref("9"),
        "target_domain": "domain_ingress",
        "worst_analysis_view_id": "asap7_setup",
        "path_ids": ["path_001", "path_002"],
        "analysis_view_ids": ["asap7_hold", "asap7_setup"],
        "dominant_object_ids": ["cell_decode_001", "net_priority_001"],
        "source_span_ids": ["source_span_001"],
        "protected_neighbor_ids": ["cdc_sync_001"],
        "features": feature_payload(),
        "root_causes": [{"category": "DEEP_PRIORITY_CHAIN", "confidence": 0.9}],
        "cluster_hash": hash_ref("0"),
    }
    payload["cluster_hash"] = payload_hash(payload, "cluster_hash")
    return payload


def test_evidence_input_identity_is_order_independent_and_change_sensitive() -> None:
    setup = view_identity("asap7_setup", "a")
    hold = view_identity("asap7_hold", "b")

    first = input_identity((setup, hold))
    second = input_identity((hold, setup))

    assert first == second
    assert tuple(item.analysis_view_id for item in first.analysis_views) == (
        "asap7_hold",
        "asap7_setup",
    )
    assert first.identity_hash == (
        "sha256:ab8d003a95d5b0e798465325af6dc49a9ac4fc7614e0b3d6f08e8f13eb466782"
    )

    changed = first.model_copy(update={"synthesis_structure_hash": hash_ref("9")})
    with pytest.raises(ValidationError, match="identity_hash"):
        EvidenceInputIdentity.model_validate(changed.model_dump(mode="json"))


def test_evidence_input_identity_rejects_duplicate_analysis_views() -> None:
    setup = view_identity("asap7_setup", "a")

    with pytest.raises(ValidationError, match="analysis view IDs must be unique"):
        input_identity((setup, setup))


def test_source_spans_are_relative_ordered_and_protection_consistent() -> None:
    span = SourceSpanRecord(
        source_span_id="source_span_001",
        rtl_snapshot_hash=hash_ref("1"),
        relative_path="rtl/domains/ingress.sv",
        start_line=41,
        start_column=9,
        end_line=48,
        end_column=24,
        owner_hierarchy="nebula_top.u_ingress",
        source_text_hash=hash_ref("2"),
        mapping_confidence=1.0,
        protected=True,
        protection_kinds=("CDC",),
    )
    assert span.relative_path == "rtl/domains/ingress.sv"

    with pytest.raises(ValidationError, match="normalized relative path"):
        SourceSpanRecord.model_validate(
            {**span.model_dump(mode="json"), "relative_path": "/tmp/ingress.sv"}
        )
    with pytest.raises(ValidationError, match="end coordinate"):
        SourceSpanRecord.model_validate(
            {**span.model_dump(mode="json"), "end_line": 40}
        )
    with pytest.raises(ValidationError, match="protection_kinds"):
        SourceSpanRecord.model_validate(
            {**span.model_dump(mode="json"), "protected": False}
        )


def test_graph_records_reject_unknown_kinds_and_mixed_snapshot_evidence() -> None:
    node = EvidenceNode(
        node_id="node_cell_001",
        candidate_id="baseline",
        kind="CELL",
        semantic_id="cell_decode_001",
        attributes={"width": 32, "hierarchy": "nebula_top.u_ingress"},
        provenance="TOOL_MEASURED",
        confidence=1.0,
        evidence_refs=(evidence_ref("source_span_001", "SOURCE_SPAN", "1"),),
        protected=False,
        protection_kinds=(),
    )
    assert tuple(node.attributes) == ("hierarchy", "width")

    with pytest.raises(ValidationError, match="Input should be"):
        EvidenceNode.model_validate({**node.model_dump(mode="json"), "kind": "UNKNOWN_NODE"})

    with pytest.raises(ValidationError, match="one snapshot"):
        EvidenceEdge(
            edge_id="edge_maps_001",
            kind="MAPS_TO",
            source_node_id="node_cell_001",
            target_node_id="node_source_001",
            attributes={},
            provenance="DETERMINISTIC_DERIVED",
            confidence=1.0,
            evidence_refs=(
                evidence_ref("cell_001", "CONE", "1"),
                evidence_ref("source_span_001", "SOURCE_SPAN", "2"),
            ),
        )


def test_path_clusters_require_canonical_members_and_self_hash() -> None:
    cluster = PathCluster.model_validate(cluster_payload())
    assert cluster.features == PathFeatureVector.model_validate(feature_payload())

    unordered = cluster_payload()
    unordered["path_ids"] = ["path_002", "path_001"]
    unordered["cluster_hash"] = payload_hash(unordered, "cluster_hash")
    with pytest.raises(ValidationError, match="path_ids must be canonically ordered"):
        PathCluster.model_validate(unordered)

    tampered = cluster_payload()
    features = tampered["features"]
    assert isinstance(features, dict)
    features["logic_depth"] = 15
    with pytest.raises(ValidationError, match="cluster_hash"):
        PathCluster.model_validate(tampered)

    unknown_worst_view = cluster_payload()
    unknown_worst_view["worst_analysis_view_id"] = "asap7_power"
    unknown_worst_view["cluster_hash"] = payload_hash(
        unknown_worst_view, "cluster_hash"
    )
    with pytest.raises(ValidationError, match="worst analysis view"):
        PathCluster.model_validate(unknown_worst_view)
