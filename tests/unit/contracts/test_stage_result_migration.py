from __future__ import annotations

import operator
from collections.abc import Callable
from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.base import ArtifactRef, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.migrations import (
    LegacyDiagnosticRule,
    LegacyMigrationContext,
    LegacyStageExpectation,
    StageResultV1ToV2,
)
from nova_rtl.contracts.platform import ToolFingerprint


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def artifact_payload(artifact_id: str, filename: str, digit: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": f"artifact://runs/run_001/stages/stage_opensta_001/{filename}",
        "sha256": hash_ref(digit),
        "media_type": "text/plain",
        "size_bytes": 10,
        "created_at": "2026-08-21T06:30:00Z",
        "producer_stage_result_id": "stage_opensta_001",
        "classification": "INTERNAL",
    }


def artifact_payloads() -> list[dict[str, object]]:
    return [
        artifact_payload("artifact_opensta_stdout", "stdout.log", "1"),
        artifact_payload("artifact_opensta_stderr", "stderr.log", "2"),
        artifact_payload("artifact_opensta_report", "report_checks.rpt", "3"),
        artifact_payload("artifact_opensta_argv", "invoked-argv.json", "4"),
    ]


def fingerprint_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_id": "opensta",
        "executable": "/opt/nova/bin/sta",
        "version": "OpenSTA 2.6.0",
        "version_args": ["-version"],
        "executable_sha256": hash_ref("5"),
        "build_hash": hash_ref("6"),
        "adapter_version": "opensta-adapter-v1",
        "container_digest": None,
    }


def legacy_stage_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage_result_id": "stage_opensta_001",
        "run_id": "run_001",
        "candidate_id": "baseline",
        "stage": "OPENSTA_FULL",
        "analysis_view_id": "func_setup_slow",
        "status": "PASS",
        "tool_name": "opensta",
        "tool_version": "OpenSTA 2.6.0",
        "input_hashes": {
            "rtl": hash_ref("1"),
            "contract": hash_ref("2"),
            "sdc": hash_ref("3"),
            "binding": hash_ref("4"),
            "view": hash_ref("5"),
            "platform": hash_ref("6"),
            "recipe": hash_ref("7"),
        },
        "metrics": {
            "setup_wns_ns": -0.18,
            "setup_tns_ns": -12.4,
            "failing_endpoints": 83,
            "critical_path_delay_ns": 2.41,
            "estimated_fmax_mhz": 414.94,
            "mapped_area_um2": 218430.2,
            "cell_count": 50184,
            "register_count": 18342,
            "runtime_ms": 2000,
        },
        "diagnostics": [],
        "raw_artifacts": [item["uri"] for item in artifact_payloads()],
        "started_at": "2026-08-21T06:30:01Z",
        "ended_at": "2026-08-21T06:30:03Z",
    }


def migration_context(
    legacy: dict[str, object] | None = None,
) -> LegacyMigrationContext:
    source = legacy if legacy is not None else legacy_stage_result()
    artifacts = tuple(ArtifactRef.model_validate(item) for item in artifact_payloads())
    unavailable_reasons = {
        "hold_wns_ns": "not produced by the registered setup-only legacy stage",
        "hold_tns_ns": "not produced by the registered setup-only legacy stage",
        "physical_area_um2": "not produced by the registered pre-layout legacy stage",
        "buffer_count": "not produced by the registered legacy report",
        "power_total_uw": "not produced without registered power activity",
        "wirelength_um": "not produced by the registered pre-layout legacy stage",
        "congestion_overflow": "not produced by the registered pre-layout legacy stage",
    }
    return LegacyMigrationContext.model_validate(
        {
            "source_object_hash": canonical_sha256(source),
            "artifact_index": {item.uri: [item.model_dump(mode="json")] for item in artifacts},
            "tool_fingerprints": [fingerprint_payload()],
            "stage_expectations": {
                "OPENSTA_FULL": {
                    "required_input_fields": [
                        "rtl_snapshot",
                        "design_contract",
                        "constraints",
                        "constraint_binding",
                        "analysis_view",
                        "platform_lock",
                        "tool_recipe",
                    ],
                    "required_metric_fields": [
                        "setup_wns_ns",
                        "setup_tns_ns",
                        "failing_endpoints",
                        "critical_path_delay_ns",
                        "estimated_fmax_mhz",
                        "mapped_area_um2",
                        "cell_count",
                        "register_count",
                        "runtime_ms",
                    ],
                    "unavailable_metric_reasons": unavailable_reasons,
                    "required_artifact_ids": ["artifact_opensta_report"],
                    "allowed_statuses": ["PASS", "FAIL", "INCONCLUSIVE"],
                }
            },
            "diagnostic_code_registry": {
                "timing_violation": {
                    "code": "TIMING_VIOLATION",
                    "severity": "ERROR",
                    "evidence_ids": ["path_0042"],
                }
            },
            "legacy_field_aliases": {
                "rtl": "rtl_snapshot",
                "contract": "design_contract",
                "sdc": "constraints",
                "binding": "constraint_binding",
                "view": "analysis_view",
                "platform": "platform_lock",
                "recipe": "tool_recipe",
            },
            "authorized_evidence_ids": ["path_0042"],
            "expected_input_hashes": {
                "rtl_snapshot": hash_ref("1"),
                "design_contract": hash_ref("2"),
                "constraints": hash_ref("3"),
                "constraint_binding": hash_ref("4"),
                "analysis_view": hash_ref("5"),
                "platform_lock": hash_ref("6"),
                "tool_recipe": hash_ref("7"),
            },
            "migration_adapter_version": "stage-result-v1-to-v2-v1",
            "migration_adapter_hash": hash_ref("9"),
        }
    )


