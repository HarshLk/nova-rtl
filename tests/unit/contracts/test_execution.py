from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.execution import (
    PreparedCommand,
    RawToolResult,
    StageResult,
    ToolJob,
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
    artifact_id: str,
    uri: str,
    digit: str,
    producer: str | None = None,
    media_type: str = "text/plain",
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": media_type,
        "size_bytes": 10,
        "created_at": "2026-08-21T06:30:00Z",
        "producer_stage_result_id": producer,
        "classification": "INTERNAL",
    }


def fingerprint_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_id": "opensta",
        "executable": "/opt/nova/bin/sta",
        "version": "OpenSTA 2.6.0",
        "version_args": ["-version"],
        "executable_sha256": hash_ref("1"),
        "build_hash": hash_ref("2"),
        "adapter_version": "opensta-adapter-v1",
        "container_digest": None,
    }


def input_hash_payload() -> dict[str, object]:
    return {
        "rtl_snapshot": hash_ref("1"),
        "design_contract": hash_ref("2"),
        "constraints": hash_ref("3"),
        "constraint_binding": hash_ref("4"),
        "analysis_view": hash_ref("5"),
        "power_activity": None,
        "platform_lock": hash_ref("6"),
        "tool_recipe": hash_ref("7"),
        "formal_model": None,
        "parent_stage_result": None,
        "extensions": {},
    }


def resource_limits_payload() -> dict[str, object]:
    return {
        "cpu_cores": 2,
        "memory_bytes": 2_147_483_648,
        "wall_time_ms": 120_000,
        "max_output_bytes": 10_485_760,
    }


def tool_job_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_job_id": "tool_job_opensta_001",
        "stage_result_id": "stage_opensta_001",
        "run_id": "run_001",
        "candidate_id": "baseline",
        "stage": "OPENSTA_FULL",
        "analysis_view_id": "func_setup_slow",
        "design_contract_hash": hash_ref("2"),
        "input_artifact_refs": [
            artifact_payload(
                "artifact_input_netlist",
                "artifact://inputs/netlist/baseline.v",
                "3",
                media_type="text/x-verilog",
            )
        ],
        "input_hashes": input_hash_payload(),
        "resource_limits": resource_limits_payload(),
        "deadline": "2026-08-21T06:35:00Z",
        "artifact_namespace": "artifact://runs/run_001/stages/stage_opensta_001/",
        "requested_at": "2026-08-21T06:30:00Z",
    }


def prepared_command_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "tool_job_id": "tool_job_opensta_001",
        "stage_result_id": "stage_opensta_001",
        "tool_fingerprint": fingerprint_payload(),
        "argv": ["/opt/nova/bin/sta", "-no_init", "run_sta.tcl"],
        "working_directory": "/tmp/nova/run_001/stage_opensta_001",
        "environment": {"PATH": "/opt/nova/bin:/usr/bin", "LC_ALL": "C"},
        "resource_limits": resource_limits_payload(),
        "deadline": "2026-08-21T06:35:00Z",
        "staged_input_artifact_refs": tool_job_payload()["input_artifact_refs"],
        "recipe_artifact_refs": [
            artifact_payload(
                "artifact_sta_recipe",
                "artifact://runs/run_001/stages/stage_opensta_001/recipe.tcl",
                "4",
                producer="stage_opensta_001",
            )
        ],
        "preparation_hash": hash_ref("0"),
    }
    payload["preparation_hash"] = canonical_payload_hash(payload, "preparation_hash")
    return payload


def raw_result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_job_id": "tool_job_opensta_001",
        "stage_result_id": "stage_opensta_001",
        "tool_fingerprint": fingerprint_payload(),
        "prepared_command_artifact": artifact_payload(
            "artifact_prepared_command",
            "artifact://runs/run_001/stages/stage_opensta_001/prepared-command.json",
            "5",
            producer="stage_opensta_001",
            media_type="application/json",
        ),
        "prepared_command_hash": hash_ref("5"),
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "started_at": "2026-08-21T06:30:01Z",
        "ended_at": "2026-08-21T06:30:03Z",
        "resource_usage": {
            "cpu_time_ms": 1500,
            "wall_time_ms": 2000,
            "peak_rss_bytes": 1048576,
        },
        "raw_artifacts": [
            artifact_payload(
                "artifact_opensta_stdout",
                "artifact://runs/run_001/stages/stage_opensta_001/stdout.log",
                "6",
                producer="stage_opensta_001",
            ),
            artifact_payload(
                "artifact_opensta_stderr",
                "artifact://runs/run_001/stages/stage_opensta_001/stderr.log",
                "7",
                producer="stage_opensta_001",
            ),
            artifact_payload(
                "artifact_opensta_report",
                "artifact://runs/run_001/stages/stage_opensta_001/report_checks.rpt",
                "8",
                producer="stage_opensta_001",
            ),
            artifact_payload(
                "artifact_opensta_argv",
                "artifact://runs/run_001/stages/stage_opensta_001/invoked-argv.json",
                "9",
                producer="stage_opensta_001",
                media_type="application/json",
            ),
        ],
    }


