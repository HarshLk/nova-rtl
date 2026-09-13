"""Runtime-only planner boundaries and the immutable M6 policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, Self, runtime_checkable

import yaml
from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    Classification,
    EvidenceKind,
    HashRef,
    NonEmptyString,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.planning import PlannerRequest, PlannerResult, ProviderResult


class PlannerPolicyError(RuntimeError):
    """The planner policy cannot be loaded without weakening its bounds."""


class Message(StrictContract):
    """Provider-neutral message containing no executable capability."""

    role: Literal["SYSTEM", "USER"]
    content: NonEmptyString


class PlannerPolicy(StrictContract):
    """Fail-closed policy for M6 context, provider, repair, and fallback."""

    schema_version: Literal[1] = 1
    allowed_modes: tuple[Literal["HEURISTIC", "SINGLE_AGENT"], ...] = Field(
        min_length=1
    )
    max_proposals: int = Field(strict=True, gt=0, le=3)
    max_context_tokens: int = Field(strict=True, gt=0)
    max_provider_input_tokens: int = Field(strict=True, gt=0)
    max_provider_output_tokens: int = Field(strict=True, gt=0)
    max_provider_attempts: int = Field(strict=True, gt=0, le=2)
    schema_repair_attempts: int = Field(strict=True, ge=0, le=1)
    deadline_seconds: int = Field(strict=True, gt=0, le=300)
    fallback_mode: Literal["HEURISTIC"]
    allowed_evidence_kinds: tuple[EvidenceKind, ...] = Field(min_length=1)
    allowed_classifications: tuple[Classification, ...] = Field(min_length=1)
    allow_model_tools: Literal[False]
    allow_model_writes: Literal[False]
    external_tracing: Literal[False]
    policy_hash: HashRef

    @field_validator(
        "allowed_modes", "allowed_evidence_kinds", "allowed_classifications"
    )
    @classmethod
    def ordered_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("planner policy sequences must be unique")
        return value

    @model_validator(mode="after")
    def bounds_and_identity_are_coherent(self) -> Self:
        if "HEURISTIC" not in self.allowed_modes:
            raise ValueError("planner policy must retain deterministic fallback")
        if self.schema_repair_attempts + 1 > self.max_provider_attempts:
            raise ValueError("provider attempt bound cannot satisfy repair policy")
        if self.max_context_tokens > self.max_provider_input_tokens:
            raise ValueError("provider input budget must contain the context budget")
        if self.policy_hash != canonical_sha256(
            self, exclude=frozenset({"policy_hash"})
        ):
            raise ValueError("planner policy hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> PlannerPolicy:
        payload = {"schema_version": 1, **values}
        return cls(**payload, policy_hash=canonical_sha256(payload))


@runtime_checkable
class Planner(Protocol):
    """Advisory planner; it cannot materialize or accept candidate RTL."""

    async def propose(self, request: PlannerRequest) -> PlannerResult: ...


@runtime_checkable
class EvidenceProvider(Protocol):
    """Stable-ID-only, read-only planner evidence boundary."""

    def fetch(
        self,
        role_id: str,
        evidence_ids: Sequence[str],
        *,
        expected_snapshot_hash: str,
        reason: str,
    ) -> Sequence[Any]: ...


@runtime_checkable
class StructuredModelProvider(Protocol):
    """Schema-constrained provider with no tool or filesystem interface."""

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        schema: Mapping[str, Any],
        deadline_s: int,
    ) -> ProviderResult: ...


def load_planner_policy(path: Path) -> PlannerPolicy:
    """Load a strict policy and bind its canonical content identity."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PlannerPolicyError(f"cannot load planner policy: {error}") from error
    if not isinstance(payload, dict):
        raise PlannerPolicyError("planner policy must be a YAML mapping")
    try:
        return PlannerPolicy.build(**payload)
    except (TypeError, ValueError) as error:
        raise PlannerPolicyError(f"invalid planner policy: {error}") from error


__all__ = [
    "EvidenceProvider",
    "Message",
    "Planner",
    "PlannerPolicy",
    "PlannerPolicyError",
    "StructuredModelProvider",
    "load_planner_policy",
]
