from __future__ import annotations

from pathlib import Path

from nova_rtl.contracts.recovery import FailureFamily
from nova_rtl.recovery.policy import load_recovery_policy

ROOT = Path(__file__).resolve().parents[3]


def test_published_policy_has_locked_m7_thresholds_and_stable_hash() -> None:
    first = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    second = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")

    assert first.policy_version == "m7-recovery-v1"
    assert first.similarity_threshold == 0.85
    assert first.wns_progress_epsilon_ns == 0.01
    assert first.area_progress_epsilon_percent == 0.10
    assert first.repeated_failure_count == 2
    assert first.max_recovery_depth == 4
    assert first.policy_hash == second.policy_hash


def test_policy_covers_every_failure_family_once_and_fail_closed() -> None:
    policy = load_recovery_policy(ROOT / "config/policy/recovery_rules.yaml")
    expected = set(FailureFamily.__args__)

    assert set(policy.rules) == expected
    assert policy.rules["INFRASTRUCTURE_TRANSIENT"].allowed_actions == (
        "RETRY_INFRASTRUCTURE",
        "STOP_RUN_OR_REQUEST_HUMAN",
    )
    assert policy.rules["PROTECTED_STRUCTURE_VIOLATION"].allowed_actions == (
        "REJECT_CANDIDATE",
        "STOP_RUN_OR_REQUEST_HUMAN",
    )
    assert all(rule.rule_id.startswith("RECOVERY_") for rule in policy.rules.values())
