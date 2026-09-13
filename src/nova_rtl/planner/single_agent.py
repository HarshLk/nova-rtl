"""Bounded single-agent planning with one schema repair and safe fallback."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, ValidationError

from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.contracts.base import Diagnostic, StrictContract, canonical_sha256
from nova_rtl.contracts.optimization import OptimizationOpportunity, OptimizationProposal
from nova_rtl.contracts.planning import (
    ContextRequest,
    PlannerRequest,
    PlannerResult,
    ProviderResult,
    RoleContextPack,
)
from nova_rtl.planner.context import build_context
from nova_rtl.planner.evidence import InMemoryEvidenceProvider
from nova_rtl.planner.heuristic import HeuristicPlanner
from nova_rtl.planner.interface import Message, PlannerPolicy, StructuredModelProvider
from nova_rtl.transforms.registry import TransformRegistry, TransformRegistryError


class _ProposalEnvelope(StrictContract):
    proposals: tuple[OptimizationProposal, ...] = Field(max_length=3)


class SingleAgentPlannerError(RuntimeError):
    """The single-agent boundary cannot validate or resolve its authority."""


class SingleAgentPlanner:
    """Treat model output as untrusted advice and compile only safe proposals."""

    def __init__(
        self,
        *,
        context_request: ContextRequest,
        common_envelope: Mapping[str, Any],
        evidence_provider: InMemoryEvidenceProvider,
        provider: StructuredModelProvider,
        heuristic_fallback: HeuristicPlanner,
        opportunities: Sequence[OptimizationOpportunity],
        registry: TransformRegistry,
        artifact_store: ArtifactStore,
        policy: PlannerPolicy,
    ) -> None:
        self._context_request = context_request
        self._common_envelope = dict(common_envelope)
        self._evidence_provider = evidence_provider
        self._provider = provider
        self._heuristic = heuristic_fallback
        self._opportunities = {item.opportunity_id: item for item in opportunities}
        self._registry = registry
        self._store = artifact_store
        self._policy = policy
        self._provider_results: list[ProviderResult] = []
        self._resolved: dict[str, OptimizationProposal] = {}
        self._context_pack: RoleContextPack | None = None
        self._output_schema = _ProposalEnvelope.model_json_schema(mode="validation")
        self._output_schema_hash = canonical_sha256(self._output_schema)

    @property
    def provider_results(self) -> tuple[ProviderResult, ...]:
        return tuple(self._provider_results)

    @property
    def context_pack(self) -> RoleContextPack | None:
        return self._context_pack

    def proposals_for(
        self, result: PlannerResult
    ) -> tuple[OptimizationProposal, ...]:
        try:
            return tuple(self._resolved[item] for item in result.proposal_ids)
        except KeyError:
            return self._heuristic.proposals_for(result)

    def _messages(self, pack: RoleContextPack) -> tuple[Message, ...]:
        try:
            payload = json.loads(self._store.open_verified(pack.rendered_message_artifact).read())
            return tuple(Message.model_validate(item) for item in payload["messages"])
        except (ArtifactStoreError, KeyError, TypeError, ValueError) as error:
            raise SingleAgentPlannerError(
                f"rendered context messages are invalid: {error}"
            ) from error

    def _output(self, result: ProviderResult) -> dict[str, Any]:
        if result.structured_output_artifact is None:
            raise SingleAgentPlannerError("passing provider result lacks output artifact")
        try:
            value = json.loads(
                self._store.open_verified(result.structured_output_artifact).read()
            )
        except (ArtifactStoreError, ValueError) as error:
            raise SingleAgentPlannerError(
                f"provider output artifact is invalid: {error}"
            ) from error
        if not isinstance(value, dict):
            raise SingleAgentPlannerError("provider output must be a JSON object")
        return value

    def _semantic_error(
        self, proposal: OptimizationProposal, request: PlannerRequest
    ) -> str | None:
        opportunity = self._opportunities.get(request.opportunity_id)
        if opportunity is None:
            return "unknown opportunity"
        if opportunity.editability != "RTL_EDITABLE":
            return "opportunity is protected or not editable"
        if (
            proposal.parent_candidate_id != request.parent_candidate_id
            or proposal.opportunity_id != request.opportunity_id
        ):
            return "proposal lineage differs from planner request"
        if (
            proposal.target.source_span_id not in opportunity.source_spans
            or proposal.target.source_span_id in opportunity.protected_neighbors
        ):
            return "proposal target is not an authorized editable source span"
        if proposal.transformation.operation not in opportunity.eligible_transform_families:
            return "proposal transform is not eligible for the opportunity"
        if proposal.correctness.contract not in opportunity.proof_contracts:
            return "proposal correctness contract is not authorized"
        refs = proposal.evidence_refs
        if not {item.evidence_id for item in refs}.issubset(
            request.authorized_evidence_ids
        ):
            return "proposal cites private or unauthorized evidence"
        if {item.snapshot_hash for item in refs} != {request.evidence_snapshot_hash}:
            return "proposal evidence snapshot is stale"
        try:
            descriptor = self._registry.get_descriptor(
                proposal.transformation.operation
            )
            self._registry.validate_parameters(
                proposal.transformation.operation,
                proposal.transformation.parameters,
            )
        except (TransformRegistryError, ValueError):
            return "proposal operation or parameters are not registered"
        if (
            descriptor.family != proposal.transformation.family
            or descriptor.correctness_contract != proposal.correctness.contract
        ):
            return "proposal registry metadata differs"
        return None

    def _diagnostic(self, code: str, message: str, request: PlannerRequest) -> Diagnostic:
        return Diagnostic(
            code=code,
            severity="ERROR",
            message=message,
            evidence_refs=(request.opportunity_id,),
        )

    async def _fallback(
        self,
        request: PlannerRequest,
        *,
        provider_result: ProviderResult,
        diagnostics: tuple[Diagnostic, ...],
        pack: RoleContextPack,
    ) -> PlannerResult:
        heuristic_request = request.model_copy(update={"planner_mode": "HEURISTIC"})
        heuristic = await self._heuristic.propose(heuristic_request)
        proposals = self._heuristic.proposals_for(heuristic)
        self._resolved.update({item.proposal_id: item for item in proposals})
        payload = {
            "schema_version": 1,
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "planner_mode": "SINGLE_AGENT",
            "status": heuristic.status,
            "proposal_ids": heuristic.proposal_ids,
            "rejected_output_diagnostics": diagnostics,
            "context_pack_hashes": (pack.rendered_message_hash,),
            "council_result_id": None,
            "provider_id": provider_result.provider_id,
            "model_id": provider_result.model_id,
            "prompt_hash": provider_result.prompt_hash,
            "output_schema_hash": self._output_schema_hash,
            "input_tokens": sum(item.input_tokens for item in self._provider_results),
            "output_tokens": sum(item.output_tokens for item in self._provider_results),
            "latency_ms": sum(item.latency_ms for item in self._provider_results),
            "fallback_used": True,
            "upstream_provider_result_id": provider_result.provider_result_id,
        }
        identity_payload = dict(payload)
        identity_payload["rejected_output_diagnostics"] = tuple(
            item.model_dump(mode="json") for item in diagnostics
        )
        identity = canonical_sha256(
            {
                "planner_request_id": request.planner_request_id,
                "result": identity_payload,
            }
        )
        return PlannerResult(
            planner_result_id=f"planner_result_{identity[-24:]}",
            **payload,
        )

    async def propose(self, request: PlannerRequest) -> PlannerResult:
        """Build context, invoke once, repair schema once, or use heuristic fallback."""

        if request.planner_mode != "SINGLE_AGENT":
            raise SingleAgentPlannerError("single-agent planner requires SINGLE_AGENT mode")
        if request.policy_hash != self._policy.policy_hash:
            raise SingleAgentPlannerError("planner policy identity changed")
        if request.transform_registry_hash != self._registry.registry_hash:
            raise SingleAgentPlannerError("transform registry identity changed")
        if request.proposal_limit > self._policy.max_proposals:
            raise SingleAgentPlannerError("proposal limit exceeds planner policy")
        if (
            request.required_output_schema_name != "optimization-proposal"
            or request.required_output_schema_version != 2
        ):
            raise SingleAgentPlannerError("single-agent output schema is unsupported")
        pack = build_context(
            self._context_request,
            planner_request=request,
            common_envelope=self._common_envelope,
            evidence_provider=self._evidence_provider,
            artifact_store=self._store,
        )
        self._context_pack = pack
        messages = self._messages(pack)
        deadline_s = min(
            self._policy.deadline_seconds,
            max(1, request.latency_budget_ms // 1000),
        )
        result = await self._provider.generate(
            messages=messages,
            schema=self._output_schema,
            deadline_s=deadline_s,
        )
        self._provider_results.append(result)
        if result.status != "PASS":
            return await self._fallback(
                request,
                provider_result=result,
                diagnostics=(
                    self._diagnostic(
                        "PLANNER_PROVIDER_FAILURE",
                        f"provider returned {result.status}: {result.error_code}",
                        request,
                    ),
                ),
                pack=pack,
            )

        output = self._output(result)
        try:
            envelope = _ProposalEnvelope.model_validate(output)
        except ValidationError as first_error:
            repair_messages = (
                Message(
                    role="SYSTEM",
                    content=(
                        "Repair JSON shape only. Do not add evidence, targets, transforms, "
                        "parameters, RTL, or new technical claims."
                    ),
                ),
                Message(
                    role="USER",
                    content=(
                        "ORIGINAL_STRUCTURED_OUTPUT\n"
                        + json.dumps(output, separators=(",", ":"), sort_keys=True)
                        + "\nSCHEMA_ERRORS\n"
                        + str(first_error)
                    ),
                ),
            )
            repaired = await self._provider.generate(
                messages=repair_messages,
                schema=self._output_schema,
                deadline_s=deadline_s,
            )
            self._provider_results.append(repaired)
            if repaired.status != "PASS":
                return await self._fallback(
                    request,
                    provider_result=repaired,
                    diagnostics=(
                        self._diagnostic(
                            "PLANNER_SCHEMA_INVALID",
                            "schema repair provider attempt failed",
                            request,
                        ),
                    ),
                    pack=pack,
                )
            try:
                envelope = _ProposalEnvelope.model_validate(self._output(repaired))
            except ValidationError as second_error:
                return await self._fallback(
                    request,
                    provider_result=repaired,
                    diagnostics=(
                        self._diagnostic(
                            "PLANNER_SCHEMA_INVALID",
                            f"schema remained invalid after one repair: {second_error}",
                            request,
                        ),
                    ),
                    pack=pack,
                )
            result = repaired

        proposals = envelope.proposals[: request.proposal_limit]
        semantic_errors = tuple(
            message
            for proposal in proposals
            if (message := self._semantic_error(proposal, request)) is not None
        )
        if semantic_errors:
            return await self._fallback(
                request,
                provider_result=result,
                diagnostics=tuple(
                    self._diagnostic("PLANNER_SEMANTIC_REJECTED", message, request)
                    for message in semantic_errors
                ),
                pack=pack,
            )
        self._resolved.update({item.proposal_id: item for item in proposals})
        status = "PASS" if proposals else "NO_SAFE_PROPOSAL"
        payload = {
            "schema_version": 1,
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "planner_mode": "SINGLE_AGENT",
            "status": status,
            "proposal_ids": tuple(item.proposal_id for item in proposals),
            "rejected_output_diagnostics": (),
            "context_pack_hashes": (pack.rendered_message_hash,),
            "council_result_id": None,
            "provider_id": result.provider_id,
            "model_id": result.model_id,
            "prompt_hash": result.prompt_hash,
            "output_schema_hash": self._output_schema_hash,
            "input_tokens": sum(item.input_tokens for item in self._provider_results),
            "output_tokens": sum(item.output_tokens for item in self._provider_results),
            "latency_ms": sum(item.latency_ms for item in self._provider_results),
            "fallback_used": False,
            "upstream_provider_result_id": None,
        }
        identity = canonical_sha256(
            {"planner_request_id": request.planner_request_id, "result": payload}
        )
        return PlannerResult(
            planner_result_id=f"planner_result_{identity[-24:]}",
            **payload,
        )


__all__ = ["SingleAgentPlanner", "SingleAgentPlannerError"]
