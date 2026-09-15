import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nova_rtl.planner.council import (
    CouncilBudgetError,
    CouncilRoleCall,
    CouncilRoleOutcome,
    load_council_policy,
    run_role_round,
)


class _BarrierInvoker:
    def __init__(self) -> None:
        self.started: set[str] = set()

    async def invoke(
        self,
        call: CouncilRoleCall,
        *,
        deadline_s: float,
    ) -> CouncilRoleOutcome:
        self.started.add(call.role_id)
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0)
        return CouncilRoleOutcome(
            role_id=call.role_id,
            status="PASS",
            structured_output={"role": call.role_id},
            input_tokens=10,
            output_tokens=5,
            latency_ms=1,
        )


class _TokenInvoker:
    async def invoke(
        self,
        call: CouncilRoleCall,
        *,
        deadline_s: float,
    ) -> CouncilRoleOutcome:
        return CouncilRoleOutcome(
            role_id=call.role_id,
            status="PASS",
            structured_output={},
            input_tokens=20_000,
            output_tokens=20_000,
            latency_ms=1,
        )


def _call(role: str) -> CouncilRoleCall:
    payload: Mapping[str, Any] = {"opportunity_id": "opportunity_target"}
    return CouncilRoleCall(
        role_id=role,
        role_kind="PROPOSER",
        input_payload=payload,
        token_budget=15_000,
    )


def test_role_round_fans_out_concurrently_and_returns_route_order() -> None:
    policy = load_council_policy(Path("config/policy/council.yaml"))
    invoker = _BarrierInvoker()

    outcomes = asyncio.run(
        run_role_round(
            (_call("timing_forensics"), _call("logic_domain_specialist")),
            invoker=invoker,
            policy=policy,
            deadline_s=1.0,
        )
    )

    assert tuple(item.role_id for item in outcomes) == (
        "timing_forensics",
        "logic_domain_specialist",
    )
    assert invoker.started == {"timing_forensics", "logic_domain_specialist"}


def test_role_round_rejects_aggregate_token_overrun() -> None:
    policy = load_council_policy(Path("config/policy/council.yaml"))

    with pytest.raises(CouncilBudgetError, match="aggregate token"):
        asyncio.run(
            run_role_round(
                (_call("timing_forensics"), _call("logic_domain_specialist")),
                invoker=_TokenInvoker(),
                policy=policy,
                deadline_s=1.0,
            )
        )


def test_role_round_rejects_fanout_above_policy() -> None:
    policy = load_council_policy(Path("config/policy/council.yaml"))
    calls = tuple(_call(f"role_{index}") for index in range(policy.fan_out_limit + 1))

    with pytest.raises(CouncilBudgetError, match="fan-out"):
        asyncio.run(
            run_role_round(calls, invoker=_BarrierInvoker(), policy=policy, deadline_s=1.0)
        )
