"""Explicit, fail-closed adapters for legacy persisted contract ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import Field, field_serializer, field_validator, model_validator

from nova_rtl.contracts.base import (
    METRIC_VALUE_FIELDS,
    ArtifactRef,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    EntityId,
    HashRef,
    NonEmptyString,
    StageInputHashes,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.execution import Stage, StageResult, StageStatus
from nova_rtl.contracts.platform import ToolFingerprint

INPUT_HASH_FIELDS = frozenset(
    {
        "rtl_snapshot",
        "design_contract",
        "constraints",
        "constraint_binding",
        "analysis_view",
        "power_activity",
        "platform_lock",
        "tool_recipe",
        "formal_model",
        "parent_stage_result",
    }
)
LEGACY_STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "stage_result_id",
        "run_id",
        "candidate_id",
        "stage",
        "analysis_view_id",
        "status",
        "tool_name",
        "tool_version",
        "input_hashes",
        "metrics",
        "diagnostics",
        "raw_artifacts",
        "started_at",
        "ended_at",
    }
)


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class LegacyDiagnosticRule(StrictContract):
    """Pinned mapping from one legacy diagnostic token to canonical semantics."""

    code: DiagnosticCode
    severity: DiagnosticSeverity
    evidence_ids: tuple[EntityId, ...]

    @model_validator(mode="after")
    def evidence_is_canonical(self) -> Self:
        _require_unique(self.evidence_ids, "diagnostic rule evidence IDs")
        if not self.evidence_ids and not self.code.startswith("INFRASTRUCTURE_"):
            raise ValueError(
                "legacy diagnostic rules require evidence unless they are "
                "infrastructure diagnostics"
            )
        return self


class LegacyStageExpectation(StrictContract):
    """Pinned facts defining what one legacy stage could and had to produce."""

    required_input_fields: tuple[str, ...] = Field(min_length=1)
    required_metric_fields: tuple[str, ...] = Field(min_length=1)
    unavailable_metric_reasons: dict[str, NonEmptyString]
    required_artifact_ids: tuple[EntityId, ...] = Field(min_length=1)
    allowed_statuses: tuple[StageStatus, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def expectation_is_complete_and_unambiguous(self) -> Self:
        for values, label in (
            (self.required_input_fields, "required input fields"),
            (self.required_metric_fields, "required metric fields"),
            (self.required_artifact_ids, "required artifact IDs"),
            (self.allowed_statuses, "allowed statuses"),
        ):
            _require_unique(values, label)
        unknown_inputs = set(self.required_input_fields) - INPUT_HASH_FIELDS
        if unknown_inputs:
            raise ValueError(f"unknown required input field: {sorted(unknown_inputs)}")
        valid_metric_fields = METRIC_VALUE_FIELDS | {"runtime_ms"}
        unknown_required = set(self.required_metric_fields) - valid_metric_fields
        unknown_unavailable = set(self.unavailable_metric_reasons) - METRIC_VALUE_FIELDS
        if unknown_required or unknown_unavailable:
            raise ValueError("legacy stage expectation contains an unknown metric field")
        overlap = set(self.required_metric_fields) & set(self.unavailable_metric_reasons)
        if overlap:
            raise ValueError("required and unavailable metric fields must be disjoint")
        covered = (
            set(self.required_metric_fields) | set(self.unavailable_metric_reasons)
        ) - {"runtime_ms"}
        if covered != METRIC_VALUE_FIELDS:
            raise ValueError("legacy stage expectation must classify every metric field")
        if "runtime_ms" not in self.required_metric_fields:
            raise ValueError("legacy stage expectation must require runtime_ms")
        object.__setattr__(
            self,
            "unavailable_metric_reasons",
            MappingProxyType(dict(self.unavailable_metric_reasons)),
        )
        return self

    @field_serializer("unavailable_metric_reasons")
    def serialize_unavailable_metric_reasons(
        self, value: Mapping[str, NonEmptyString]
    ) -> dict[str, NonEmptyString]:
        return dict(value)


class LegacyMigrationContext(StrictContract):
    """Immutable resolver inputs required for one legacy object migration."""

    source_object_hash: HashRef
    artifact_index: dict[str, tuple[ArtifactRef, ...]]
    tool_fingerprints: tuple[ToolFingerprint, ...] = Field(min_length=1)
    stage_expectations: dict[Stage, LegacyStageExpectation]
    diagnostic_code_registry: dict[NonEmptyString, LegacyDiagnosticRule]
    legacy_field_aliases: dict[NonEmptyString, str]
    authorized_evidence_ids: tuple[EntityId, ...]
    expected_input_hashes: dict[str, HashRef]
    migration_adapter_version: NonEmptyString
    migration_adapter_hash: HashRef

    @field_validator("legacy_field_aliases")
    @classmethod
    def aliases_target_named_input_fields(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value.values()) - INPUT_HASH_FIELDS
        if unknown:
            raise ValueError(f"legacy alias targets unknown input fields: {sorted(unknown)}")
        return value

    @model_validator(mode="after")
    def indexes_are_coherent(self) -> Self:
        _require_unique(self.authorized_evidence_ids, "authorized evidence IDs")
        for uri, refs in self.artifact_index.items():
            if not refs:
                raise ValueError("artifact index entries cannot be empty")
            if any(item.uri != uri for item in refs):
                raise ValueError("artifact index key must equal each ArtifactRef URI")
        unknown_expected_inputs = set(self.expected_input_hashes) - INPUT_HASH_FIELDS
        if unknown_expected_inputs:
            raise ValueError(
                "expected input hashes contain unknown fields: "
                f"{sorted(unknown_expected_inputs)}"
            )
        unpinned_aliases = set(self.legacy_field_aliases.values()) - set(
            self.expected_input_hashes
        )
        if unpinned_aliases:
            raise ValueError(
                "legacy input aliases lack expected input identities: "
                f"{sorted(unpinned_aliases)}"
            )
        required_inputs = {
            field
            for expectation in self.stage_expectations.values()
            for field in expectation.required_input_fields
        }
        unpinned_required = required_inputs - set(self.expected_input_hashes)
        if unpinned_required:
            raise ValueError(
                "stage expectations lack expected input identities: "
                f"{sorted(unpinned_required)}"
            )
        authorized = set(self.authorized_evidence_ids)
        for token, rule in self.diagnostic_code_registry.items():
            if not set(rule.evidence_ids).issubset(authorized):
                raise ValueError(
                    f"diagnostic rule evidence is not authorized for token: {token}"
                )
        for field_name in (
            "artifact_index",
            "stage_expectations",
            "diagnostic_code_registry",
            "legacy_field_aliases",
            "expected_input_hashes",
        ):
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(getattr(self, field_name))),
            )
        return self

    @field_serializer(
        "artifact_index",
        "stage_expectations",
        "diagnostic_code_registry",
        "legacy_field_aliases",
        "expected_input_hashes",
    )
    def serialize_resolver_mapping(
        self, value: Mapping[Any, Any]
    ) -> dict[Any, Any]:
        return dict(value)


class _MigrationAuditPayload(StrictContract):
    schema_version: Literal[1] = 1
    source_schema_version: Literal[1] = 1
    source_object_hash: HashRef
    migration_adapter_version: NonEmptyString
    migration_adapter_hash: HashRef
    migrated_stage_result_id: EntityId
    migrated_stage_result_hash: HashRef


class StageResultV1ToV2:
    """The sole accepted compatibility path for legacy StageResult version 1."""

    @classmethod
    def migrate(
        cls,
        legacy: Mapping[str, Any],
        *,
        context: LegacyMigrationContext,
    ) -> StageResult:
        cls._validate_source(legacy, context)
        stage = cls._require_string(legacy, "stage")
        expectation = context.stage_expectations.get(stage)  # type: ignore[arg-type]
        if expectation is None:
            raise ValueError(f"no registered legacy stage expectation for {stage}")
        status = cls._require_string(legacy, "status")
        if status not in expectation.allowed_statuses:
            raise ValueError(f"legacy status is not allowed for {stage}: {status}")

        tool_fingerprint = cls._resolve_tool(legacy, context)
        input_hashes = cls._normalize_input_hashes(legacy, context, expectation)
        metrics = cls._normalize_metrics(legacy, expectation)
        diagnostics = cls._normalize_diagnostics(legacy, context)
        raw_artifacts = cls._resolve_artifacts(
            legacy, context, expectation, status=status
        )

        return StageResult.model_validate(
            {
                "schema_version": 2,
                "stage_result_id": legacy["stage_result_id"],
                "run_id": legacy["run_id"],
                "candidate_id": legacy["candidate_id"],
                "stage": stage,
                "analysis_view_id": legacy["analysis_view_id"],
                "status": status,
                "tool_fingerprint": tool_fingerprint,
                "input_hashes": input_hashes,
                "metrics": metrics,
                "diagnostics": diagnostics,
                "raw_artifacts": raw_artifacts,
                "started_at": legacy["started_at"],
                "ended_at": legacy["ended_at"],
            }
        )

    @staticmethod
    def audit_payload(
        migrated: StageResult,
        *,
        legacy: Mapping[str, Any],
        context: LegacyMigrationContext,
    ) -> _MigrationAuditPayload:
        """Return the payload a caller persists in the migration RunEvent."""

        expected_result = StageResultV1ToV2.migrate(legacy, context=context)
        if canonical_json_bytes(migrated) != canonical_json_bytes(expected_result):
            raise ValueError("migrated result does not belong to the migration context")
        expected_extensions = {
            "legacy_source_object": context.source_object_hash,
            "legacy_migration_adapter": context.migration_adapter_hash,
        }
        if migrated.input_hashes.extensions != expected_extensions:
            raise ValueError("migrated result does not belong to the migration context")
        return _MigrationAuditPayload(
            source_object_hash=context.source_object_hash,
            migration_adapter_version=context.migration_adapter_version,
            migration_adapter_hash=context.migration_adapter_hash,
            migrated_stage_result_id=migrated.stage_result_id,
            migrated_stage_result_hash=canonical_sha256(migrated),
        )

    @staticmethod
    def _validate_source(
        legacy: Mapping[str, Any], context: LegacyMigrationContext
    ) -> None:
        unknown = set(legacy) - LEGACY_STAGE_RESULT_FIELDS
        missing = LEGACY_STAGE_RESULT_FIELDS - set(legacy)
        if unknown or missing:
            raise ValueError(
                "legacy field set is not translatable "
                f"(unknown={sorted(unknown)}, missing={sorted(missing)})"
            )
        if type(legacy["schema_version"]) is not int or legacy["schema_version"] != 1:
            raise ValueError("legacy StageResult schema_version must be exactly 1")
        if canonical_sha256(legacy) != context.source_object_hash:
            raise ValueError("legacy source-object hash does not match migration context")

    @staticmethod
    def _require_string(legacy: Mapping[str, Any], field: str) -> str:
        value = legacy[field]
        if not isinstance(value, str) or not value:
            raise ValueError(f"legacy {field} must be a nonempty string")
        return value

    @classmethod
    def _resolve_tool(
        cls, legacy: Mapping[str, Any], context: LegacyMigrationContext
    ) -> ToolFingerprint:
        tool_name = cls._require_string(legacy, "tool_name")
        tool_version = cls._require_string(legacy, "tool_version")
        matches = tuple(
            item
            for item in context.tool_fingerprints
            if item.tool_id == tool_name and item.version == tool_version
        )
        if len(matches) != 1:
            raise ValueError(
                "legacy tool name/version must resolve to exactly one tool fingerprint"
            )
        return matches[0]

    @staticmethod
    def _normalize_input_hashes(
        legacy: Mapping[str, Any],
        context: LegacyMigrationContext,
        expectation: LegacyStageExpectation,
    ) -> StageInputHashes:
        source = legacy["input_hashes"]
        if not isinstance(source, Mapping):
            raise ValueError("legacy input_hashes must be a mapping")
        normalized: dict[str, Any] = dict.fromkeys(INPUT_HASH_FIELDS)
        for legacy_name, hash_value in source.items():
            if not isinstance(legacy_name, str):
                raise ValueError("legacy input alias must be a string")
            canonical_name = context.legacy_field_aliases.get(legacy_name)
            if canonical_name is None:
                raise ValueError(f"unregistered legacy input alias: {legacy_name}")
            existing = normalized[canonical_name]
            if existing is not None and existing != hash_value:
                raise ValueError(f"conflicting legacy input hashes for {canonical_name}")
            normalized[canonical_name] = hash_value
        missing = sorted(
            field
            for field in expectation.required_input_fields
            if normalized[field] is None
        )
        if missing:
            raise ValueError(f"legacy input hashes miss required identity: {', '.join(missing)}")
        for field_name, expected_hash in context.expected_input_hashes.items():
            if normalized[field_name] != expected_hash:
                raise ValueError(
                    "legacy input does not match expected input identity "
                    f"for {field_name}"
                )
        normalized["extensions"] = {
            "legacy_source_object": context.source_object_hash,
            "legacy_migration_adapter": context.migration_adapter_hash,
        }
        return StageInputHashes.model_validate(normalized)

    @staticmethod
    def _normalize_metrics(
        legacy: Mapping[str, Any], expectation: LegacyStageExpectation
    ) -> dict[str, Any]:
        source = legacy["metrics"]
        if not isinstance(source, Mapping):
            raise ValueError("legacy metrics must be a mapping")
        allowed = METRIC_VALUE_FIELDS | {"runtime_ms"}
        unknown = set(source) - allowed
        if unknown:
            raise ValueError(f"unregistered legacy metric: {', '.join(sorted(unknown))}")
        missing_required = sorted(
            name
            for name in expectation.required_metric_fields
            if name not in source or source[name] is None
        )
        if missing_required:
            raise ValueError(
                f"legacy result is missing required metric: {', '.join(missing_required)}"
            )
        wrongly_present = sorted(
            name
            for name in expectation.unavailable_metric_reasons
            if name in source and source[name] is not None
        )
        if wrongly_present:
            raise ValueError(
                "legacy result reports metrics the registered stage could not produce: "
                f"{', '.join(wrongly_present)}"
            )
        normalized = {
            name: source.get(name) for name in METRIC_VALUE_FIELDS
        }
        normalized.update(
            {
                "analysis_view_id": legacy["analysis_view_id"],
                "runtime_ms": source["runtime_ms"],
                "missing_metric_reasons": dict(expectation.unavailable_metric_reasons),
            }
        )
        return normalized

    @classmethod
    def _normalize_diagnostics(
        cls, legacy: Mapping[str, Any], context: LegacyMigrationContext
    ) -> tuple[Diagnostic, ...]:
        source = legacy["diagnostics"]
        if not isinstance(source, list):
            raise ValueError("legacy diagnostics must be a list")
        normalized: list[Diagnostic] = []
        for item in source:
            if isinstance(item, str):
                token = item
                supplied_severity = None
                message = item
                supplied_evidence: tuple[str, ...] | None = None
            elif isinstance(item, Mapping):
                allowed = {"code", "severity", "message", "evidence_refs"}
                if set(item) - allowed or not {"code", "message", "evidence_refs"}.issubset(
                    item
                ):
                    raise ValueError("legacy diagnostic field set is not translatable")
                token = item["code"]
                supplied_severity = item.get("severity")
                message = item["message"]
                raw_evidence = item["evidence_refs"]
                if not isinstance(raw_evidence, list) or not all(
                    isinstance(value, str) for value in raw_evidence
                ):
                    raise ValueError("legacy diagnostic evidence must be a string list")
                supplied_evidence = tuple(raw_evidence)
            else:
                raise ValueError("legacy diagnostic must be a string or mapping")
            if not isinstance(token, str):
                raise ValueError("legacy diagnostic code must be a string")
            rule = context.diagnostic_code_registry.get(token)
            if rule is None:
                raise ValueError(f"unregistered legacy diagnostic code: {token}")
            if supplied_severity is not None and supplied_severity != rule.severity:
                raise ValueError("legacy diagnostic severity conflicts with registry")
            if supplied_evidence is not None and supplied_evidence != rule.evidence_ids:
                raise ValueError("legacy diagnostic evidence conflicts with registry")
            normalized.append(
                Diagnostic.model_validate(
                    {
                        "code": rule.code,
                        "severity": rule.severity,
                        "message": message,
                        "evidence_refs": rule.evidence_ids,
                    }
                )
            )
        return tuple(normalized)

    @staticmethod
    def _resolve_artifacts(
        legacy: Mapping[str, Any],
        context: LegacyMigrationContext,
        expectation: LegacyStageExpectation,
        *,
        status: str,
    ) -> tuple[ArtifactRef, ...]:
        source = legacy["raw_artifacts"]
        if not isinstance(source, list) or not source:
            raise ValueError("legacy raw_artifacts must be a nonempty list")
        resolved: list[ArtifactRef] = []
        for item in source:
            expected_metadata: Mapping[str, Any] | None = None
            if isinstance(item, str):
                uri = item
            elif isinstance(item, Mapping):
                if set(item) != {"uri", "sha256", "size_bytes"}:
                    raise ValueError("legacy artifact metadata field set is not translatable")
                uri = item["uri"]
                expected_metadata = item
            else:
                raise ValueError("legacy artifact must be a URI string or metadata mapping")
            if not isinstance(uri, str):
                raise ValueError("legacy artifact URI must be a string")
            matches = context.artifact_index.get(uri, ())
            if len(matches) != 1:
                raise ValueError("legacy artifact URI must resolve to exactly one artifact")
            artifact = matches[0]
            if expected_metadata is not None and (
                expected_metadata["sha256"] != artifact.sha256
                or expected_metadata["size_bytes"] != artifact.size_bytes
            ):
                raise ValueError("legacy artifact metadata does not match the artifact index")
            resolved.append(artifact)
        artifact_ids = tuple(item.artifact_id for item in resolved)
        _require_unique(artifact_ids, "resolved legacy artifact IDs")
        missing = sorted(set(expectation.required_artifact_ids) - set(artifact_ids))
        if status == "PASS" and missing:
            raise ValueError(f"legacy PASS lacks required artifact: {', '.join(missing)}")
        return tuple(resolved)


__all__ = [
    "LegacyDiagnosticRule",
    "LegacyMigrationContext",
    "LegacyStageExpectation",
    "StageResultV1ToV2",
]
