from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.manifest import ProjectManifest


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def valid_manifest_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "project": "nebula_multiclock_benchmark",
        "top": "nebula_top",
        "rtl": {
            "files": ["rtl/nebula_top.sv", "rtl/domains/*.sv", "rtl/cdc/*.sv"],
            "include_dirs": ["rtl/include"],
            "defines": [],
        },
        "compilation_profiles": {
            "functional": {"defines": [], "parameters": {}},
            "formal": {
                "property_files": ["formal/properties/*.sv"],
                "harness_manifest": "formal/harnesses.yaml",
                "property_only_define": "FORMAL",
                "disallow_design_behavior_defines": True,
                "require_functional_logic_hash_match": True,
            },
        },
        "constraints": {
            "sdc": "constraints/nebula.sdc",
            "immutable": True,
            "expected_master_clocks": 5,
            "expected_generated_clocks_per_master": 21,
            "require_zero_unconstrained_endpoints": True,
            "require_constraint_binding_equivalence": True,
            "require_exception_coverage_equivalence": True,
        },
        "technology": {
            "platform_id": "asap7",
            "platform_lock": "config/platform/platform.lock.yaml",
            "platform_lock_hash": hash_ref("1"),
        },
        "analysis_views": [
            {
                "id": "func_setup_slow",
                "mode": "FUNCTIONAL",
                "check": "SETUP",
                "liberty_corner": "asap7_wc",
                "rc_corner": "asap7_rc_nominal",
                "operating_condition": "pvt_0p63v_100c",
                "derate_policy": "config/timing_derates_slow.yaml",
                "clock_uncertainty_policy": "config/clock_uncertainty.yaml",
                "hard_limits": {"setup_wns_ns": 0.0},
                "sdc": "constraints/nebula.sdc",
                "required_stages": ["OPENSTA_FULL", "OPENROAD_PLACED_CTS"],
                "required": True,
            },
            {
                "id": "func_hold_fast",
                "mode": "FUNCTIONAL",
                "check": "HOLD",
                "liberty_corner": "asap7_bc",
                "rc_corner": "asap7_rc_nominal",
                "operating_condition": "pvt_0p77v_0c",
                "derate_policy": "config/timing_derates_fast.yaml",
                "clock_uncertainty_policy": "config/clock_uncertainty.yaml",
                "hard_limits": {"hold_wns_ns": 0.0},
                "sdc": "constraints/nebula.sdc",
                "required_stages": ["OPENSTA_FULL", "OPENROAD_PLACED_CTS"],
                "required": True,
            },
        ],
        "power_activity": {
            "format": "VCD",
            "source": "sim/activity/nebula_power_workload.vcd",
            "scope": "nebula_top",
            "time_window_ns": [1000.0, 101000.0],
            "require_same_activity_hash": True,
            "vectorless_fallback": "ESTIMATED_ONLY_NOT_COMPARABLE",
        },
        "cdc": {
            "checker_mode": "STRUCTURAL_INVARIANT_AUDIT",
            "approved_pattern_registry": "config/policy/cdc_patterns.yaml",
            "require_zero_unapproved_crossings": True,
            "require_candidate_inventory_equivalence": True,
            "protocol_property_manifest": "formal/cdc_protocol_properties.yaml",
            "external_cdc_tool": None,
        },
        "formal": {
            "primary_competition_contract": "STRICT_SEQ_EQUIV",
            "require_primary_latency_preserving_candidate": True,
            "proof_scope_policy": "WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
            "asynchronous_master_clocks": 5,
            "master_clock_model": "INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
            "generated_clock_model": "DERIVED_FROM_PROTECTED_DIVIDER_STATE",
            "sby_multiclock": True,
            "reset_assumption_manifest": "config/formal/reset_assumptions.yaml",
            "prohibit_assumption_only_equivalence": True,
        },
        "protection": {
            "modules": ["clock_divider_bank", "async_fifo", "sync_2ff", "reset_synchronizer"],
            "path_patterns": ["rtl/clocking/**", "rtl/cdc/**"],
        },
        "optimization": {
            "objective_policy": "BALANCED_PPA",
            "max_area_growth_percent": 5.0,
            "max_candidates": 40,
            "openroad_finalists": 4,
            "allowed_contracts": ["STRICT_SEQ_EQUIV", "RETIMING_EQUIV", "LATENCY_AWARE"],
            "editable_path_patterns": ["rtl/domains/**"],
            "deterministic_seed": 20260808,
        },
        "planner": {
            "mode": "AGENT_COUNCIL",
            "runtime": "LANGGRAPH",
            "fallback_order": ["SINGLE_AGENT", "HEURISTIC"],
            "max_proposals": 3,
            "max_parallel_specialists": 3,
            "max_revision_rounds": 1,
            "deadline_seconds": 120,
            "aggregate_token_budget": 30000,
        },
        "context_isolation": {
            "mode": "ROLE_SCOPED_EVIDENCE_PACKS",
            "shared_envelope_max_tokens": 2000,
            "private_pack_max_tokens": 7000,
            "chair_pack_max_tokens": 6000,
            "allow_read_only_retrieval": True,
            "require_snapshot_hash_match": True,
            "blind_independent_round": True,
            "blind_parallel_critics": True,
            "randomize_neutral_proposal_order": True,
        },
        "recovery": {
            "enabled": True,
            "policy_version": "recovery-policy-v1",
            "diagnostic_rule_registry": "config/recovery_rules.yaml",
            "fingerprint_schema": "candidate-fingerprint-v1",
            "similarity_threshold": 0.85,
            "no_progress_wns_epsilon_ns": 0.01,
            "no_progress_area_epsilon_percent": 0.1,
            "repeated_failure_count": 2,
            "max_recovery_depth_per_lineage": 4,
            "allow_targeted_recovery_council": True,
            "force_fresh_sessions_after_stagnation": True,
            "prohibit_same_transform_family_after_stagnation": True,
        },
        "physical": {
            "placement_finalists": 4,
            "routed_finalists": 2,
            "repeat_final_seeds": [20260808, 20260809, 20260810],
        },
    }


