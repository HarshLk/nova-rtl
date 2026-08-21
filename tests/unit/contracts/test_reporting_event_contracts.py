from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.analysis import FrequencySweepContract
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.reporting import (
    ExperimentRecord,
    ParetoRecord,
    ReportBundle,
    SearchRequest,
    SearchResult,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def self_hash(payload: dict[str, object], field: str) -> str:
    encoded = json.dumps(
        {key: value for key, value in payload.items() if key != field},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def artifact_payload(artifact_id: str, suffix: str, digit: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": f"artifact://runs/run_001/{suffix}",
        "sha256": hash_ref(digit),
        "media_type": "application/json",
        "size_bytes": 100,
        "created_at": "2026-08-21T11:00:00Z",
        "producer_stage_result_id": "stage_report_001",
        "classification": "INTERNAL",
    }


def evidence_payload() -> dict[str, object]:
    return {
        "evidence_id": "path_delta_017",
        "kind": "PATH",
        "artifact_id": "artifact_timing",
        "json_pointer": "/paths/0",
        "snapshot_hash": hash_ref("a"),
    }


def metric_payload(view_id: str, setup_wns: float) -> dict[str, object]:
    unavailable = {
        "hold_wns_ns",
        "hold_tns_ns",
        "physical_area_um2",
        "buffer_count",
        "power_total_uw",
        "wirelength_um",
        "congestion_overflow",
    }
    return {
        "analysis_view_id": view_id,
        "setup_wns_ns": setup_wns,
        "setup_tns_ns": min(setup_wns, 0.0),
        "hold_wns_ns": None,
        "hold_tns_ns": None,
        "failing_endpoints": 0 if setup_wns >= 0 else 10,
        "critical_path_delay_ns": 2.0 - setup_wns,
        "estimated_fmax_mhz": 500.0,
        "mapped_area_um2": 220000.0,
        "physical_area_um2": None,
        "cell_count": 50500,
        "register_count": 18342,
        "buffer_count": None,
        "power_total_uw": None,
        "wirelength_um": None,
        "congestion_overflow": None,
        "runtime_ms": 2000,
        "missing_metric_reasons": {key: "not produced by this stage" for key in unavailable},
    }


def sweep_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "frequency_sweep_contract_id": "frequency_sweep_dma_001",
        "baseline_analysis_view_hash": hash_ref("1"),
        "target_master_clock_id": "clk_dma",
        "target_domain_id": "domain_dma",
        "ordered_trial_periods_ns": [2.0, 1.5, 1.75],
        "fixed_non_target_clock_definitions_hash": hash_ref("2"),
        "setup_pass_limit_ns": 0.0,
        "hold_pass_limit_ns": 0.0,
        "constraint_overlay_generator_hash": hash_ref("3"),
        "search_method": "BOUNDED_BINARY",
        "maximum_trials": 8,
        "result_label_policy": "ACHIEVED_BY_SWEEP_ONLY_ON_SETUP_HOLD_PASS",
        "contract_hash": hash_ref("0"),
    }
    payload["contract_hash"] = self_hash(payload, "contract_hash")
    return payload


def run_event_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "event_000042",
        "run_id": "run_001",
        "sequence": 42,
        "event_type": "CANDIDATE_STATE_CHANGED",
        "entity_type": "CANDIDATE",
        "entity_id": "cand_017",
        "timestamp": "2026-08-21T11:00:00Z",
        "prior_state": "EVALUATING",
        "new_state": "FEASIBLE_PARETO",
        "policy_hash": hash_ref("b"),
        "payload_schema_name": "candidate-record",
        "payload_schema_version": 1,
        "payload_artifact": artifact_payload(
            "artifact_candidate_record", "events/event-42.json", "4"
        ),
        "duration_ms": 120,
        "resource_usage": {
            "cpu_time_ms": 100,
            "wall_time_ms": 120,
            "peak_rss_bytes": 1024,
        },
        "status": "PASS",
        "error_code": None,
    }


