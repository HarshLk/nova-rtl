"""Deterministic heuristic planner implementing the canonical M6 boundary."""

from __future__ import annotations

from collections.abc import Sequence

from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.optimization import OptimizationOpportunity, OptimizationProposal
from nova_rtl.contracts.planning import PlannerRequest, PlannerResult
from nova_rtl.transforms.registry import TransformRegistry, TransformRegistryError


class PlannerIntegrityError(RuntimeError):
    """Planner inputs violate immutable evidence or registry authority."""


class HeuristicPlanner:
    """Select precomputed deterministic proposals without model involvement."""

    def __init__(
        self,
        *,
        opportunities: Sequence[OptimizationOpportunity],
        proposals: Sequence[OptimizationProposal],
        registry: TransformRegistry,
    ) -> None:
        self._opportunities = {item.opportunity_id: item for item in opportunities}
        if len(self._opportunities) != len(tuple(opportunities)):
            raise ValueError("heuristic opportunities must have unique identities")
        self._proposals = {item.proposal_id: item for item in proposals}
        if len(self._proposals) != len(tuple(proposals)):
            raise ValueError("heuristic proposals must have unique identities")
        self._by_opportunity: dict[str, tuple[OptimizationProposal, ...]] = {}
        for opportunity_id in sorted(self._opportunities):
            self._by_opportunity[opportunity_id] = tuple(
                sorted(
                    (
                        item
                        for item in proposals
                        if item.opportunity_id == opportunity_id
                    ),
                    key=lambda item: item.proposal_id,
                )
            )
        self._registry = registry
        self._output_schema_hash = canonical_sha256(
            OptimizationProposal.model_json_schema(mode="validation")
        )

    def _is_safe(
        self,
        proposal: OptimizationProposal,
        opportunity: OptimizationOpportunity,
        request: PlannerRequest,
    ) -> bool:
        if opportunity.editability != "RTL_EDITABLE":
            return False
        if (
            proposal.parent_candidate_id != request.parent_candidate_id
            or proposal.opportunity_id != request.opportunity_id
            or proposal.target.source_span_id not in opportunity.source_spans
            or proposal.target.source_span_id in opportunity.protected_neighbors
            or proposal.transformation.operation
            not in opportunity.eligible_transform_families
            or proposal.correctness.contract not in opportunity.proof_contracts
        ):
            return False
        evidence_ids = {item.evidence_id for item in proposal.evidence_refs}
        snapshots = {item.snapshot_hash for item in proposal.evidence_refs}
        if not evidence_ids.issubset(request.authorized_evidence_ids):
            return False
        if snapshots != {request.evidence_snapshot_hash}:
            return False
        try:
            descriptor = self._registry.get_descriptor(
                proposal.transformation.operation
            )
            self._registry.validate_parameters(
                proposal.transformation.operation,
                proposal.transformation.parameters,
            )
        except (TransformRegistryError, ValueError):
            return False
        return (
            descriptor.family == proposal.transformation.family
            and descriptor.correctness_contract == proposal.correctness.contract
        )

    async def propose(self, request: PlannerRequest) -> PlannerResult:
        """Return bounded, registry-validated proposals for one opportunity."""

        if request.planner_mode != "HEURISTIC":
            raise PlannerIntegrityError("heuristic planner requires HEURISTIC mode")
        if request.transform_registry_hash != self._registry.registry_hash:
            raise PlannerIntegrityError("planner transform registry identity changed")
        if (
            request.required_output_schema_name != "optimization-proposal"
            or request.required_output_schema_version != 2
        ):
            raise PlannerIntegrityError("planner output schema is unsupported")
        opportunity = self._opportunities.get(request.opportunity_id)
        if opportunity is None:
            raise PlannerIntegrityError("planner opportunity identity is unknown")
        if opportunity.parent_candidate_id != request.parent_candidate_id:
            raise PlannerIntegrityError("planner opportunity parent identity changed")
        selected = tuple(
            item
            for item in self._by_opportunity[request.opportunity_id]
            if self._is_safe(item, opportunity, request)
        )[: request.proposal_limit]
        result_payload = {
            "schema_version": 1,
            "run_id": request.run_id,
            "opportunity_id": request.opportunity_id,
            "planner_mode": "HEURISTIC",
            "status": "PASS" if selected else "NO_SAFE_PROPOSAL",
            "proposal_ids": tuple(item.proposal_id for item in selected),
            "rejected_output_diagnostics": (),
            "context_pack_hashes": (),
            "council_result_id": None,
            "provider_id": None,
            "model_id": None,
            "prompt_hash": None,
            "output_schema_hash": self._output_schema_hash,
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0,
            "fallback_used": False,
            "upstream_provider_result_id": None,
        }
        identity = canonical_sha256(
            {
                "planner_request_id": request.planner_request_id,
                "result": result_payload,
            }
        )
        return PlannerResult(
            planner_result_id=f"planner_result_{identity[-24:]}",
            **result_payload,
        )

    def proposals_for(
        self, result: PlannerResult
    ) -> tuple[OptimizationProposal, ...]:
        """Resolve result IDs to immutable proposals owned by this planner."""

        try:
            return tuple(self._proposals[item] for item in result.proposal_ids)
        except KeyError as error:
            raise PlannerIntegrityError("planner result references an unknown proposal") from error


__all__ = ["HeuristicPlanner", "PlannerIntegrityError"]
