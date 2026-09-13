from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.planner.interface import (
    Planner,
    PlannerPolicy,
    StructuredModelProvider,
    load_planner_policy,
)


def test_repository_planner_policy_is_bounded_and_self_hashed() -> None:
    policy = load_planner_policy(Path("config/policy/planner.yaml"))

    assert policy.allowed_modes == ("HEURISTIC", "SINGLE_AGENT")
    assert policy.max_proposals == 3
    assert policy.max_provider_attempts == 2
    assert policy.schema_repair_attempts == 1
    assert policy.fallback_mode == "HEURISTIC"
    assert policy.allow_model_tools is False
    assert policy.allow_model_writes is False
    assert policy.policy_hash.startswith("sha256:")


def test_planner_policy_rejects_unbounded_or_authoritative_model_access() -> None:
    with pytest.raises(ValidationError):
        PlannerPolicy.build(
            allowed_modes=("HEURISTIC", "SINGLE_AGENT"),
            max_proposals=4,
            max_context_tokens=7000,
            max_provider_input_tokens=9000,
            max_provider_output_tokens=2000,
            max_provider_attempts=2,
            schema_repair_attempts=1,
            deadline_seconds=120,
            fallback_mode="HEURISTIC",
            allowed_evidence_kinds=("PATH",),
            allowed_classifications=("INTERNAL",),
            allow_model_tools=True,
            allow_model_writes=False,
            external_tracing=False,
        )


def test_planner_and_provider_are_async_advisory_protocols() -> None:
    assert Planner.propose is not None
    assert StructuredModelProvider.generate is not None