def experiment_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "experiment_record_id": "experiment_017",
        "run_id": "run_001",
        "planner_result_id": "planner_result_001",
        "council_result_id": "council_result_001",
        "opportunity_id": "path_cluster_0042",
        "cone_fingerprint": "cone:v3:7ed1",
        "proposal_id": "opt_017",
        "operation": "RESTRUCTURE_PRIORITY_MUX",
        "parameters": {"strategy": "BALANCED_PREDECODE"},
        "parent_candidate_id": "baseline",
        "candidate_id": "cand_017",
        "source_hash": hash_ref("1"),
        "patch_hash": hash_ref("2"),
        "transform_fingerprint": "transform:v1:913f",
        "stage_result_ids": ["stage_cand_017_synth", "stage_cand_017_formal"],
        "comparison_identity_hashes": {
            "analysis_view": hash_ref("3"),
            "constraint_binding": hash_ref("4"),
        },
        "proof_result_id": "proof_cand_017",
        "proof_outcome": "PASS",
        "counterexample_artifact_id": None,
        "before_metrics": {"func_setup_slow": metric_payload("func_setup_slow", -0.18)},
        "after_metrics": {"func_setup_slow": metric_payload("func_setup_slow", 0.04)},
        "failure_event_id": None,
        "repair_directive_id": None,
        "candidate_failure_fingerprint_id": None,
        "recovery_decision_id": None,
        "descendant_outcome": None,
        "role_ids": ["timing_forensics", "logic_specialist"],
        "model_configuration_hashes": [hash_ref("5")],
        "prompt_hashes": [hash_ref("6")],
        "context_pack_hashes": [hash_ref("7")],
        "input_tokens": 4000,
        "output_tokens": 900,
        "planner_latency_ms": 3000,
        "eda_runtime_ms": 12000,
        "terminal_disposition": "FEASIBLE_PARETO",
        "human_review": None,
        "created_at": "2026-08-21T11:00:00Z",
    }


def search_request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "search_request_id": "search_request_001",
        "run_id": "run_001",
        "design_contract_hash": hash_ref("1"),
        "policy_hash": hash_ref("b"),
        "transform_registry_hash": hash_ref("2"),
        "ordered_opportunity_ids": ["path_cluster_0042", "path_cluster_0043"],
        "planner_mode": "AGENT_COUNCIL",
        "required_correctness_class": "PRIMARY_STRICT",
        "candidate_budget": 20,
        "formal_budget": 10,
        "physical_budget": 3,
        "token_budget": 50000,
        "latency_budget_ms": 600000,
        "deterministic_seed": 42,
        "stop_policy_hash": hash_ref("3"),
        "created_at": "2026-08-21T11:00:00Z",
    }


def search_result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "search_result_id": "search_result_001",
        "search_request_id": "search_request_001",
        "status": "COMPLETED",
        "ordered_candidate_ids": ["cand_010", "cand_017"],
        "feasible_candidate_ids": ["cand_017"],
        "pareto_candidate_ids": ["cand_017"],
        "selected_candidate_id": "cand_017",
        "stop_reason_code": "POLICY_OBJECTIVE_MET",
        "consumed_budgets": {
            "candidates": 2,
            "formal_jobs": 1,
            "physical_jobs": 1,
            "tokens": 12000,
            "latency_ms": 120000,
        },
        "planner_result_ids": ["planner_result_001"],
        "council_result_ids": ["council_result_001"],
        "recovery_decision_ids": [],
        "event_sequence_range": [0, 42],
        "artifact_refs": [
            artifact_payload("artifact_search_trace", "search/trace.json", "5")
        ],
        "completed_at": "2026-08-21T11:02:00Z",
    }


def bundle_payload() -> dict[str, object]:
    timing = artifact_payload("artifact_timing", "reports/timing.json", "1")
    ppa = artifact_payload("artifact_ppa", "reports/ppa.json", "2")
    formal = artifact_payload("artifact_formal", "reports/formal.json", "3")
    binding = artifact_payload("artifact_binding", "reports/binding.json", "4")
    clock = artifact_payload("artifact_clock", "reports/clock.json", "5")
    cdc = artifact_payload("artifact_cdc", "reports/cdc.json", "6")
    replay = artifact_payload("artifact_replay_manifest", "replay/manifest.json", "7")
    ui = artifact_payload("artifact_ui_snapshot", "ui/summary.json", "8")
    payload: dict[str, object] = {
        "schema_version": 1,
        "report_bundle_id": "report_bundle_001",
        "run_id": "run_001",
        "selected_candidate_id": "cand_017",
        "claim_to_evidence_index": {
            "setup_improvement": {
                "artifact_ids": ["artifact_timing"],
                "evidence_refs": [evidence_payload()],
                "comparison_identity_hashes": {"analysis_view": hash_ref("a")},
            }
        },
        "baseline_final_comparison_identities": {"analysis_view": hash_ref("a")},
        "timing_artifact_refs": [timing],
        "ppa_artifact_refs": [ppa],
        "formal_artifact_refs": [formal],
        "binding_artifact_refs": [binding],
        "clock_artifact_refs": [clock],
        "cdc_artifact_refs": [cdc],
        "experiment_ledger_sequence_range": [0, 17],
        "experiment_ledger_hash": hash_ref("9"),
        "replay_manifest_artifact": replay,
        "replay_manifest_hash": hash_ref("7"),
        "ui_snapshot_refs": [ui],
        "bundle_hash": hash_ref("0"),
        "created_at": "2026-08-21T11:02:00Z",
    }
    payload["bundle_hash"] = self_hash(payload, "bundle_hash")
    return payload


