from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.planner.interface import Message, load_planner_policy
from nova_rtl.planner.provider import (
    RecordedProviderResponse,
    RecordedStructuredModelProvider,
)


def _response(**updates: object) -> RecordedProviderResponse:
    values = {
        "status": "PASS",
        "structured_output": {"proposal_id": "proposal_recorded"},
        "input_tokens": 20,
        "output_tokens": 10,
        "latency_ms": 25,
        "error_code": None,
        "completed_at": datetime(2026, 9, 14, tzinfo=UTC),
    }
    values.update(updates)
    return RecordedProviderResponse(**values)


def _provider(tmp_path: Path, *responses: RecordedProviderResponse):
    return RecordedStructuredModelProvider(
        planner_request_id="planner_request_provider",
        context_request_id="context_request_provider",
        provider_id="recorded_provider",
        model_id="recorded-m6-v1",
        provider_configuration={"mode": "offline", "temperature": 0},
        requested_schema_name="optimization-proposal",
        requested_schema_version=2,
        responses=responses,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        policy=load_planner_policy(Path("config/policy/planner.yaml")),
    )


def test_recorded_provider_preserves_canonical_structured_output(tmp_path: Path) -> None:
    provider = _provider(tmp_path, _response())

    result = asyncio.run(
        provider.generate(
            messages=(Message(role="USER", content="choose a registered experiment"),),
            schema={"type": "object"},
            deadline_s=30,
        )
    )

    assert result.status == "PASS"
    assert result.provider_id == "recorded_provider"
    assert result.provider_configuration_hash.startswith("sha256:")
    assert result.structured_output_artifact is not None
    content = provider.artifact_store.open_verified(
        result.structured_output_artifact
    ).read()
    assert json.loads(content) == {"proposal_id": "proposal_recorded"}
    assert provider.call_count == 1


def test_recorded_provider_reports_timeout_without_normalized_output(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        _response(
            status="TIMEOUT",
            structured_output=None,
            output_tokens=0,
            error_code="PROVIDER_DEADLINE_EXCEEDED",
        ),
    )

    result = asyncio.run(
        provider.generate(
            messages=(Message(role="USER", content="bounded request"),),
            schema={"type": "object"},
            deadline_s=30,
        )
    )

    assert result.status == "TIMEOUT"
    assert result.structured_output_artifact is None
    assert result.error_code == "PROVIDER_DEADLINE_EXCEEDED"


def test_recorded_provider_enforces_output_token_budget(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        _response(output_tokens=2001, structured_output={"large": "response"}),
    )

    result = asyncio.run(
        provider.generate(
            messages=(Message(role="USER", content="bounded request"),),
            schema={"type": "object"},
            deadline_s=30,
        )
    )

    assert result.status == "BUDGET_EXHAUSTED"
    assert result.error_code == "PROVIDER_OUTPUT_TOKEN_BUDGET"
    assert result.structured_output_artifact is None
