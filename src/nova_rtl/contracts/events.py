"""Ordered replay-authority event contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    HashRef,
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
)
from nova_rtl.contracts.execution import ResourceUsage

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$"),
]
SchemaName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,95}$"),
]


class RunEvent(StrictContract):
    """Append-only event whose sequence, not timestamp, defines replay order."""

    schema_version: Literal[1] = 1
    event_id: EntityId
    run_id: EntityId
    sequence: NonNegativeInt
    event_type: StableUpperString
    entity_type: StableUpperString
    entity_id: EntityId
    timestamp: UtcDatetime
    prior_state: StableUpperString | None
    new_state: StableUpperString | None
    policy_hash: HashRef
    payload_schema_name: SchemaName
    payload_schema_version: int = Field(strict=True, gt=0)
    payload_artifact: ArtifactRef
    duration_ms: NonNegativeInt
    resource_usage: ResourceUsage
    status: Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]
    error_code: StableUpperString | None

    @model_validator(mode="after")
    def transition_and_status_are_coherent(self) -> Self:
        if self.prior_state is not None and self.prior_state == self.new_state:
            raise ValueError("state transition must change state")
        if self.status == "PASS":
            if self.error_code is not None:
                raise ValueError("PASS event cannot declare error_code")
        elif self.error_code is None:
            raise ValueError("non-pass event requires error_code")
        if self.resource_usage.wall_time_ms > self.duration_ms:
            raise ValueError("resource wall time cannot exceed event duration")
        return self


__all__ = ["RunEvent"]