def test_project_manifest_accepts_the_locked_five_clock_strict_contract() -> None:
    manifest = ProjectManifest.model_validate(valid_manifest_payload())

    assert manifest.schema_version == 2
    assert manifest.constraints.expected_master_clocks == 5
    assert manifest.formal.primary_competition_contract == "STRICT_SEQ_EQUIV"
    assert tuple(view.id for view in manifest.analysis_views) == (
        "func_setup_slow",
        "func_hold_fast",
    )


def test_project_manifest_accepts_the_m0_locked_openroad_physical_stage() -> None:
    payload = valid_manifest_payload()
    views = payload["analysis_views"]
    assert isinstance(views, list)
    for view in views:
        view["required_stages"] = ["OPENSTA_FULL", "OPENROAD_PHYSICAL"]

    manifest = ProjectManifest.model_validate(payload)
    assert manifest.analysis_views[0].required_stages[-1] == "OPENROAD_PHYSICAL"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("constraints", "expected_master_clocks"), 4, "5"),
        (("formal", "primary_competition_contract"), "LATENCY_AWARE", "STRICT_SEQ_EQUIV"),
        (("constraints", "immutable"), False, "immutable"),
        (("optimization", "max_candidates"), 3, "physical finalist"),
    ],
)
def test_project_manifest_rejects_weakened_global_invariants(
    path: tuple[str, str], value: object, message: str
) -> None:
    payload = valid_manifest_payload()
    section = payload[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value

    with pytest.raises(ValidationError, match=message):
        ProjectManifest.model_validate(payload)


def test_project_manifest_requires_distinct_required_setup_and_hold_views() -> None:
    payload = valid_manifest_payload()
    views = payload["analysis_views"]
    assert isinstance(views, list)
    views[1]["check"] = "SETUP"
    views[1]["hard_limits"] = {"setup_wns_ns": 0.0}

    with pytest.raises(ValidationError, match="required setup and hold"):
        ProjectManifest.model_validate(payload)


def test_project_manifest_rejects_a_formal_only_define_in_functional_rtl() -> None:
    payload = valid_manifest_payload()
    rtl = payload["rtl"]
    assert isinstance(rtl, dict)
    rtl["defines"] = ["FORMAL=1"]

    with pytest.raises(ValidationError, match="property-only define"):
        ProjectManifest.model_validate(payload)


def test_project_manifest_rejects_protected_paths_in_the_editable_allowlist() -> None:
    payload = valid_manifest_payload()
    optimization = payload["optimization"]
    assert isinstance(optimization, dict)
    optimization["editable_path_patterns"] = ["rtl/cdc/**"]

    with pytest.raises(ValidationError, match="protected path"):
        ProjectManifest.model_validate(payload)


def test_project_manifest_rejects_unknown_nested_policy_fields() -> None:
    payload = deepcopy(valid_manifest_payload())
    planner = payload["planner"]
    assert isinstance(planner, dict)
    planner["agent_score"] = 0.99

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProjectManifest.model_validate(payload)