def test_stage_result_v1_migrates_once_to_canonical_v2() -> None:
    legacy = legacy_stage_result()
    migrated = StageResultV1ToV2.migrate(legacy, context=migration_context(legacy))

    assert migrated.schema_version == 2
    assert isinstance(migrated.tool_fingerprint, ToolFingerprint)
    assert all(isinstance(item, ArtifactRef) for item in migrated.raw_artifacts)
    assert migrated.input_hashes.extensions == {
        "legacy_source_object": canonical_sha256(legacy),
        "legacy_migration_adapter": hash_ref("9"),
    }
    assert set(migrated.metrics.missing_metric_reasons) == {
        "hold_wns_ns",
        "hold_tns_ns",
        "physical_area_um2",
        "buffer_count",
        "power_total_uw",
        "wirelength_um",
        "congestion_overflow",
    }

    audit = StageResultV1ToV2.audit_payload(
        migrated, legacy=legacy, context=migration_context(legacy)
    )
    assert audit.source_schema_version == 1
    assert audit.source_object_hash == canonical_sha256(legacy)
    assert audit.migrated_stage_result_hash == canonical_sha256(migrated)
    assert type(migrated).model_validate_json(canonical_json_bytes(migrated)) == migrated


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["input_hashes"].update({"mystery": hash_ref("8")}), "alias"),
        (lambda value: value["metrics"].pop("setup_wns_ns"), "required metric"),
        (lambda value: value["metrics"].update({"predicted_slack": 0.5}), "metric"),
        (lambda value: value["raw_artifacts"].append("artifact://missing/report.rpt"), "artifact"),
        (lambda value: value.update({"status": "SUCCESS"}), "status"),
        (lambda value: value.update({"agent_score": 0.99}), "field"),
    ],
)
def test_migration_rejects_untranslatable_legacy_shapes(
    mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    legacy = legacy_stage_result()
    mutation(legacy)

    with pytest.raises((ValueError, ValidationError), match=message):
        StageResultV1ToV2.migrate(legacy, context=migration_context(legacy))


def test_migration_rejects_ambiguous_tool_and_artifact_identity() -> None:
    legacy = legacy_stage_result()
    context_data = migration_context(legacy).model_dump(mode="json")
    context_data["tool_fingerprints"].append(fingerprint_payload())
    ambiguous_tools = LegacyMigrationContext.model_validate(context_data)
    with pytest.raises(ValueError, match="exactly one tool fingerprint"):
        StageResultV1ToV2.migrate(legacy, context=ambiguous_tools)

    context_data = migration_context(legacy).model_dump(mode="json")
    uri = legacy["raw_artifacts"][0]
    context_data["artifact_index"][uri].append(context_data["artifact_index"][uri][0])
    ambiguous_artifacts = LegacyMigrationContext.model_validate(context_data)
    with pytest.raises(ValueError, match="exactly one artifact"):
        StageResultV1ToV2.migrate(legacy, context=ambiguous_artifacts)


def test_migration_validates_diagnostic_registry_severity_and_evidence() -> None:
    legacy = legacy_stage_result()
    legacy["status"] = "FAIL"
    legacy["diagnostics"] = [
        {
            "code": "timing_violation",
            "severity": "ERROR",
            "message": "setup slack is negative",
            "evidence_refs": ["path_0042"],
        }
    ]
    context = migration_context(legacy)
    migrated = StageResultV1ToV2.migrate(legacy, context=context)
    assert migrated.diagnostics[0].code == "TIMING_VIOLATION"

    wrong_severity = deepcopy(legacy)
    wrong_severity["diagnostics"][0]["severity"] = "WARNING"
    with pytest.raises(ValueError, match="severity"):
        StageResultV1ToV2.migrate(
            wrong_severity, context=migration_context(wrong_severity)
        )

    unauthorized = deepcopy(legacy)
    unauthorized["diagnostics"][0]["evidence_refs"] = ["path_untrusted"]
    with pytest.raises(ValueError, match="evidence"):
        StageResultV1ToV2.migrate(
            unauthorized, context=migration_context(unauthorized)
        )

    string_legacy = deepcopy(legacy)
    string_legacy["diagnostics"] = ["timing_violation"]
    migrated_string = StageResultV1ToV2.migrate(
        string_legacy, context=migration_context(string_legacy)
    )
    assert migrated_string.diagnostics[0].evidence_refs == ("path_0042",)


def test_nonpass_migration_does_not_require_a_pass_only_report() -> None:
    legacy = legacy_stage_result()
    legacy["status"] = "FAIL"
    legacy["diagnostics"] = [
        {
            "code": "timing_violation",
            "severity": "ERROR",
            "message": "legacy analysis failed before report completion",
            "evidence_refs": ["path_0042"],
        }
    ]
    legacy["raw_artifacts"] = [
        uri for uri in legacy["raw_artifacts"] if "report_checks.rpt" not in uri
    ]

    migrated = StageResultV1ToV2.migrate(legacy, context=migration_context(legacy))
    assert migrated.status == "FAIL"
    assert "artifact_opensta_report" not in {
        item.artifact_id for item in migrated.raw_artifacts
    }


def test_migration_audit_rejects_a_result_from_another_source_context() -> None:
    legacy = legacy_stage_result()
    context = migration_context(legacy)
    migrated = StageResultV1ToV2.migrate(legacy, context=context)
    payload = migrated.model_dump(mode="json")
    payload["metrics"]["setup_wns_ns"] = -0.17
    unrelated = type(migrated).model_validate(payload)

    with pytest.raises(ValueError, match="migration context"):
        StageResultV1ToV2.audit_payload(
            unrelated, legacy=legacy, context=context
        )


def test_migration_context_authorities_are_deeply_immutable() -> None:
    context = migration_context()
    mutations = (
        lambda: operator.setitem(context.legacy_field_aliases, "new", "rtl_snapshot"),
        lambda: operator.setitem(context.artifact_index, "artifact://new", ()),
        lambda: operator.setitem(
            context.stage_expectations,
            "FAST_SYNTH", context.stage_expectations["OPENSTA_FULL"]
        ),
        lambda: operator.setitem(
            context.diagnostic_code_registry,
            "new", context.diagnostic_code_registry["timing_violation"]
        ),
        lambda: operator.setitem(
            context.expected_input_hashes,
            "platform_lock", hash_ref("f")
        ),
        lambda: operator.setitem(
            context.stage_expectations["OPENSTA_FULL"].unavailable_metric_reasons,
            "hold_wns_ns", "mutated after validation"
        ),
        lambda: dict.__setitem__(
            context.expected_input_hashes, "platform_lock", hash_ref("f")
        ),
    )
    for mutation in mutations:
        with pytest.raises(TypeError):
            mutation()


def test_migration_rejects_input_identity_not_pinned_by_context() -> None:
    legacy = legacy_stage_result()
    legacy["input_hashes"]["platform"] = hash_ref("f")

    with pytest.raises(ValueError, match="expected input identity.*platform_lock"):
        StageResultV1ToV2.migrate(legacy, context=migration_context(legacy))


def test_migration_rejects_source_hash_or_artifact_metadata_mismatch() -> None:
    legacy = legacy_stage_result()
    stale_context = migration_context(legacy)
    changed = deepcopy(legacy)
    changed["metrics"]["setup_wns_ns"] = -0.17
    with pytest.raises(ValueError, match="source-object hash"):
        StageResultV1ToV2.migrate(changed, context=stale_context)

    described = deepcopy(legacy)
    first_uri = described["raw_artifacts"][0]
    described["raw_artifacts"][0] = {
        "uri": first_uri,
        "sha256": hash_ref("f"),
        "size_bytes": 10,
    }
    with pytest.raises(ValueError, match="metadata"):
        StageResultV1ToV2.migrate(described, context=migration_context(described))


def test_legacy_context_types_are_strict_and_immutable() -> None:
    rule = LegacyDiagnosticRule.model_validate(
        {
            "code": "TIMING_VIOLATION",
            "severity": "ERROR",
            "evidence_ids": ["path_0042"],
        }
    )
    expectation = LegacyStageExpectation.model_validate(
        migration_context().stage_expectations["OPENSTA_FULL"].model_dump(mode="json")
    )
    assert rule.severity == "ERROR"
    assert "setup_wns_ns" in expectation.required_metric_fields

    with pytest.raises(ValidationError, match="extra_forbidden"):
        LegacyDiagnosticRule.model_validate(
            {
                "code": "TIMING_VIOLATION",
                "severity": "ERROR",
                "evidence_ids": ["path_0042"],
                "guess": True,
            }
        )