def setup_metric_payload() -> dict[str, object]:
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
        "analysis_view_id": "func_setup_slow",
        "setup_wns_ns": -0.18,
        "setup_tns_ns": -12.4,
        "hold_wns_ns": None,
        "hold_tns_ns": None,
        "failing_endpoints": 83,
        "critical_path_delay_ns": 2.41,
        "estimated_fmax_mhz": 414.94,
        "mapped_area_um2": 218430.2,
        "physical_area_um2": None,
        "cell_count": 50184,
        "register_count": 18342,
        "buffer_count": None,
        "power_total_uw": None,
        "wirelength_um": None,
        "congestion_overflow": None,
        "runtime_ms": 2000,
        "missing_metric_reasons": {name: "not produced by this stage" for name in unavailable},
    }


def stage_result_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "stage_result_id": "stage_opensta_001",
        "run_id": "run_001",
        "candidate_id": "baseline",
        "stage": "OPENSTA_FULL",
        "analysis_view_id": "func_setup_slow",
        "status": "PASS",
        "tool_fingerprint": fingerprint_payload(),
        "input_hashes": input_hash_payload(),
        "metrics": setup_metric_payload(),
        "diagnostics": [],
        "raw_artifacts": raw_result_payload()["raw_artifacts"],
        "started_at": "2026-08-21T06:30:01Z",
        "ended_at": "2026-08-21T06:30:03Z",
    }


def test_tool_job_binds_view_specific_execution_to_complete_input_identity() -> None:
    job = ToolJob.model_validate(tool_job_payload())
    assert job.stage_result_id == "stage_opensta_001"

    missing_view = tool_job_payload()
    missing_view["analysis_view_id"] = None
    with pytest.raises(ValidationError, match="analysis_view_id"):
        ToolJob.model_validate(missing_view)

    missing_hash = tool_job_payload()
    hashes = missing_hash["input_hashes"]
    assert isinstance(hashes, dict)
    hashes["constraint_binding"] = None
    with pytest.raises(ValidationError, match="constraint_binding"):
        ToolJob.model_validate(missing_hash)


def test_tool_job_accepts_the_m0_locked_openroad_physical_stage() -> None:
    payload = tool_job_payload()
    payload["stage"] = "OPENROAD_PHYSICAL"

    job = ToolJob.model_validate(payload)
    assert job.stage == "OPENROAD_PHYSICAL"


def test_execution_boundary_accepts_immutable_outputs_from_prior_stages() -> None:
    job_payload = tool_job_payload()
    input_artifacts = job_payload["input_artifact_refs"]
    assert isinstance(input_artifacts, list)
    input_artifacts[0]["producer_stage_result_id"] = "stage_yosys_001"

    job = ToolJob.model_validate(job_payload)
    assert job.input_artifact_refs[0].producer_stage_result_id == "stage_yosys_001"

    command_payload = prepared_command_payload()
    staged_inputs = command_payload["staged_input_artifact_refs"]
    assert isinstance(staged_inputs, list)
    staged_inputs[0]["producer_stage_result_id"] = "stage_yosys_001"
    command_payload["preparation_hash"] = canonical_payload_hash(
        command_payload, "preparation_hash"
    )

    command = PreparedCommand.model_validate(command_payload)
    assert command.staged_input_artifact_refs[0].producer_stage_result_id == "stage_yosys_001"


def test_prepared_command_is_argv_only_secret_free_and_self_hashed() -> None:
    command = PreparedCommand.model_validate(prepared_command_payload())
    assert command.argv[0] == command.tool_fingerprint.executable

    shell_string = prepared_command_payload()
    shell_string["argv"] = ["/opt/nova/bin/sta -no_init run_sta.tcl"]
    shell_string["preparation_hash"] = canonical_payload_hash(shell_string, "preparation_hash")
    with pytest.raises(ValidationError, match=r"argv\[0\]"):
        PreparedCommand.model_validate(shell_string)

    secret = prepared_command_payload()
    secret["environment"] = {"OPENAI_API_KEY": "must-not-persist"}
    secret["preparation_hash"] = canonical_payload_hash(secret, "preparation_hash")
    with pytest.raises(ValidationError, match="secret"):
        PreparedCommand.model_validate(secret)


