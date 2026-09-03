from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.analysis import (
    CDCInventory,
    ClockInventory,
    CriticalPathRecord,
    EvidenceGraphSnapshot,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def canonical_payload_hash(payload: dict[str, object], hash_field: str) -> str:
    encoded = json.dumps(
        {key: value for key, value in payload.items() if key != hash_field},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def artifact_payload(
    artifact_id: str, uri: str, digit: str, media_type: str
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": media_type,
        "size_bytes": 100,
        "created_at": "2026-08-21T07:00:00Z",
        "producer_stage_result_id": "stage_evidence_001",
        "classification": "INTERNAL",
    }


def critical_path_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "path_id": "path_setup_001",
        "candidate_id": "baseline",
        "analysis_view_id": "func_setup_slow",
        "path_group": "clk_dma",
        "launch_clock_id": "clk_master_0",
        "capture_clock_id": "clk_master_0",
        "startpoint": "pin:u_dma/start_reg/Q",
        "endpoint": "pin:u_dma/end_reg/D",
        "arrival_ns": 2.18,
        "required_ns": 2.0,
        "slack_ns": -0.18,
        "cell_delay_ns": 1.6,
        "net_delay_ns": 0.58,
        "logic_depth": 14,
        "max_fanout": 8,
        "object_sequence": ["pin:u_dma/start_reg/Q", "net:u_dma/n1", "pin:u_dma/end_reg/D"],
        "source_span_refs": ["source_span_001"],
        "raw_report_artifact_id": "artifact_opensta_report",
    }


def clock_inventory_payload() -> dict[str, object]:
    masters = []
    generated = []
    edges = []
    for index in range(5):
        master_id = f"clk_master_{index}"
        generated_id = f"clk_gen_{index}"
        masters.append(
            {
                "clock_id": master_id,
                "domain_id": f"domain_{index}",
                "source_object": f"port:clk_{index}",
                "period_ns": 2.0 + index,
                "waveform_ns": [0.0, 1.0],
                "active_consumer_count": 10,
            }
        )
        generated.append(
            {
                "clock_id": generated_id,
                "domain_id": f"domain_{index}",
                "master_clock_id": master_id,
                "source_object": f"pin:u_div_{index}/q",
                "multiply_by": 1,
                "divide_by": 2,
                "waveform_ns": [0.0, 2.0],
                "active_consumer_count": 4,
            }
        )
        edges.append(
            {
                "parent_clock_id": master_id,
                "child_clock_id": generated_id,
                "relationship": "GENERATED_FROM",
            }
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": "baseline",
        "expected_master_count": 5,
        "expected_generated_clocks_per_master": 1,
        "master_clocks": masters,
        "generated_clocks": generated,
        "lineage_edges": edges,
        "cross_master_synchronous_relationships": [],
        "clock_graph_hash": hash_ref("0"),
    }
    payload["clock_graph_hash"] = canonical_payload_hash(payload, "clock_graph_hash")
    return payload


def cdc_inventory_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": "baseline",
        "crossings": [
            {
                "crossing_id": "crossing_001",
                "source_domain_id": "domain_0",
                "destination_domain_id": "domain_1",
                "source_object": "pin:u_src/data/Q",
                "destination_object": "pin:u_sync/ff1/D",
                "signal_class": "SINGLE_BIT_CONTROL",
                "recognized_pattern": "sync_2ff",
                "structural_fingerprint": hash_ref("1"),
                "protocol_property_refs": ["property_sync_stable"],
                "status": "APPROVED",
            }
        ],
        "approved_pattern_registry_hash": hash_ref("2"),
        "new_unapproved_count": 0,
        "changed_approved_structure_count": 0,
        "removed_approved_structure_count": 0,
        "ambiguous_count": 0,
        "inventory_hash": hash_ref("0"),
    }
    payload["inventory_hash"] = canonical_payload_hash(payload, "inventory_hash")
    return payload


