from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.base import (
    ArtifactRef,
    Diagnostic,
    EvidenceRef,
    MetricSet,
    StageInputHashes,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def artifact_payload() -> dict[str, object]:
    return {
        "artifact_id": "artifact_source_rtl",
        "uri": "artifact://inputs/rtl/source.sv",
        "sha256": hash_ref("1"),
        "media_type": "text/x-systemverilog",
        "size_bytes": 42,
        "created_at": "2026-08-21T06:30:00Z",
        "producer_stage_result_id": None,
        "classification": "RESTRICTED_RTL",
    }


def test_artifact_ref_serializes_a_canonical_utc_timestamp() -> None:
    artifact = ArtifactRef.model_validate(artifact_payload())

    assert artifact.created_at == datetime(2026, 8, 21, 6, 30, tzinfo=UTC)
    assert artifact.model_dump(mode="json")["created_at"] == "2026-08-21T06:30:00Z"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("uri", "https://example.test/source.sv", "string_pattern_mismatch"),
        ("size_bytes", -1, "greater_than_equal"),
        ("size_bytes", "42", "int_type"),
        ("media_type", "text plain", "media_type"),
        ("created_at", "2026-08-21T06:30:00+00:00", "trailing Z"),
        (
            "created_at",
            datetime(2026, 8, 21, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            "UTC",
        ),
    ],
)
def test_artifact_ref_rejects_noncanonical_identity_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = artifact_payload()
    payload[field] = value

    with pytest.raises(ValidationError, match=message):
        ArtifactRef.model_validate(payload)


def test_evidence_ref_accepts_an_rfc6901_subobject_pointer() -> None:
    evidence = EvidenceRef(
        evidence_id="evidence_path_001",
        kind="PATH",
        artifact_id="artifact_timing_report",
        json_pointer="/paths/0/~0name~1escaped",
        snapshot_hash=hash_ref("2"),
    )

    assert evidence.json_pointer == "/paths/0/~0name~1escaped"


@pytest.mark.parametrize("pointer", ["paths/0", "/paths/~2bad"])
def test_evidence_ref_rejects_a_non_rfc6901_pointer(pointer: str) -> None:
    with pytest.raises(ValidationError, match="JSON pointer"):
        EvidenceRef(
            evidence_id="evidence_path_001",
            kind="PATH",
            artifact_id="artifact_timing_report",
            json_pointer=pointer,
            snapshot_hash=hash_ref("2"),
        )


def test_diagnostic_requires_evidence_unless_it_is_infrastructure_scoped() -> None:
    with pytest.raises(ValidationError, match="evidence_refs"):
        Diagnostic(
            code="TIMING_CONTRACT_MISMATCH",
            severity="ERROR",
            message="The candidate used a different timing contract.",
            evidence_refs=(),
        )

    diagnostic = Diagnostic(
        code="INFRASTRUCTURE_TOOL_TIMEOUT",
        severity="FATAL",
        message="The tool process exceeded its wall-time limit.",
        evidence_refs=(),
    )
    assert diagnostic.evidence_refs == ()


def stage_input_payload() -> dict[str, object]:
    return {
        "rtl_snapshot": hash_ref("1"),
        "design_contract": hash_ref("2"),
        "constraints": hash_ref("3"),
        "constraint_binding": None,
        "analysis_view": None,
        "power_activity": None,
        "platform_lock": hash_ref("4"),
        "tool_recipe": hash_ref("5"),
        "formal_model": None,
        "parent_stage_result": None,
        "extensions": {},
    }


def test_stage_input_hashes_reject_unregistered_extension_identity() -> None:
    payload = stage_input_payload()
    payload["extensions"] = {"arbitrary_cache_key": hash_ref("6")}

    with pytest.raises(ValidationError, match="unregistered stage input extension"):
        StageInputHashes.model_validate(payload)


def metric_payload() -> dict[str, object]:
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
        "runtime_ms": 9000,
        "missing_metric_reasons": {name: "not produced by this stage" for name in unavailable},
    }


def test_metric_set_requires_reasons_for_exactly_the_unavailable_metrics() -> None:
    metrics = MetricSet.model_validate(metric_payload())
    assert metrics.setup_wns_ns == -0.18

    missing_reason = metric_payload()
    del missing_reason["missing_metric_reasons"]["power_total_uw"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="exactly cover unavailable metrics"):
        MetricSet.model_validate(missing_reason)

    extra_reason = metric_payload()
    extra_reason["missing_metric_reasons"]["setup_wns_ns"] = "incorrect"  # type: ignore[index]
    with pytest.raises(ValidationError, match="exactly cover unavailable metrics"):
        MetricSet.model_validate(extra_reason)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_ms", -1),
        ("critical_path_delay_ns", 0.0),
        ("mapped_area_um2", float("nan")),
        ("mapped_area_um2", "218430.2"),
    ],
)
def test_metric_set_rejects_impossible_numeric_values(field: str, value: object) -> None:
    payload = metric_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        MetricSet.model_validate(payload)
