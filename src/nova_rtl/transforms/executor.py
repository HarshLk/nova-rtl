"""Deterministic, isolated execution of registered RTL transforms."""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.optimization import CandidateRecord, OptimizationProposal
from nova_rtl.evidence.models import SourceSpanRecord
from nova_rtl.transforms.priority_mux import PriorityMuxContext, PriorityMuxMatch
from nova_rtl.transforms.registry import TransformRegistry
from nova_rtl.transforms.syntax import (
    AppliedSourceEdit,
    ParsedSlangAst,
    SyntaxBackendError,
    apply_authorized_edit,
)


class TransformExecutionError(RuntimeError):
    """A proposal cannot be deterministically materialized."""


class UnauthorizedDiffError(TransformExecutionError):
    """A transform attempted to modify content outside its exact authorization."""


@dataclass(frozen=True, slots=True)
class MaterializationRequest:
    """Runtime inputs binding one proposal to its parent snapshot and source facts."""

    run_id: str
    parent_candidate_id: str
    parent_root: Path
    source_paths: tuple[str, ...]
    proposal: OptimizationProposal
    authorized_span: SourceSpanRecord
    protected_spans: tuple[SourceSpanRecord, ...]
    parsed_ast: ParsedSlangAst
    transform_context: PriorityMuxContext
    lineage_depth: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MaterializedCandidate:
    """New immutable candidate record plus its read-only isolated workspace."""

    candidate: CandidateRecord
    workspace: Path
    applied_edit: AppliedSourceEdit


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise TransformExecutionError("source path must be normalized and relative")
    return value


def _read_sources(root: Path, source_paths: Sequence[str]) -> dict[str, str]:
    resolved_root = root.resolve()
    sources: dict[str, str] = {}
    for relative_path in sorted(source_paths):
        normalized = _normalized_path(relative_path)
        path = (resolved_root / normalized).resolve()
        if resolved_root not in path.parents or path.is_symlink() or not path.is_file():
            raise TransformExecutionError(
                f"source is missing, linked, or escapes root: {normalized}"
            )
        try:
            sources[normalized] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise TransformExecutionError(
                f"cannot read UTF-8 source {normalized}: {error}"
            ) from error
    if not sources or len(sources) != len(source_paths):
        raise TransformExecutionError("source path set must be nonempty and unique")
    return sources


def _snapshot_bytes(sources: dict[str, str]) -> bytes:
    files = tuple(
        {
            "relative_path": path,
            "sha256": "sha256:" + sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
        }
        for path, text in sorted(sources.items())
    )
    return canonical_json_bytes({"schema_version": 1, "files": files})


