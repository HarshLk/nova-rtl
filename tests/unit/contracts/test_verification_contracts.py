from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.verification import (
    ConstraintBindingManifest,
    FormalModelContract,
    ProofResult,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def self_hash(payload: dict[str, object], field: str) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def artifact_payload(artifact_id: str, uri: str, digit: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": "text/plain",
        "size_bytes": 100,
        "created_at": "2026-08-21T08:00:00Z",
        "producer_stage_result_id": "stage_formal_001",
        "classification": "INTERNAL",
    }


def binding_manifest_payload() -> dict[str, object]:
    resolved_ids = ["pin:u_dma/sync_ff1/D", "pin:u_dma/sync_ff2/D"]
    command = {
        "command_id": "false_path_004",
        "normalized_command_hash": hash_ref("1"),
        "resolved_object_ids": resolved_ids,
        "resolved_object_set_hash": canonical_hash({"resolved_object_ids": resolved_ids}),
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "binding_manifest_id": "binding_baseline",
        "candidate_id": "baseline",
        "sdc_hash": hash_ref("2"),
        "netlist_snapshot_hash": hash_ref("3"),
        "analysis_view_ids": ["func_hold_fast", "func_setup_slow"],
        "resolved_commands": [command],
        "coverage": {
            "sequential_endpoints_total": 18342,
            "timed_endpoints": 18290,
            "reviewed_exception_endpoints": 52,
            "unresolved_selectors": 0,
        },
        "effective_binding_hash": hash_ref("0"),
        "comparison_to_baseline": "EQUIVALENT",
        "reviewed_mapping_refs": [],
    }
    payload["effective_binding_hash"] = self_hash(payload, "effective_binding_hash")
    return payload


def formal_model_payload(candidate_id: str = "cand_001") -> dict[str, object]:
    return {
        "schema_version": 1,
        "formal_model_contract_id": "formal_model_cand_001",
        "candidate_id": candidate_id,
        "functional_rtl_hash": hash_ref("1"),
        "parameter_hash": hash_ref("2"),
        "gold_snapshot_hash": hash_ref("3"),
        "gate_snapshot_hash": hash_ref("4") if candidate_id != "baseline" else None,
        "property_manifest_hash": hash_ref("5"),
        "master_clock_model": "INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
        "generated_clock_model": "DERIVED_FROM_PROTECTED_DIVIDER_STATE",
        "multiclock_enabled": True,
        "reset_assumption_hash": hash_ref("6"),
        "environment_assumption_hash": hash_ref("7"),
        "proof_scope_policy": "WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
        "behavioral_elaboration_delta": "NONE",
    }


def proof_result_payload() -> dict[str, object]:
    report = artifact_payload(
        "artifact_formal_report",
        "artifact://runs/run_001/candidates/cand_001/proof/report.txt",
        "8",
    )
    config = artifact_payload(
        "artifact_formal_config",
        "artifact://runs/run_001/candidates/cand_001/proof/config.sby",
        "9",
    )
    return {
        "schema_version": 1,
        "proof_result_id": "proof_cand_001",
        "run_id": "run_001",
        "candidate_id": "cand_001",
        "contract": "STRICT_SEQ_EQUIV",
        "outcome": "PASS",
        "formal_model_contract_id": "formal_model_cand_001",
        "gold_hash": hash_ref("3"),
        "gate_hash": hash_ref("4"),
        "proof_scope": "WHOLE_DESIGN",
        "partitions": [
            {
                "partition_id": "partition_whole_design",
                "status": "PASS",
                "runtime_ms": 4500,
                "strategy": "EQY_STRUCTURAL_THEN_SAT",
                "artifact_refs": [report.copy()],
            }
        ],
        "composition_manifest_artifact_id": None,
        "assumption_hashes": [hash_ref("6"), hash_ref("7")],
        "counterexample_artifact_id": None,
        "raw_artifacts": [report, config],
        "runtime_ms": 5000,
    }


def test_binding_manifest_requires_complete_endpoint_arithmetic_and_zero_unresolved() -> None:
    manifest = ConstraintBindingManifest.model_validate(binding_manifest_payload())
    assert manifest.comparison_to_baseline == "EQUIVALENT"

    incomplete = binding_manifest_payload()
    coverage = incomplete["coverage"]
    assert isinstance(coverage, dict)
    coverage["timed_endpoints"] = 18289
    incomplete["effective_binding_hash"] = self_hash(incomplete, "effective_binding_hash")
    with pytest.raises(ValidationError, match="sequential_endpoints_total"):
        ConstraintBindingManifest.model_validate(incomplete)

    unresolved = binding_manifest_payload()
    unresolved_coverage = unresolved["coverage"]
    assert isinstance(unresolved_coverage, dict)
    unresolved_coverage["unresolved_selectors"] = 1
    unresolved["effective_binding_hash"] = self_hash(unresolved, "effective_binding_hash")
    with pytest.raises(ValidationError, match="unresolved_selectors"):
        ConstraintBindingManifest.model_validate(unresolved)


def test_binding_manifest_checks_resolved_object_set_hash() -> None:
    payload = binding_manifest_payload()
    commands = payload["resolved_commands"]
    assert isinstance(commands, list)
    commands[0]["resolved_object_set_hash"] = hash_ref("f")
    payload["effective_binding_hash"] = self_hash(payload, "effective_binding_hash")

    with pytest.raises(ValidationError, match="resolved_object_set_hash"):
        ConstraintBindingManifest.model_validate(payload)


def test_formal_model_contract_fails_closed_on_behavioral_or_clock_model_delta() -> None:
    contract = FormalModelContract.model_validate(formal_model_payload())
    assert contract.behavioral_elaboration_delta == "NONE"

    changed = formal_model_payload()
    changed["behavioral_elaboration_delta"] = "DATAPATH_CHANGED"
    with pytest.raises(ValidationError, match="NONE"):
        FormalModelContract.model_validate(changed)

    candidate_without_gate = formal_model_payload()
    candidate_without_gate["gate_snapshot_hash"] = None
    with pytest.raises(ValidationError, match="gate_snapshot_hash"):
        FormalModelContract.model_validate(candidate_without_gate)


def test_proof_pass_requires_every_partition_and_closed_scope_to_pass() -> None:
    result = ProofResult.model_validate(proof_result_payload())
    assert result.outcome == "PASS"

    inconclusive_partition = proof_result_payload()
    partitions = inconclusive_partition["partitions"]
    assert isinstance(partitions, list)
    partitions[0]["status"] = "INCONCLUSIVE"
    with pytest.raises(ValidationError, match="PASS outcome"):
        ProofResult.model_validate(inconclusive_partition)

    open_composition = proof_result_payload()
    open_composition["proof_scope"] = "COMPOSITIONALLY_CLOSED"
    with pytest.raises(ValidationError, match="composition manifest"):
        ProofResult.model_validate(open_composition)


def test_proof_runtime_cannot_be_shorter_than_partition_runtime() -> None:
    payload = proof_result_payload()
    payload["runtime_ms"] = 4000

    with pytest.raises(ValidationError, match="partition runtime"):
        ProofResult.model_validate(payload)


def test_proof_nonpass_outcome_must_be_witnessed_by_a_partition() -> None:
    payload = proof_result_payload()
    payload["outcome"] = "INCONCLUSIVE"

    with pytest.raises(ValidationError, match="INCONCLUSIVE outcome"):
        ProofResult.model_validate(payload)


def test_successful_proof_cannot_reference_a_counterexample() -> None:
    payload = proof_result_payload()
    payload["counterexample_artifact_id"] = "artifact_formal_report"

    with pytest.raises(ValidationError, match="PASS outcome.*counterexample"):
        ProofResult.model_validate(payload)


def test_partition_artifacts_must_resolve_exactly_in_raw_artifacts() -> None:
    missing = proof_result_payload()
    partitions = missing["partitions"]
    assert isinstance(partitions, list)
    partitions[0]["artifact_refs"][0]["artifact_id"] = "artifact_unindexed_report"

    with pytest.raises(ValidationError, match="partition artifact"):
        ProofResult.model_validate(missing)

    mismatched = proof_result_payload()
    mismatched_partitions = mismatched["partitions"]
    assert isinstance(mismatched_partitions, list)
    mismatched_partitions[0]["artifact_refs"][0]["sha256"] = hash_ref("f")

    with pytest.raises(ValidationError, match="partition artifact"):
        ProofResult.model_validate(mismatched)