def evidence_graph_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "snapshot_hash": hash_ref("0"),
        "node_schema_version": 1,
        "edge_schema_version": 1,
        "node_counts": {"CELL": 100, "PIN": 300},
        "edge_counts": {"CONNECTS": 250, "TIMING_ARC": 180},
        "graph_artifact": artifact_payload(
            "artifact_evidence_graph",
            "artifact://runs/run_001/evidence/graph.json.zst",
            "3",
            "application/zstd",
        ),
        "source_map_artifact": artifact_payload(
            "artifact_source_map",
            "artifact://runs/run_001/evidence/source-map.json",
            "4",
            "application/json",
        ),
        "path_record_artifact": artifact_payload(
            "artifact_path_records",
            "artifact://runs/run_001/evidence/critical-paths.json",
            "5",
            "application/json",
        ),
        "clock_inventory_id": "clock_inventory_baseline",
        "cdc_inventory_id": "cdc_inventory_baseline",
        "evidence_resolution_index_hash": hash_ref("6"),
    }
    payload["snapshot_hash"] = canonical_payload_hash(payload, "snapshot_hash")
    return payload


def test_critical_path_record_checks_parser_arithmetic() -> None:
    record = CriticalPathRecord.model_validate(critical_path_payload())
    assert record.slack_ns == -0.18

    inconsistent = critical_path_payload()
    inconsistent["slack_ns"] = -0.1
    with pytest.raises(ValidationError, match="required_ns - arrival_ns"):
        CriticalPathRecord.model_validate(inconsistent)

    hold = critical_path_payload()
    hold.update(path_type="MIN", arrival_ns=0.2, required_ns=0.1, slack_ns=0.1)
    assert CriticalPathRecord.model_validate(hold).path_type == "MIN"


def test_clock_inventory_requires_five_masters_complete_lineage_and_consumers() -> None:
    inventory = ClockInventory.model_validate(clock_inventory_payload())
    assert len(inventory.master_clocks) == 5

    missing_generated = clock_inventory_payload()
    generated = missing_generated["generated_clocks"]
    assert isinstance(generated, list)
    generated.pop()
    missing_generated["clock_graph_hash"] = canonical_payload_hash(
        missing_generated, "clock_graph_hash"
    )
    with pytest.raises(ValidationError, match="generated clock count"):
        ClockInventory.model_validate(missing_generated)

    inactive = clock_inventory_payload()
    masters = inactive["master_clocks"]
    assert isinstance(masters, list)
    masters[0]["active_consumer_count"] = 0
    inactive["clock_graph_hash"] = canonical_payload_hash(inactive, "clock_graph_hash")
    with pytest.raises(ValidationError):
        ClockInventory.model_validate(inactive)


def test_cdc_inventory_preserves_unsafe_counts_and_detects_tampering() -> None:
    inventory = CDCInventory.model_validate(cdc_inventory_payload())
    assert inventory.new_unapproved_count == 0

    bad_count = cdc_inventory_payload()
    bad_count["ambiguous_count"] = 1
    bad_count["inventory_hash"] = canonical_payload_hash(bad_count, "inventory_hash")
    with pytest.raises(ValidationError, match="ambiguous_count"):
        CDCInventory.model_validate(bad_count)

    tampered = cdc_inventory_payload()
    tampered["approved_pattern_registry_hash"] = hash_ref("f")
    with pytest.raises(ValidationError, match="inventory_hash"):
        CDCInventory.model_validate(tampered)


def test_evidence_graph_snapshot_binds_all_indexes_and_artifacts() -> None:
    graph = EvidenceGraphSnapshot.model_validate(evidence_graph_payload())
    assert graph.graph_artifact.media_type == "application/zstd"

    upstream_artifacts = evidence_graph_payload()
    upstream_artifacts["source_map_artifact"]["producer_stage_result_id"] = (
        "stage_yosys_001"
    )
    upstream_artifacts["path_record_artifact"]["producer_stage_result_id"] = (
        "stage_opensta_001"
    )
    upstream_artifacts["snapshot_hash"] = canonical_payload_hash(
        upstream_artifacts, "snapshot_hash"
    )
    graph = EvidenceGraphSnapshot.model_validate(upstream_artifacts)
    assert graph.source_map_artifact.producer_stage_result_id == "stage_yosys_001"

    tampered = evidence_graph_payload()
    tampered["clock_inventory_id"] = "clock_inventory_other"
    with pytest.raises(ValidationError, match="snapshot_hash"):
        EvidenceGraphSnapshot.model_validate(tampered)
