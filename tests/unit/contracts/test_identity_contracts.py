from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.analysis import AnalysisViewContract, PowerActivityContract
from nova_rtl.contracts.manifest import DesignContract


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def canonical_payload_hash(payload: dict[str, object], hash_field: str) -> str:
    identity = {key: value for key, value in payload.items() if key != hash_field}
    encoded = json.dumps(
        identity,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def artifact_payload(artifact_id: str, uri: str, digit: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": "application/octet-stream",
        "size_bytes": 10,
        "created_at": "2026-08-21T06:30:00Z",
        "producer_stage_result_id": None,
        "classification": "RESTRICTED_RTL",
    }


def power_activity_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "power_activity_contract_id": "power_activity_main",
        "format": "VCD",
        "source_artifact": artifact_payload(
            "artifact_power_vcd", "artifact://inputs/power/activity.vcd", "1"
        ),
        "scope": "nebula_top",
        "time_window_ns": [1000.0, 101000.0],
        "propagation_policy": "ANNOTATED",
        "comparability": "OFFICIAL_COMPARABLE",
        "contract_hash": hash_ref("0"),
    }
    payload["contract_hash"] = canonical_payload_hash(payload, "contract_hash")
    return payload


def analysis_view_payload(check: str = "SETUP") -> dict[str, object]:
    limit = "setup_wns_ns" if check == "SETUP" else "hold_wns_ns"
    return {
        "schema_version": 1,
        "analysis_view_id": f"func_{check.lower()}_view",
        "mode": "FUNCTIONAL",
        "check": check,
        "required": True,
        "liberty_corner": {"id": "asap7_wc", "artifact_hash": hash_ref("2")},
        "rc_corner": {"id": "asap7_rc_nominal", "artifact_hash": hash_ref("3")},
        "sdc_hash": hash_ref("4"),
        "operating_condition": "PVT_0P63V_100C",
        "derate_policy_hash": hash_ref("5"),
        "clock_uncertainty_policy_hash": hash_ref("6"),
        "hard_limits": {limit: 0.0},
        "required_stages": ["OPENSTA_FULL", "OPENROAD_PLACED_CTS"],
        "power_activity_contract_id": None,
    }


def design_contract_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "design_contract_id": "design_contract_main",
        "run_id": "run_001",
        "project_manifest_hash": hash_ref("1"),
        "functional_source_artifacts": [
            artifact_payload("artifact_source_a", "artifact://inputs/rtl/a.sv", "2"),
            artifact_payload("artifact_source_b", "artifact://inputs/rtl/b.sv", "3"),
        ],
        "top": "nebula_top",
        "parameters": {"LANES": 4},
        "defines": [],
        "functional_compilation_profile_hash": hash_ref("4"),
        "formal_compilation_profile_hash": hash_ref("5"),
        "constraint_snapshot_artifact": artifact_payload(
            "artifact_constraints", "artifact://inputs/constraints/nebula.sdc", "6"
        ),
        "constraint_snapshot_hash": hash_ref("6"),
        "analysis_view_ids": ["func_hold_fast", "func_setup_slow"],
        "analysis_view_set_hash": hash_ref("7"),
        "power_activity_contract_id": "power_activity_main",
        "power_activity_contract_hash": hash_ref("8"),
        "platform_lock_hash": hash_ref("9"),
        "cdc_policy_id": "cdc_policy_main",
        "cdc_policy_hash": hash_ref("a"),
        "protection_policy_id": "protection_policy_main",
        "protection_policy_hash": hash_ref("b"),
        "formal_policy_id": "formal_policy_main",
        "formal_policy_hash": hash_ref("c"),
        "effective_policy_id": "effective_policy_main",
        "effective_policy_hash": hash_ref("d"),
        "organizer_decision_hash": hash_ref("e"),
        "created_at": "2026-08-21T06:30:00Z",
        "contract_hash": hash_ref("0"),
    }
    payload["contract_hash"] = canonical_payload_hash(payload, "contract_hash")
    return payload


def test_analysis_view_contract_requires_complete_timing_identity() -> None:
    view = AnalysisViewContract.model_validate(analysis_view_payload())
    assert view.check == "SETUP"

    missing_physical = analysis_view_payload()
    missing_physical["required_stages"] = ["OPENSTA_FULL"]
    with pytest.raises(ValidationError, match="physical stage"):
        AnalysisViewContract.model_validate(missing_physical)


def test_power_view_requires_a_power_activity_contract() -> None:
    payload = analysis_view_payload("SETUP")
    payload["analysis_view_id"] = "func_power_view"
    payload["check"] = "POWER"
    payload["hard_limits"] = {"power_total_uw": 1000.0}

    with pytest.raises(ValidationError, match="power_activity_contract_id"):
        AnalysisViewContract.model_validate(payload)


def test_power_activity_contract_validates_source_semantics_and_its_own_hash() -> None:
    contract = PowerActivityContract.model_validate(power_activity_payload())
    assert contract.comparability == "OFFICIAL_COMPARABLE"

    missing_source = power_activity_payload()
    missing_source["source_artifact"] = None
    missing_source["contract_hash"] = canonical_payload_hash(missing_source, "contract_hash")
    with pytest.raises(ValidationError, match="requires source_artifact"):
        PowerActivityContract.model_validate(missing_source)

    wrong_hash = power_activity_payload()
    wrong_hash["scope"] = "different_scope"
    with pytest.raises(ValidationError, match="contract_hash"):
        PowerActivityContract.model_validate(wrong_hash)


def test_design_contract_seals_sorted_sources_constraints_and_policy_identity() -> None:
    contract = DesignContract.model_validate(design_contract_payload())
    assert [item.artifact_id for item in contract.functional_source_artifacts] == [
        "artifact_source_a",
        "artifact_source_b",
    ]

    unsorted_sources = design_contract_payload()
    sources = unsorted_sources["functional_source_artifacts"]
    assert isinstance(sources, list)
    sources.reverse()
    unsorted_sources["contract_hash"] = canonical_payload_hash(unsorted_sources, "contract_hash")
    with pytest.raises(ValidationError, match="sorted"):
        DesignContract.model_validate(unsorted_sources)

    mismatched_constraint = design_contract_payload()
    mismatched_constraint["constraint_snapshot_hash"] = hash_ref("f")
    mismatched_constraint["contract_hash"] = canonical_payload_hash(
        mismatched_constraint, "contract_hash"
    )
    with pytest.raises(ValidationError, match="constraint_snapshot_hash"):
        DesignContract.model_validate(mismatched_constraint)


def test_design_contract_detects_identity_tampering() -> None:
    payload = deepcopy(design_contract_payload())
    payload["platform_lock_hash"] = hash_ref("f")

    with pytest.raises(ValidationError, match="contract_hash"):
        DesignContract.model_validate(payload)
