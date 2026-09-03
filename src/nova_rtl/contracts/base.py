"""Shared strict primitives for persisted NOVA-RTL contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

HashRef = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$")]
ArtifactUri = Annotated[
    str,
    StringConstraints(pattern=r"^artifact://[a-zA-Z0-9._/-]+$"),
]
MediaType = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
    ),
]


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


def _require_rfc3339_z(value: Any) -> Any:
    if isinstance(value, str) and not value.endswith("Z"):
        raise ValueError("UTC timestamp string must use a trailing Z")
    return value


UtcDatetime = Annotated[
    AwareDatetime,
    BeforeValidator(_require_rfc3339_z),
    AfterValidator(_require_utc),
]
Classification = Literal["PUBLIC", "INTERNAL", "RESTRICTED_RTL"]
EvidenceKind = Literal[
    "PATH",
    "ARC",
    "CONE",
    "SOURCE_SPAN",
    "CLOCK",
    "CDC",
    "BINDING",
    "PHYSICAL",
    "FORMAL",
    "METRIC",
    "HISTORY",
]
DiagnosticSeverity = Literal["INFO", "WARNING", "ERROR", "FATAL"]
DiagnosticCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$"),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]

REGISTERED_STAGE_INPUT_EXTENSIONS = frozenset(
    {
        "analysis_view_set",
        "cdc_inventory",
        "clock_inventory",
        "critical_path_records",
        "evidence_graph",
        "legacy_migration_adapter",
        "legacy_source_object",
        "protection_policy",
        "synthesis_structure",
        "transform_registry",
    }
)
METRIC_VALUE_FIELDS = frozenset(
    {
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
)


class StrictContract(BaseModel):
    """Immutable contract that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(StrictContract):
    """Immutable identity and location of exact artifact bytes."""

    artifact_id: EntityId
    uri: ArtifactUri
    sha256: HashRef
    media_type: MediaType
    size_bytes: NonNegativeInt
    created_at: UtcDatetime
    producer_stage_result_id: EntityId | None
    classification: Classification


class EvidenceRef(StrictContract):
    """Stable semantic evidence address within an immutable snapshot."""

    evidence_id: EntityId
    kind: EvidenceKind
    artifact_id: EntityId
    json_pointer: str | None
    snapshot_hash: HashRef

    @field_validator("json_pointer")
    @classmethod
    def json_pointer_is_rfc6901(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value and not value.startswith("/"):
            raise ValueError("JSON pointer must be empty or begin with '/'")
        index = 0
        while index < len(value):
            if value[index] == "~":
                if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
                    raise ValueError("JSON pointer contains an invalid escape")
                index += 1
            index += 1
        return value


class Diagnostic(StrictContract):
    """Stable machine-readable finding with evidence-linked human context."""

    code: DiagnosticCode
    severity: DiagnosticSeverity
    message: NonEmptyString
    evidence_refs: tuple[EntityId, ...]

    @model_validator(mode="after")
    def evidence_is_present_for_domain_diagnostics(self) -> Self:
        if not self.evidence_refs and not self.code.startswith("INFRASTRUCTURE_"):
            raise ValueError("evidence_refs may be empty only for infrastructure diagnostics")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("diagnostic evidence_refs must be unique")
        return self


class StageInputHashes(StrictContract):
    """Named cache and comparability identities shared by deterministic stages."""

    rtl_snapshot: HashRef | None
    design_contract: HashRef | None
    constraints: HashRef | None
    constraint_binding: HashRef | None
    analysis_view: HashRef | None
    power_activity: HashRef | None
    platform_lock: HashRef | None
    tool_recipe: HashRef | None
    formal_model: HashRef | None
    parent_stage_result: HashRef | None
    extensions: dict[str, HashRef]

    @model_validator(mode="after")
    def extension_names_are_registered(self) -> Self:
        unknown = sorted(set(self.extensions) - REGISTERED_STAGE_INPUT_EXTENSIONS)
        if unknown:
            raise ValueError(f"unregistered stage input extension: {', '.join(unknown)}")
        return self


class MetricSet(StrictContract):
    """Complete, unit-explicit measured metrics for a stage or comparison."""

    analysis_view_id: EntityId | None
    setup_wns_ns: FiniteFloat | None
    setup_tns_ns: FiniteFloat | None
    hold_wns_ns: FiniteFloat | None
    hold_tns_ns: FiniteFloat | None
    failing_endpoints: NonNegativeInt | None
    critical_path_delay_ns: PositiveFloat | None
    estimated_fmax_mhz: PositiveFloat | None
    mapped_area_um2: NonNegativeFloat | None
    physical_area_um2: NonNegativeFloat | None
    cell_count: NonNegativeInt | None
    register_count: NonNegativeInt | None
    buffer_count: NonNegativeInt | None
    power_total_uw: NonNegativeFloat | None
    wirelength_um: NonNegativeFloat | None
    congestion_overflow: NonNegativeFloat | None
    runtime_ms: NonNegativeInt
    missing_metric_reasons: dict[str, NonEmptyString]

    @model_validator(mode="after")
    def missing_reasons_exactly_cover_unavailable_metrics(self) -> Self:
        unavailable = {
            field_name for field_name in METRIC_VALUE_FIELDS if getattr(self, field_name) is None
        }
        supplied = set(self.missing_metric_reasons)
        if supplied != unavailable:
            missing = sorted(unavailable - supplied)
            extra = sorted(supplied - unavailable)
            raise ValueError(
                "missing_metric_reasons must exactly cover unavailable metrics "
                f"(missing={missing}, extra={extra})"
            )
        return self


def canonical_json_bytes(value: StrictContract | Mapping[str, Any]) -> bytes:
    """Serialize a contract or already-normalized mapping into stable UTF-8 JSON bytes."""

    payload = value.model_dump(mode="json") if isinstance(value, StrictContract) else value
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(
    value: StrictContract | Mapping[str, Any], *, exclude: frozenset[str] = frozenset()
) -> HashRef:
    """Hash canonical JSON after excluding declared self-identity fields."""

    if isinstance(value, StrictContract):
        payload = value.model_dump(mode="json", exclude=exclude)
    else:
        payload = {key: item for key, item in value.items() if key not in exclude}
    return f"sha256:{sha256(canonical_json_bytes(payload)).hexdigest()}"


__all__ = [
    "ArtifactRef",
    "ArtifactUri",
    "AwareDatetime",
    "Classification",
    "Diagnostic",
    "DiagnosticCode",
    "DiagnosticSeverity",
    "EntityId",
    "EvidenceKind",
    "EvidenceRef",
    "FiniteFloat",
    "HashRef",
    "MetricSet",
    "NonEmptyString",
    "NonNegativeFloat",
    "NonNegativeInt",
    "PositiveFloat",
    "StageInputHashes",
    "StrictContract",
    "UtcDatetime",
    "canonical_json_bytes",
    "canonical_sha256",
]