def test_frequency_sweep_is_bounded_deterministic_and_self_hashed() -> None:
    sweep = FrequencySweepContract.model_validate(sweep_payload())
    assert sweep.search_method == "BOUNDED_BINARY"

    duplicate = sweep_payload()
    duplicate["ordered_trial_periods_ns"] = [2.0, 2.0]
    duplicate["contract_hash"] = self_hash(duplicate, "contract_hash")
    with pytest.raises(ValidationError, match="trial periods"):
        FrequencySweepContract.model_validate(duplicate)


def test_run_event_preserves_sequence_state_and_error_semantics() -> None:
    event = RunEvent.model_validate(run_event_payload())
    assert event.sequence == 42

    failed_without_error = run_event_payload()
    failed_without_error["status"] = "FAIL"
    with pytest.raises(ValidationError, match="error_code"):
        RunEvent.model_validate(failed_without_error)

    no_transition = run_event_payload()
    no_transition["new_state"] = "EVALUATING"
    with pytest.raises(ValidationError, match="state transition"):
        RunEvent.model_validate(no_transition)


def test_experiment_record_requires_comparable_views_and_proof_semantics() -> None:
    record = ExperimentRecord.model_validate(experiment_payload())
    assert record.proof_outcome == "PASS"

    mismatched_views = experiment_payload()
    mismatched_views["after_metrics"] = {
        "func_hold_fast": metric_payload("func_hold_fast", 0.01)
    }
    with pytest.raises(ValidationError, match="same analysis views"):
        ExperimentRecord.model_validate(mismatched_views)

    pass_with_counterexample = experiment_payload()
    pass_with_counterexample["counterexample_artifact_id"] = "artifact_cex"
    with pytest.raises(ValidationError, match="PASS proof"):
        ExperimentRecord.model_validate(pass_with_counterexample)


def test_pareto_and_search_contracts_fail_closed_on_class_and_subset_errors() -> None:
    pareto = ParetoRecord.model_validate(
        {
            "schema_version": 1,
            "pareto_record_id": "pareto_cand_017",
            "candidate_id": "cand_017",
            "correctness_contract": "STRICT_SEQ_EQUIV",
            "selection_class": "PRIMARY_STRICT",
            "feasibility_predicate_version": "feasibility-v1",
            "required_view_metric_ids": {"func_setup_slow": "stage_cand_017_sta"},
            "metric_vector": {"setup_wns_ns": 0.04, "mapped_area_um2": 220000.0},
            "dominated_candidate_ids": ["cand_010"],
            "objective_policy_rank": 1,
            "binding_hash": hash_ref("1"),
            "clock_hash": hash_ref("2"),
            "cdc_hash": hash_ref("3"),
            "formal_hash": hash_ref("4"),
            "physical_stage": "OPENROAD_ROUTED",
            "comparison_identity_hashes": {"analysis_view": hash_ref("a")},
            "recorded_at": "2026-08-21T11:00:00Z",
        }
    )
    assert pareto.selection_class == "PRIMARY_STRICT"

    request = SearchRequest.model_validate(search_request_payload())
    assert request.physical_budget < request.formal_budget

    invalid_budget = search_request_payload()
    invalid_budget["physical_budget"] = 11
    with pytest.raises(ValidationError, match="physical_budget"):
        SearchRequest.model_validate(invalid_budget)

    result = SearchResult.model_validate(search_result_payload())
    assert result.selected_candidate_id == "cand_017"

    invalid_subset = search_result_payload()
    invalid_subset["pareto_candidate_ids"] = ["cand_999"]
    with pytest.raises(ValidationError, match="Pareto candidates"):
        SearchResult.model_validate(invalid_subset)


def test_report_bundle_resolves_every_claim_and_comparison_identity() -> None:
    bundle = ReportBundle.model_validate(bundle_payload())
    assert bundle.replay_manifest_hash == bundle.replay_manifest_artifact.sha256

    unresolved = deepcopy(bundle_payload())
    claim = unresolved["claim_to_evidence_index"]["setup_improvement"]
    claim["artifact_ids"] = ["artifact_missing"]
    unresolved["bundle_hash"] = self_hash(unresolved, "bundle_hash")
    with pytest.raises(ValidationError, match="claim artifact"):
        ReportBundle.model_validate(unresolved)

    wrong_identity = deepcopy(bundle_payload())
    wrong_claim = wrong_identity["claim_to_evidence_index"]["setup_improvement"]
    wrong_claim["comparison_identity_hashes"]["analysis_view"] = hash_ref("f")
    wrong_identity["bundle_hash"] = self_hash(wrong_identity, "bundle_hash")
    with pytest.raises(ValidationError, match="comparison identity"):
        ReportBundle.model_validate(wrong_identity)