def test_raw_tool_result_records_execution_without_claiming_analysis_pass() -> None:
    raw = RawToolResult.model_validate(raw_result_payload())
    assert raw.exit_code == 0
    assert not hasattr(raw, "status")

    reversed_time = raw_result_payload()
    reversed_time["ended_at"] = "2026-08-21T06:30:00Z"
    with pytest.raises(ValidationError, match="ended_at"):
        RawToolResult.model_validate(reversed_time)

    missing_invocation = raw_result_payload()
    artifacts = missing_invocation["raw_artifacts"]
    assert isinstance(artifacts, list)
    artifacts.pop()
    with pytest.raises(ValidationError, match="stdout, stderr and invocation"):
        RawToolResult.model_validate(missing_invocation)


def test_stage_result_pass_requires_view_metrics_hashes_and_matching_artifacts() -> None:
    result = StageResult.model_validate(stage_result_payload())
    assert result.schema_version == 2
    assert result.metrics.analysis_view_id == result.analysis_view_id

    missing_metrics = stage_result_payload()
    metrics = missing_metrics["metrics"]
    assert isinstance(metrics, dict)
    metrics["setup_wns_ns"] = None
    metrics["setup_tns_ns"] = None
    reasons = metrics["missing_metric_reasons"]
    assert isinstance(reasons, dict)
    reasons["setup_wns_ns"] = "parser did not find setup WNS"
    reasons["setup_tns_ns"] = "parser did not find setup TNS"
    with pytest.raises(ValidationError, match="completed timing stage"):
        StageResult.model_validate(missing_metrics)

    wrong_producer = stage_result_payload()
    artifacts = wrong_producer["raw_artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["producer_stage_result_id"] = "stage_other_001"
    with pytest.raises(ValidationError, match="producer"):
        StageResult.model_validate(wrong_producer)


def test_nonpass_stage_result_requires_a_diagnostic() -> None:
    payload = deepcopy(stage_result_payload())
    payload["status"] = "FAIL"

    with pytest.raises(ValidationError, match="diagnostic"):
        StageResult.model_validate(payload)


def m3_metric_payload() -> dict[str, object]:
    unavailable = {
        "setup_wns_ns",
        "setup_tns_ns",
        "hold_wns_ns",
        "hold_tns_ns",
        "failing_endpoints",
        "critical_path_delay_ns",
        "estimated_fmax_mhz",
        "mapped_area_um2",
        "physical_area_um2",
        "cell_count",
        "register_count",
        "buffer_count",
        "power_total_uw",
        "wirelength_um",
        "congestion_overflow",
    }
    return {
        "analysis_view_id": None,
        **{name: None for name in unavailable},
        "runtime_ms": 12,
        "missing_metric_reasons": {
            name: "not produced by this deterministic stage" for name in unavailable
        },
    }


def m3_stage_result_payload(stage: str) -> dict[str, object]:
    payload = stage_result_payload()
    payload["stage"] = stage
    payload["analysis_view_id"] = None
    payload["metrics"] = m3_metric_payload()
    hashes = payload["input_hashes"]
    assert isinstance(hashes, dict)
    hashes["analysis_view"] = None
    if stage == "EVIDENCE_GRAPH":
        hashes["extensions"] = {
            "analysis_view_set": hash_ref("8"),
            "cdc_inventory": hash_ref("9"),
            "clock_inventory": hash_ref("a"),
            "critical_path_records": hash_ref("b"),
            "protection_policy": hash_ref("c"),
            "synthesis_structure": hash_ref("d"),
        }
    else:
        hashes["parent_stage_result"] = hash_ref("8")
        hashes["extensions"] = {
            "evidence_graph": hash_ref("9"),
            "protection_policy": hash_ref("a"),
            "transform_registry": hash_ref("b"),
        }
    return payload


@pytest.mark.parametrize("stage", ["EVIDENCE_GRAPH", "OPPORTUNITY_FORMATION"])
def test_m3_stage_results_require_complete_stage_specific_identity(stage: str) -> None:
    payload = m3_stage_result_payload(stage)

    result = StageResult.model_validate(payload)

    assert result.stage == stage
    assert result.analysis_view_id is None

    missing_identity = m3_stage_result_payload(stage)
    hashes = missing_identity["input_hashes"]
    assert isinstance(hashes, dict)
    extensions = hashes["extensions"]
    assert isinstance(extensions, dict)
    extensions.pop(next(iter(extensions)))
    with pytest.raises(ValidationError, match="stage input extension identity is missing"):
        StageResult.model_validate(missing_identity)


def test_m3_extension_identities_cannot_leak_into_prior_stage_contracts() -> None:
    payload = stage_result_payload()
    hashes = payload["input_hashes"]
    assert isinstance(hashes, dict)
    hashes["extensions"] = {"evidence_graph": hash_ref("8")}

    with pytest.raises(ValidationError, match="not valid for OPENSTA_FULL"):
        StageResult.model_validate(payload)
