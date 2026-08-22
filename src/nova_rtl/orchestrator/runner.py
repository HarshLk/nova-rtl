"""Deterministic resume planning for interrupted NOVA-RTL runs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, ValidationError

from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    StageInputHashes,
    StrictContract,
    canonical_json_bytes,
)
from nova_rtl.contracts.execution import StageResult
from nova_rtl.orchestrator.state import RunOrchestrator


class ResumePlanError(RuntimeError):
    """An interrupted run cannot be planned safely from its persisted identity."""


class ResumeStage(StrictContract):
    stage_id: EntityId
    cached_stage_result_artifact: ArtifactRef
    requested_input_hashes: StageInputHashes


class InterruptedRunManifest(StrictContract):
    schema_version: int = Field(default=1, strict=True, ge=1, le=1)
    stages: tuple[ResumeStage, ...] = Field(min_length=1)


def plan_resume(run_directory: Path) -> dict[str, list[str]]:
    """Classify stages without executing tools or invoking a model."""

    manifest_path = run_directory.resolve() / "resume-manifest.json"
    try:
        manifest = InterruptedRunManifest.model_validate_json(manifest_path.read_bytes())
    except OSError as error:
        raise ResumePlanError(f"resume manifest cannot be read: {manifest_path}") from error
    except ValidationError as error:
        raise ResumePlanError(f"resume manifest is invalid: {manifest_path}") from error
    stage_ids = tuple(stage.stage_id for stage in manifest.stages)
    if len(stage_ids) != len(set(stage_ids)):
        raise ResumePlanError("resume manifest stage IDs must be unique")
    try:
        store = ArtifactStore.open_existing(run_directory.resolve() / "artifacts")
    except ArtifactStoreError as error:
        raise ResumePlanError("interrupted run artifact store is unavailable") from error
    reusable: list[str] = []
    rerun: list[str] = []
    for stage in manifest.stages:
        cached = _load_verified_stage_result(store, stage)
        destination = rerun
        if cached is not None and RunOrchestrator.can_reuse(
            cached, stage.requested_input_hashes
        ):
            destination = reusable
        destination.append(stage.stage_id)
    return {"rerun": rerun, "reusable": reusable}


def _load_verified_stage_result(
    store: ArtifactStore, stage: ResumeStage
) -> StageResult | None:
    reference = stage.cached_stage_result_artifact
    if reference.media_type != "application/json":
        return None
    try:
        encoded = store.open_verified(reference).read()
        result = StageResult.model_validate_json(encoded)
        if canonical_json_bytes(result) != encoded:
            return None
        if result.stage_result_id != stage.stage_id:
            return None
        for artifact in result.raw_artifacts:
            store.open_verified(artifact).close()
    except (ArtifactStoreError, ValueError):
        return None
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.resume or not arguments.dry_run:
        parser.error("this M1 runner supports only --resume --dry-run")
    try:
        result = plan_resume(arguments.run_directory)
    except ResumePlanError as error:
        parser.exit(2, f"resume planning failed: {error}\n")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess exercised
    raise SystemExit(main())


__all__ = [
    "InterruptedRunManifest",
    "ResumePlanError",
    "ResumeStage",
    "main",
    "plan_resume",
]
