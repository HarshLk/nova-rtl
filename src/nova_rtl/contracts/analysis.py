"""Immutable analysis-view and power-activity identities."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeFloat,
    StrictContract,
    canonical_sha256,
)

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,95}$"),
]
HierarchyPath = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.$/]*$"),
]


class CornerArtifactIdentity(StrictContract):
    id: EntityId
    artifact_hash: HashRef


class AnalysisViewContract(StrictContract):
    """Complete immutable identity for one official timing, physical, or power view."""

    schema_version: Literal[1] = 1
    analysis_view_id: EntityId
    mode: Literal["FUNCTIONAL"]
    check: Literal["SETUP", "HOLD", "POWER"]
    required: bool
    liberty_corner: CornerArtifactIdentity
    rc_corner: CornerArtifactIdentity
    sdc_hash: HashRef
    operating_condition: NonEmptyString
    derate_policy_hash: HashRef
    clock_uncertainty_policy_hash: HashRef
    hard_limits: dict[str, FiniteFloat]
    required_stages: tuple[StableUpperString, ...] = Field(min_length=1)
    power_activity_contract_id: EntityId | None

    @field_validator("required_stages")
    @classmethod
    def stages_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required stages must be unique")
        return value

    @model_validator(mode="after")
    def view_requirements_match_check(self) -> Self:
        limit = {
            "SETUP": "setup_wns_ns",
            "HOLD": "hold_wns_ns",
            "POWER": "power_total_uw",
        }[self.check]
        if limit not in self.hard_limits:
            raise ValueError(f"{self.check} analysis view requires hard limit {limit}")
        if self.check == "POWER":
            if self.power_activity_contract_id is None:
                raise ValueError("POWER analysis view requires power_activity_contract_id")
        elif self.power_activity_contract_id is not None:
            raise ValueError("non-power analysis view cannot declare power_activity_contract_id")

        if self.required and self.check in {"SETUP", "HOLD"}:
            if "OPENSTA_FULL" not in self.required_stages:
                raise ValueError("required timing view must include OPENSTA_FULL")
            if not any(stage.startswith("OPENROAD_") for stage in self.required_stages):
                raise ValueError("required timing view must include a physical stage")
        return self


class PowerActivityContract(StrictContract):
    """Exact activity source and semantics required for comparable power evidence."""

    schema_version: Literal[1] = 1
    power_activity_contract_id: EntityId
    format: Literal["VCD", "SAIF", "VECTORLESS"]
    source_artifact: ArtifactRef | None
    scope: HierarchyPath
    time_window_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    propagation_policy: Literal["ANNOTATED", "PROPAGATED", "VECTORLESS_ESTIMATE"]
    comparability: Literal["OFFICIAL_COMPARABLE", "ESTIMATED_ONLY_NOT_COMPARABLE"]
    contract_hash: HashRef

    @model_validator(mode="after")
    def activity_semantics_and_hash_are_consistent(self) -> Self:
        start, end = self.time_window_ns
        if end <= start:
            raise ValueError("power activity time window end must be greater than start")
        if self.format in {"VCD", "SAIF"}:
            if self.source_artifact is None:
                raise ValueError(f"{self.format} power activity requires source_artifact")
            if self.propagation_policy == "VECTORLESS_ESTIMATE":
                raise ValueError("annotated activity cannot use VECTORLESS_ESTIMATE")
        else:
            if self.source_artifact is not None:
                raise ValueError("VECTORLESS power activity cannot declare source_artifact")
            if self.propagation_policy != "VECTORLESS_ESTIMATE":
                raise ValueError("VECTORLESS power activity requires VECTORLESS_ESTIMATE")
            if self.comparability != "ESTIMATED_ONLY_NOT_COMPARABLE":
                raise ValueError("VECTORLESS power activity is not officially comparable")

        expected_hash = canonical_sha256(self, exclude=frozenset({"contract_hash"}))
        if self.contract_hash != expected_hash:
            raise ValueError("contract_hash does not match canonical power activity identity")
        return self


__all__ = ["AnalysisViewContract", "PowerActivityContract"]
