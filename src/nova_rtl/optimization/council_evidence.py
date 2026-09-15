"""Immutable persistence and replay for advisory council outputs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from nova_rtl.contracts.base import canonical_json_bytes
from nova_rtl.contracts.optimization import OptimizationProposal
from nova_rtl.contracts.planning import (
    CouncilRequest,
    CouncilResult,
    CouncilTrace,
    PlannerResult,
)
from nova_rtl.planner.council import CouncilPlanner, CouncilPlanningError


def _publish_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise CouncilPlanningError(f"immutable council evidence differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def publish_council_evidence(
    output_directory: Path,
    *,
    planner: CouncilPlanner,
    planner_result: PlannerResult,
) -> Path:
    """Persist replayable council contracts without exposing private artifact bytes."""

    if planner_result.council_result_id != planner.council_result.council_result_id:
        raise CouncilPlanningError("planner and council result identities differ")
    output = output_directory.resolve()
    proposals = planner.proposals_for(planner_result)
    files = {
        "council-request.json": canonical_json_bytes(planner.council_request) + b"\n",
        "council-result.json": canonical_json_bytes(planner.council_result) + b"\n",
        "council-trace.json": canonical_json_bytes(planner.council_trace) + b"\n",
        "planner-result.json": canonical_json_bytes(planner_result) + b"\n",
        "proposals.json": canonical_json_bytes(
            {"proposals": tuple(item.model_dump(mode="json") for item in proposals)}
        )
        + b"\n",
    }
    for name, content in files.items():
        _publish_immutable(output / name, content)
    verify_council_evidence(output)
    return output / "council-result.json"


def verify_council_evidence(output_directory: Path) -> CouncilResult:
    """Reconstruct council lineage solely from immutable persisted contracts."""

    root = output_directory.resolve(strict=True)
    try:
        request = CouncilRequest.model_validate_json(
            (root / "council-request.json").read_bytes()
        )
        result = CouncilResult.model_validate_json(
            (root / "council-result.json").read_bytes()
        )
        trace = CouncilTrace.model_validate_json(
            (root / "council-trace.json").read_bytes()
        )
        planner_result = PlannerResult.model_validate_json(
            (root / "planner-result.json").read_bytes()
        )
        proposal_payload = yaml.safe_load((root / "proposals.json").read_text())
        proposals = tuple(
            OptimizationProposal.model_validate(item)
            for item in proposal_payload["proposals"]
        )
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        raise CouncilPlanningError("council evidence is missing or invalid") from error
    if (
        result.council_request_id != request.council_request_id
        or result.council_trace_id != trace.council_trace_id
        or planner_result.council_result_id != result.council_result_id
        or tuple(item.proposal_id for item in proposals) != planner_result.proposal_ids
        or result.final_ordered_proposal_ids != planner_result.proposal_ids
    ):
        raise CouncilPlanningError("persisted council evidence lineage differs")
    return result


__all__ = ["publish_council_evidence", "verify_council_evidence"]
