"""Provider-neutral recorded structured-model boundary for reproducible M6 runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import model_validator

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import (
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.planning import ProviderResult
from nova_rtl.planner.interface import Message, PlannerPolicy


class ProviderBoundaryError(RuntimeError):
    """A provider configuration or invocation violates the bounded interface."""


class RecordedProviderResponse(StrictContract):
    """One immutable provider outcome used for deterministic execution and replay."""

    status: Literal[
        "PASS", "INVALID_OUTPUT", "PROVIDER_ERROR", "TIMEOUT", "BUDGET_EXHAUSTED"
    ]
    structured_output: dict[str, Any] | None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    latency_ms: NonNegativeInt
    error_code: str | None
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def status_is_coherent(self) -> Self:
        if self.status == "PASS":
            if self.structured_output is None or self.error_code is not None:
                raise ValueError("PASS recorded response requires output and no error")
        elif self.structured_output is not None or self.error_code is None:
            raise ValueError("non-pass recorded response requires only an error code")
        return self


class RecordedStructuredModelProvider:
    """Serve pinned structured outcomes without network, tools, or filesystem authority."""

    def __init__(
        self,
        *,
        planner_request_id: str,
        context_request_id: str,
        provider_id: str,
        model_id: str,
        provider_configuration: Mapping[str, Any],
        requested_schema_name: str,
        requested_schema_version: int,
        responses: Sequence[RecordedProviderResponse],
        artifact_store: ArtifactStore,
        policy: PlannerPolicy,
    ) -> None:
        if not responses:
            raise ValueError("recorded provider requires at least one response")
        sensitive = tuple(
            key
            for key in provider_configuration
            if any(fragment in key.lower() for fragment in ("key", "secret", "token"))
        )
        if sensitive:
            raise ProviderBoundaryError("provider configuration contains credential fields")
        self._planner_request_id = planner_request_id
        self._context_request_id = context_request_id
        self._provider_id = provider_id
        self._model_id = model_id
        self._configuration_hash = canonical_sha256(provider_configuration)
        self._schema_name = requested_schema_name
        self._schema_version = requested_schema_version
        self._responses = tuple(responses)
        self._policy = policy
        self._calls = 0
        self.artifact_store = artifact_store

    @property
    def call_count(self) -> int:
        return self._calls

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        schema: Mapping[str, Any],
        deadline_s: int,
    ) -> ProviderResult:
        """Return one recorded result under the same limits as a live provider."""

        if not messages:
            raise ProviderBoundaryError("provider request requires messages")
        if deadline_s <= 0 or deadline_s > self._policy.deadline_seconds:
            raise ProviderBoundaryError("provider deadline exceeds planner policy")
        sequence = self._calls
        self._calls += 1
        prompt_hash = canonical_sha256(
            {
                "messages": tuple(item.model_dump(mode="json") for item in messages),
                "schema_hash": canonical_sha256(schema),
            }
        )
        if sequence >= min(len(self._responses), self._policy.max_provider_attempts):
            response = RecordedProviderResponse(
                status="PROVIDER_ERROR",
                structured_output=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                error_code="PROVIDER_ATTEMPT_LIMIT",
                completed_at=self._responses[-1].completed_at,
            )
        else:
            response = self._responses[sequence]
        status = response.status
        error_code = response.error_code
        output = response.structured_output
        if response.input_tokens > self._policy.max_provider_input_tokens:
            status = "BUDGET_EXHAUSTED"
            error_code = "PROVIDER_INPUT_TOKEN_BUDGET"
            output = None
        elif response.output_tokens > self._policy.max_provider_output_tokens:
            status = "BUDGET_EXHAUSTED"
            error_code = "PROVIDER_OUTPUT_TOKEN_BUDGET"
            output = None
        elif response.latency_ms > deadline_s * 1000:
            status = "TIMEOUT"
            error_code = "PROVIDER_DEADLINE_EXCEEDED"
            output = None

        output_ref = None
        output_hash = None
        if status == "PASS" and output is not None:
            content = canonical_json_bytes(output)
            output_hash = "sha256:" + __import__("hashlib").sha256(content).hexdigest()
            output_ref = self.artifact_store.put_named_bytes(
                content,
                artifact_id=f"artifact_provider_output_{output_hash[-16:]}",
                media_type="application/json",
                classification="RESTRICTED_RTL",
                producer_stage_result_id=None,
            )
        identity = canonical_sha256(
            {
                "planner_request_id": self._planner_request_id,
                "context_request_id": self._context_request_id,
                "provider_id": self._provider_id,
                "model_id": self._model_id,
                "provider_configuration_hash": self._configuration_hash,
                "prompt_hash": prompt_hash,
                "schema_name": self._schema_name,
                "schema_version": self._schema_version,
                "attempt": sequence,
                "status": status,
                "output_hash": output_hash,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
                "error_code": error_code,
            }
        )
        return ProviderResult(
            provider_result_id=f"provider_result_{identity[-24:]}",
            planner_request_id=self._planner_request_id,
            context_request_id=self._context_request_id,
            status=status,
            provider_id=self._provider_id,
            model_id=self._model_id,
            provider_configuration_hash=self._configuration_hash,
            prompt_hash=prompt_hash,
            requested_schema_name=self._schema_name,
            requested_schema_version=self._schema_version,
            structured_output_artifact=output_ref,
            structured_output_hash=output_hash,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            error_code=error_code,
            completed_at=response.completed_at,
        )


__all__ = [
    "ProviderBoundaryError",
    "RecordedProviderResponse",
    "RecordedStructuredModelProvider",
]
