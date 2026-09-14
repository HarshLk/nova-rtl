"""Versioned recovery-policy loading and canonical identity."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, model_validator

from nova_rtl.contracts.base import HashRef, NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.contracts.recovery import FailureFamily, RecoveryAction, Repairability


class RecoveryRule(StrictContract):
    rule_id: str = Field(pattern=r"^RECOVERY_[A-Z0-9_]+$")
    priority: int = Field(strict=True, ge=0)
    repairability: Repairability
    retryable: bool
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    allowed_actions: tuple[RecoveryAction, ...] = Field(min_length=1)
    required_evidence_kinds: tuple[str, ...] = Field(min_length=1)


class RecoveryPolicyRegistry(StrictContract):
    schema_version: Literal[1] = 1
    policy_version: NonEmptyString
    fingerprint_schema: NonEmptyString
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    wns_progress_epsilon_ns: float = Field(ge=0.0)
    area_progress_epsilon_percent: float = Field(ge=0.0)
    repeated_failure_count: int = Field(strict=True, gt=0)
    max_recovery_depth: int = Field(strict=True, ge=0)
    rules: dict[FailureFamily, RecoveryRule]
    policy_hash: HashRef

    @model_validator(mode="after")
    def identity_and_coverage_are_exact(self) -> Self:
        expected_families = set(FailureFamily.__args__)
        if set(self.rules) != expected_families:
            missing = sorted(expected_families - set(self.rules))
            extra = sorted(set(self.rules) - expected_families)
            raise ValueError(f"recovery rule coverage mismatch: missing={missing}, extra={extra}")
        rule_ids = tuple(rule.rule_id for rule in self.rules.values())
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("recovery rule IDs must be unique")
        expected_hash = canonical_sha256(self, exclude=frozenset({"policy_hash"}))
        if self.policy_hash != expected_hash:
            raise ValueError("policy_hash does not match canonical recovery policy")
        return self


def load_recovery_policy(path: Path) -> RecoveryPolicyRegistry:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("recovery policy must be a YAML mapping")
    payload["rules"] = dict(sorted(payload["rules"].items()))
    payload["policy_hash"] = canonical_sha256(payload)
    return RecoveryPolicyRegistry.model_validate(payload)


__all__ = ["RecoveryPolicyRegistry", "RecoveryRule", "load_recovery_policy"]