def _patch_bytes(before: dict[str, str], after: dict[str, str]) -> bytes:
    lines: list[str] = []
    for path in sorted(before):
        if before[path] == after[path]:
            continue
        lines.extend(
            difflib.unified_diff(
                before[path].splitlines(),
                after[path].splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


class TransformExecutor:
    """Resolve, authorize, apply, fingerprint, and persist one registered rewrite."""

    def __init__(
        self,
        *,
        registry: TransformRegistry,
        artifact_store: ArtifactStore,
        candidates_root: Path,
    ) -> None:
        self._registry = registry
        self._store = artifact_store
        self._candidates_root = candidates_root.resolve()
        self._candidates_root.mkdir(parents=True, exist_ok=True)

    def materialize(self, request: MaterializationRequest) -> MaterializedCandidate:
        proposal = request.proposal
        if proposal.parent_candidate_id != request.parent_candidate_id:
            raise TransformExecutionError("proposal parent does not match materialization parent")
        if proposal.target.source_span_id != request.authorized_span.source_span_id:
            raise TransformExecutionError("proposal target does not match authorized source span")
        capability = self._registry.resolve(proposal.transformation.operation)
        metadata = capability.metadata
        if proposal.transformation.family != metadata.family:
            raise TransformExecutionError("proposal transform family does not match registry")
        if proposal.correctness.contract != metadata.correctness_contract:
            raise TransformExecutionError("proposal correctness contract does not match registry")
        parameters = self._registry.validate_parameters(
            proposal.transformation.operation, proposal.transformation.parameters
        )
        match = capability.match(request.transform_context)  # type: ignore[attr-defined]
        if not isinstance(match, PriorityMuxMatch):
            raise TransformExecutionError("M4 capability returned an unsupported match type")
        edit = capability.rewrite(match, parameters)  # type: ignore[attr-defined]
        fingerprint = capability.fingerprint(match, parameters)  # type: ignore[attr-defined]

        before = _read_sources(request.parent_root, request.source_paths)
        with tempfile.TemporaryDirectory(
            prefix=".materialize-", dir=self._candidates_root
        ) as temporary:
            staging = Path(temporary)
            for relative_path, source in before.items():
                destination = staging / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(source, encoding="utf-8")
            try:
                applied = apply_authorized_edit(
                    repository_root=staging,
                    parsed_ast=request.parsed_ast,
                    authorized_span=request.authorized_span,
                    protected_spans=request.protected_spans,
                    edit=edit,
                    allowed_ast_node_kinds=metadata.allowed_ast_node_kinds,
                )
            except SyntaxBackendError as error:
                raise UnauthorizedDiffError(str(error)) from error
            after = _read_sources(staging, request.source_paths)
            changed_paths = tuple(
                path for path in sorted(before) if before[path] != after[path]
            )
            if changed_paths != (request.authorized_span.relative_path,):
                raise UnauthorizedDiffError(
                    "transform must change exactly the one proposal-authorized source file"
                )
            snapshot_bytes = _snapshot_bytes(after)
            patch_bytes = _patch_bytes(before, after)
            if not patch_bytes:
                raise TransformExecutionError("transform produced an empty patch")
            snapshot_ref = self._store.put_bytes(
                snapshot_bytes,
                media_type="application/vnd.nova-rtl.rtl-snapshot+json",
                classification="RESTRICTED_RTL",
            )
            patch_ref = self._store.put_bytes(
                patch_bytes,
                media_type="text/x-diff",
                classification="RESTRICTED_RTL",
            )
            candidate_digest = canonical_sha256(
                {
                    "parent_candidate_id": request.parent_candidate_id,
                    "proposal": proposal.model_dump(mode="json"),
                    "source_hash": snapshot_ref.sha256,
                    "patch_hash": patch_ref.sha256,
                    "transform_fingerprint": fingerprint,
                }
            ).removeprefix("sha256:")
            candidate_id = f"cand_{candidate_digest[:24]}"
            workspace = self._candidates_root / candidate_id
            if workspace.exists():
                existing = _read_sources(workspace, request.source_paths)
                if _snapshot_bytes(existing) != snapshot_bytes:
                    raise TransformExecutionError(
                        "existing candidate workspace disagrees with content identity"
                    )
            else:
                for path in staging.rglob("*"):
                    if path.is_file():
                        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                os.replace(staging, workspace)

        candidate = CandidateRecord(
            candidate_id=candidate_id,
            run_id=request.run_id,
            parent_candidate_id=request.parent_candidate_id,
            lineage_depth=request.lineage_depth,
            proposal_id=proposal.proposal_id,
            opportunity_id=proposal.opportunity_id,
            rtl_snapshot_artifact=snapshot_ref,
            patch_artifact=patch_ref,
            source_hash=snapshot_ref.sha256,
            changed_spans=(request.authorized_span.source_span_id,),
            transform_fingerprint=fingerprint,
            required_correctness_contract=proposal.correctness.contract,
            stage_result_ids=(),
            per_view_metrics={},
            proof_result_id=None,
            binding_manifest_id=None,
            clock_inventory_id=None,
            cdc_inventory_id=None,
            hard_gate_summary=None,
            classification="INCONCLUSIVE",
            selection_class="PRIMARY_STRICT",
            terminal_disposition="PENDING_EVALUATION",
            created_at=request.created_at,
            recovery_parent_failure_id=None,
        )
        return MaterializedCandidate(
            candidate=candidate,
            workspace=workspace,
            applied_edit=applied,
        )


__all__ = [
    "MaterializationRequest",
    "MaterializedCandidate",
    "TransformExecutionError",
    "TransformExecutor",
    "UnauthorizedDiffError",
]
