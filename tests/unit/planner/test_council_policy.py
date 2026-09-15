from pathlib import Path

import pytest

from nova_rtl.planner.council import CouncilPolicyError, load_council_policy

POLICY = Path("config/policy/council.yaml")

def test_published_council_policy_is_bounded_and_self_hashed() -> None:
    policy = load_council_policy(POLICY)

    assert policy.max_reasoning_roles == 5
    assert policy.max_strategist_roles == 2
    assert policy.max_final_proposals == 3
    assert policy.max_revisions == 1
    assert policy.deadline_seconds == 120
    assert policy.max_aggregate_tokens == 30_000
    assert policy.fallback_order == ("SINGLE_AGENT", "HEURISTIC")
    assert policy.allow_model_tools is False
    assert policy.allow_model_writes is False


def test_council_policy_rejects_authority_or_budget_expansion(tmp_path: Path) -> None:
    source = POLICY.read_text(encoding="utf-8")
    for replacement in (
        source.replace("max_reasoning_roles: 5", "max_reasoning_roles: 6"),
        source.replace("allow_model_tools: false", "allow_model_tools: true"),
        source.replace("max_revisions: 1", "max_revisions: 2"),
    ):
        path = tmp_path / f"invalid-{len(list(tmp_path.iterdir()))}.yaml"
        path.write_text(replacement, encoding="utf-8")
        with pytest.raises(CouncilPolicyError):
            load_council_policy(path)
