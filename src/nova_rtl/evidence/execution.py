"""Durable M3 evidence-graph and opportunity-stage orchestration."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

import zstandard
from pydantic import Field, model_validator

from nova_rtl import __version__
from nova_rtl.adapters.base import AdapterParseContext, metric_set
from nova_rtl.adapters.opensta import parse_critical_paths
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_digest, replay_run
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.baseline.execution import _append_stage_event, _persist_progress
from nova_rtl.baseline.flow import BaselineRunIndex, load_run_index
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory, EvidenceGraphSnapshot
from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    HashRef,
    StageInputHashes,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.execution import Stage, StageResult
from nova_rtl.contracts.manifest import DesignContract, ProjectManifest
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.evidence.graph import (
    EvidenceGraphBuildInputs,
    EvidenceGraphDocument,
    build_evidence_graph,
)
from nova_rtl.evidence.models import (
    AnalysisViewEvidenceIdentity,
    PathCluster,
    build_evidence_input_identity,
)
from nova_rtl.evidence.opportunities import (
    CAUSE_TO_TRANSFORM,
    EvidenceGraphView,
    RankedOpportunitySet,
    default_opportunity_policy,
    rank_opportunities,
)
from nova_rtl.evidence.paths import cluster_paths
from nova_rtl.evidence.source_map import build_source_map, enrich_critical_paths
from nova_rtl.orchestrator.state import RunOrchestrator

_M3_STAGES = frozenset({"evidence", "opportunities"})
_EVIDENCE_STAGE_ID = "stage_evidence_graph"
_OPPORTUNITY_STAGE_ID = "stage_opportunity_formation"


class EvidenceExecutionError(RuntimeError):
    """M2 evidence cannot be converted into a trustworthy M3 result."""


class PathClusterCollection(StrictContract):
    """Canonical immutable path-cluster artifact."""

    schema_version: Literal[1] = 1
    evidence_snapshot_hash: HashRef
    clusters: tuple[PathCluster, ...] = Field(min_length=1)
    collection_hash: HashRef

    @model_validator(mode="after")
    def collection_is_ordered_and_self_hashed(self) -> Self:
        ids = tuple(item.cluster_id for item in self.clusters)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("path clusters must be unique and canonically ordered")
        if {item.evidence_snapshot_hash for item in self.clusters} != {
            self.evidence_snapshot_hash
        }:
            raise ValueError("path clusters must bind one evidence snapshot")
        expected = canonical_sha256(self, exclude=frozenset({"collection_hash"}))
        if self.collection_hash != expected:
            raise ValueError("collection_hash does not match path clusters")
        return self


class M3EvidenceAnalysis(StrictContract):
    """Compact result returned by the M3 CLI execution boundary."""

    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    run_id: EntityId
    evidence_snapshot_hash: HashRef
    ranking_hash: HashRef
    path_count: int = Field(strict=True, gt=0)
    cluster_count: int = Field(strict=True, gt=0)
    opportunity_count: int = Field(strict=True, gt=0)
    editable_opportunity_count: int = Field(strict=True, gt=0)
    stage_result_ids: tuple[EntityId, EntityId]
    reused_stage_result_ids: tuple[EntityId, ...]
    replay_digest_before: HashRef
    replay_digest_after: HashRef


def normalize_evidence_stages(stages: Iterable[str]) -> tuple[str, str]:
    """Validate the indivisible M3 evidence-stage set."""

    selected = frozenset(item.strip().lower() for item in stages if item.strip())
    unknown = sorted(selected - _M3_STAGES)
    if unknown:
        raise EvidenceExecutionError(
            f"unknown M3 stage aliases: {', '.join(unknown)}"
        )
    missing = sorted(_M3_STAGES - selected)
    if missing:
        raise EvidenceExecutionError(
            f"M3 evidence requires a complete stage set; missing: {', '.join(missing)}"
        )
    return ("evidence", "opportunities")


def _load_contract(store: ArtifactStore, reference: ArtifactRef, model):  # type: ignore[no-untyped-def]
    try:
        return model.model_validate_json(store.open_verified(reference).read())
    except (ArtifactStoreError, ValueError) as error:
        raise EvidenceExecutionError(
            f"invalid evidence input artifact: {reference.artifact_id}"
        ) from error


def _raw(result: StageResult, suffix: str) -> ArtifactRef:
    matches = tuple(
        item for item in result.raw_artifacts if item.artifact_id.endswith(suffix)
    )
    if len(matches) != 1:
        raise EvidenceExecutionError(
            f"stage {result.stage_result_id} must preserve exactly one {suffix} artifact"
        )
    return matches[0]


def _required_result(
    index: BaselineRunIndex,
    store: ArtifactStore,
    stage_id: str,
    *,
    allow_timing_violation: bool = False,
) -> tuple[StageResult, ArtifactRef]:
    reference = index.stage_result_artifacts.get(stage_id)
    if reference is None:
        raise EvidenceExecutionError(f"completed M2 run lacks {stage_id}")
    result = _load_contract(store, reference, StageResult)
    if result.stage_result_id != stage_id:
        raise EvidenceExecutionError(f"stage identity differs for {stage_id}")
    if result.status != "PASS":
        timing_codes = {item.code for item in result.diagnostics}
        accepted = allow_timing_violation and bool(
            timing_codes & {"TIMING_SETUP_VIOLATION", "TIMING_HOLD_VIOLATION"}
        )
        complete = (
            result.metrics.setup_wns_ns is not None
            and result.metrics.setup_tns_ns is not None
        ) or (
            result.metrics.hold_wns_ns is not None
            and result.metrics.hold_tns_ns is not None
        )
        if not (accepted and complete):
            raise EvidenceExecutionError(f"required M2 stage is not usable: {stage_id}")
    for raw in result.raw_artifacts:
        store.open_verified(raw).close()
    return result, reference


def _stage_artifact(
    store: ArtifactStore,
    *,
    stage_id: str,
    suffix: str,
    data: bytes,
    media_type: str,
    classification: str = "INTERNAL",
) -> ArtifactRef:
    return store.put_named_bytes(
        data,
        artifact_id=f"{stage_id}_{suffix}",
        media_type=media_type,
        classification=classification,
        producer_stage_result_id=stage_id,
    )


def _implementation_fingerprint(stage: str, source_paths: Sequence[Path]) -> ToolFingerprint:
    executable = Path(sys.executable).resolve(strict=True)
    sources = {
        str(path.as_posix()): f"sha256:{sha256(path.read_bytes()).hexdigest()}"
        for path in sorted(source_paths, key=lambda item: item.as_posix())
    }
    return ToolFingerprint(
        tool_id="nova_rtl",
        executable=str(executable),
        version=__version__,
        version_args=("--version",),
        executable_sha256=f"sha256:{sha256(executable.read_bytes()).hexdigest()}",
        build_hash=canonical_sha256(sources),
        adapter_version=f"{stage}-v1",
    )


def _stage_result(
    *,
    index: BaselineRunIndex,
    store: ArtifactStore,
    stage_id: str,
    stage: Stage,
    fingerprint: ToolFingerprint,
    input_hashes: StageInputHashes,
    raw_artifacts: tuple[ArtifactRef, ...],
    started_at: datetime,
) -> tuple[StageResult, ArtifactRef]:
    ended_at = datetime.now(UTC)
    context = AdapterParseContext(
        stage=stage,
        analysis_view_id=None,
        runtime_ms=max(0, round((ended_at - started_at).total_seconds() * 1000)),
        evidence_artifact_id=raw_artifacts[0].artifact_id,
    )
    result = StageResult(
        stage_result_id=stage_id,
        run_id=index.run_id,
        candidate_id=index.candidate_id,
        stage=stage,
        analysis_view_id=None,
        status="PASS",
        tool_fingerprint=fingerprint,
        input_hashes=input_hashes,
        metrics=metric_set(context),
        diagnostics=(),
        raw_artifacts=raw_artifacts,
        started_at=started_at,
        ended_at=ended_at,
    )
    reference = _stage_artifact(
        store,
        stage_id=stage_id,
        suffix="stage_result",
        data=canonical_json_bytes(result),
        media_type="application/json",
    )
    return result, reference


def _cluster_collection(
    snapshot_hash: str, clusters: Sequence[PathCluster]
) -> PathClusterCollection:
    ordered = tuple(sorted(clusters, key=lambda item: item.cluster_id))
    payload = {
        "schema_version": 1,
        "evidence_snapshot_hash": snapshot_hash,
        "clusters": tuple(item.model_dump(mode="json") for item in ordered),
    }
    return PathClusterCollection(
        evidence_snapshot_hash=snapshot_hash,
        clusters=ordered,
        collection_hash=canonical_sha256(payload),
    )


def _clock_maps(clock_inventory: ClockInventory) -> tuple[dict[str, str], dict[str, str]]:
    clocks = (*clock_inventory.master_clocks, *clock_inventory.generated_clocks)
    names: dict[str, str] = {}
    domains: dict[str, str] = {}
    for clock in clocks:
        names[clock.clock_id] = clock.clock_id
        names[clock.source_object.rsplit("/", 1)[-1]] = clock.clock_id
        domains[clock.clock_id] = clock.domain_id
    return names, domains


def _m3_sources(*names: str) -> tuple[Path, ...]:
    package = Path(__file__).resolve().parent
    return tuple(package / name for name in names)


def analyze_evidence_run(
    run_directory: Path,
    *,
    requested_stages: Iterable[str],
) -> M3EvidenceAnalysis:
    """Build or reuse both M3 stages from one complete M2 run."""

    normalize_evidence_stages(requested_stages)
    resolved = run_directory.resolve(strict=True)
    index = load_run_index(resolved)
    if index.status != "PASS":
        raise EvidenceExecutionError("M3 evidence requires a completed PASS M2 run")
    store = ArtifactStore(resolved / "artifacts")
    project = _load_contract(store, index.project_manifest_artifact, ProjectManifest)
    design = _load_contract(store, index.design_contract_artifact, DesignContract)

    existing_results = {
        stage_id: _load_contract(store, reference, StageResult)
        for stage_id, reference in index.stage_result_artifacts.items()
    }
    for stage_id, result in existing_results.items():
        if result.stage_result_id != stage_id:
            raise EvidenceExecutionError(f"stage identity differs for {stage_id}")
        for artifact in result.raw_artifacts:
            store.open_verified(artifact).close()

    yosys, _ = _required_result(index, store, "stage_yosys")
    binding, _ = _required_result(index, store, "stage_binding")
    clock_result, _ = _required_result(index, store, "stage_clock")
    cdc_result, _ = _required_result(index, store, "stage_cdc")
    clock_ref = _raw(clock_result, "_contract")
    cdc_ref = _raw(cdc_result, "_contract")
    binding_ref = _raw(binding, "_contract")
    clock_inventory = _load_contract(store, clock_ref, ClockInventory)
    cdc_inventory = _load_contract(store, cdc_ref, CDCInventory)
    if clock_inventory.candidate_id != index.candidate_id:
        raise EvidenceExecutionError("clock inventory candidate differs from run")
    if cdc_inventory.candidate_id != index.candidate_id:
        raise EvidenceExecutionError("CDC inventory candidate differs from run")

    design_json_ref = _raw(yosys, "mapped_design_json")
    mapped_netlist_ref = _raw(yosys, "mapped_netlist_v")
    clock_ids_by_name, clock_domains = _clock_maps(clock_inventory)
    paths = []
    view_identities = []
    for view in sorted(index.analysis_views, key=lambda item: item.analysis_view_id):
        stage_id = f"stage_opensta_{view.analysis_view_id}"
        sta, sta_ref = _required_result(
            index,
            store,
            stage_id,
            allow_timing_violation=True,
        )
        stdout = _raw(sta, "_stdout")
        report = store.open_verified(stdout).read().decode("utf-8")
        paths.extend(
            parse_critical_paths(
                report,
                candidate_id=index.candidate_id,
                analysis_view_id=view.analysis_view_id,
                raw_report_artifact_id=stdout.artifact_id,
                clock_ids_by_name=clock_ids_by_name,
            )
        )
        view_identities.append(
            AnalysisViewEvidenceIdentity(
                analysis_view_id=view.analysis_view_id,
                analysis_view_hash=canonical_sha256(view),
                opensta_stage_result_hash=sta_ref.sha256,
            )
        )

    requested_names = tuple(
        sorted(
            {
                name
                for path in paths
                for name in (*path.object_sequence, path.startpoint, path.endpoint)
            }
        )
    )
    try:
        mapped_design = json.loads(store.open_verified(design_json_ref).read())
        mapped_netlist = store.open_verified(mapped_netlist_ref).read().decode("utf-8")
        rtl_bundle = store.open_verified(index.rtl_bundle_artifact).read().decode("utf-8")
    except (ArtifactStoreError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceExecutionError("synthesis or RTL evidence is malformed") from error
    source_map = build_source_map(
        mapped_design,
        mapped_netlist=mapped_netlist,
        rtl_bundle=rtl_bundle,
        candidate_id=index.candidate_id,
        rtl_snapshot_hash=index.benchmark_snapshot_hash,
        synthesis_structure_hash=design_json_ref.sha256,
        protected_modules=project.protection.modules,
        protected_path_patterns=project.protection.path_patterns,
        requested_object_names=requested_names,
    )
    enriched_paths = enrich_critical_paths(paths, source_map)
    protection_hash = canonical_sha256(project.protection)
    identity = build_evidence_input_identity(
        candidate_id=index.candidate_id,
        rtl_snapshot_hash=index.benchmark_snapshot_hash,
        design_contract_hash=index.design_contract_hash,
        constraint_binding_hash=binding_ref.sha256,
        platform_lock_hash=index.platform_lock_hash,
        synthesis_structure_hash=design_json_ref.sha256,
        clock_inventory_hash=clock_inventory.clock_graph_hash,
        cdc_inventory_hash=cdc_inventory.inventory_hash,
        protection_policy_hash=protection_hash,
        analysis_views=view_identities,
    )

    ledger = ExperimentLedger(resolved / "experiment-ledger.sqlite3", artifact_store=store)
    orchestrator = RunOrchestrator(
        ledger=ledger,
        artifact_store=store,
        policy_hash=design.effective_policy_hash,
    )
    stage_refs = dict(index.stage_result_artifacts)
    stage_hashes = {
        stage_id: result.input_hashes for stage_id, result in existing_results.items()
    }
    reused: list[str] = []

    evidence_recipe_payload = {
        "operation": "BUILD_EVIDENCE_GRAPH",
        "implementation": {
            path.name: f"sha256:{sha256(path.read_bytes()).hexdigest()}"
            for path in _m3_sources("execution.py", "graph.py", "source_map.py")
        },
    }
    evidence_recipe = _stage_artifact(
        store,
        stage_id=_EVIDENCE_STAGE_ID,
        suffix="recipe",
        data=canonical_json_bytes(evidence_recipe_payload),
        media_type="application/json",
    )
    evidence_hashes = StageInputHashes(
        rtl_snapshot=index.benchmark_snapshot_hash,
        design_contract=index.design_contract_hash,
        constraints=index.constraint_contract_artifact.sha256,
        constraint_binding=binding_ref.sha256,
        analysis_view=None,
        power_activity=None,
        platform_lock=index.platform_lock_hash,
        tool_recipe=evidence_recipe.sha256,
        formal_model=None,
        parent_stage_result=None,
        extensions={
            "analysis_view_set": canonical_sha256(
                {
                    "analysis_views": tuple(
                        item.model_dump(mode="json") for item in view_identities
                    )
                }
            ),
            "cdc_inventory": cdc_inventory.inventory_hash,
            "clock_inventory": clock_inventory.clock_graph_hash,
            "critical_path_records": canonical_sha256(
                {
                    "critical_paths": tuple(
                        item.model_dump(mode="json") for item in enriched_paths
                    )
                }
            ),
            "protection_policy": protection_hash,
            "synthesis_structure": design_json_ref.sha256,
        },
    )
    stage_hashes[_EVIDENCE_STAGE_ID] = evidence_hashes
    cached_evidence_ref = stage_refs.get(_EVIDENCE_STAGE_ID)
    cached_evidence = (
        _load_contract(store, cached_evidence_ref, StageResult)
        if cached_evidence_ref is not None
        else None
    )
    if cached_evidence is not None and RunOrchestrator.can_reuse(
        cached_evidence, evidence_hashes
    ):
        evidence_snapshot = _load_contract(
            store,
            _raw(cached_evidence, "_snapshot"),
            EvidenceGraphSnapshot,
        )
        reused.append(_EVIDENCE_STAGE_ID)
        evidence_result_ref = cached_evidence_ref
    else:
        started = datetime.now(UTC)
        evidence_snapshot = build_evidence_graph(
            EvidenceGraphBuildInputs(
                input_identity=identity,
                source_map=source_map,
                critical_paths=enriched_paths,
                clock_inventory=clock_inventory,
                cdc_inventory=cdc_inventory,
            ),
            artifact_store=store,
            producer_stage_result_id=_EVIDENCE_STAGE_ID,
        )
        snapshot_ref = _stage_artifact(
            store,
            stage_id=_EVIDENCE_STAGE_ID,
            suffix="snapshot",
            data=canonical_json_bytes(evidence_snapshot),
            media_type="application/json",
        )
        stdout = _stage_artifact(
            store,
            stage_id=_EVIDENCE_STAGE_ID,
            suffix="stdout",
            data=b"NOVA_EVIDENCE_GRAPH_PASS\n",
            media_type="text/plain",
        )
        stderr = _stage_artifact(
            store,
            stage_id=_EVIDENCE_STAGE_ID,
            suffix="stderr",
            data=b"",
            media_type="text/plain",
        )
        invocation = _stage_artifact(
            store,
            stage_id=_EVIDENCE_STAGE_ID,
            suffix="invocation",
            data=canonical_json_bytes(
                {
                    "operation": "BUILD_EVIDENCE_GRAPH",
                    "input_hashes": evidence_hashes.model_dump(mode="json"),
                }
            ),
            media_type="application/json",
        )
        evidence_result, evidence_result_ref = _stage_result(
            index=index,
            store=store,
            stage_id=_EVIDENCE_STAGE_ID,
            stage="EVIDENCE_GRAPH",
            fingerprint=_implementation_fingerprint(
                "evidence", _m3_sources("execution.py", "graph.py", "source_map.py")
            ),
            input_hashes=evidence_hashes,
            raw_artifacts=(
                snapshot_ref,
                evidence_snapshot.graph_artifact,
                evidence_snapshot.source_map_artifact,
                evidence_snapshot.path_record_artifact,
                evidence_recipe,
                invocation,
                stdout,
                stderr,
            ),
            started_at=started,
        )
        _append_stage_event(
            ledger,
            index,
            evidence_result,
            evidence_result_ref,
            design.effective_policy_hash,
        )
        stage_refs[_EVIDENCE_STAGE_ID] = evidence_result_ref
        index = _persist_progress(
            resolved, index, stage_refs, stage_hashes, status="PASS"
        )

    try:
        graph_bytes = store.open_verified(evidence_snapshot.graph_artifact).read()
        document = EvidenceGraphDocument.model_validate_json(
            zstandard.ZstdDecompressor().decompress(graph_bytes)
        )
    except (ArtifactStoreError, ValueError, zstandard.ZstdError) as error:
        raise EvidenceExecutionError("compressed evidence graph is invalid") from error
    graph = EvidenceGraphView(snapshot=evidence_snapshot, document=document)
    clusters = cluster_paths(
        enriched_paths,
        evidence_snapshot_hash=evidence_snapshot.snapshot_hash,
        source_map=source_map,
        clock_domain_by_id=clock_domains,
    )
    cluster_collection = _cluster_collection(evidence_snapshot.snapshot_hash, clusters)
    policy = default_opportunity_policy()

    opportunity_recipe_payload = {
        "operation": "FORM_AND_RANK_OPPORTUNITIES",
        "implementation": {
            path.name: f"sha256:{sha256(path.read_bytes()).hexdigest()}"
            for path in _m3_sources("execution.py", "opportunities.py", "paths.py")
        },
    }
    opportunity_recipe = _stage_artifact(
        store,
        stage_id=_OPPORTUNITY_STAGE_ID,
        suffix="recipe",
        data=canonical_json_bytes(opportunity_recipe_payload),
        media_type="application/json",
    )
    opportunity_hashes = StageInputHashes(
        rtl_snapshot=index.benchmark_snapshot_hash,
        design_contract=index.design_contract_hash,
        constraints=index.constraint_contract_artifact.sha256,
        constraint_binding=binding_ref.sha256,
        analysis_view=None,
        power_activity=None,
        platform_lock=index.platform_lock_hash,
        tool_recipe=opportunity_recipe.sha256,
        formal_model=None,
        parent_stage_result=evidence_result_ref.sha256,
        extensions={
            "evidence_graph": evidence_snapshot.snapshot_hash,
            "protection_policy": policy.policy_hash,
            "transform_registry": canonical_sha256(dict(sorted(CAUSE_TO_TRANSFORM.items()))),
        },
    )
    stage_hashes[_OPPORTUNITY_STAGE_ID] = opportunity_hashes
    cached_opportunity_ref = stage_refs.get(_OPPORTUNITY_STAGE_ID)
    cached_opportunity = (
        _load_contract(store, cached_opportunity_ref, StageResult)
        if cached_opportunity_ref is not None
        else None
    )
    if cached_opportunity is not None and RunOrchestrator.can_reuse(
        cached_opportunity, opportunity_hashes
    ):
        ranked = _load_contract(
            store,
            _raw(cached_opportunity, "_ranked_opportunities"),
            RankedOpportunitySet,
        )
        reused.append(_OPPORTUNITY_STAGE_ID)
    else:
        started = datetime.now(UTC)
        ranked = rank_opportunities(clusters, graph, policy)
        cluster_ref = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="path_clusters",
            data=canonical_json_bytes(cluster_collection),
            media_type="application/json",
        )
        policy_ref = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="policy",
            data=canonical_json_bytes(policy),
            media_type="application/json",
        )
        ranked_ref = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="ranked_opportunities",
            data=canonical_json_bytes(ranked),
            media_type="application/json",
        )
        stdout = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="stdout",
            data=b"NOVA_OPPORTUNITY_FORMATION_PASS\n",
            media_type="text/plain",
        )
        stderr = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="stderr",
            data=b"",
            media_type="text/plain",
        )
        invocation = _stage_artifact(
            store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            suffix="invocation",
            data=canonical_json_bytes(
                {
                    "operation": "FORM_AND_RANK_OPPORTUNITIES",
                    "input_hashes": opportunity_hashes.model_dump(mode="json"),
                }
            ),
            media_type="application/json",
        )
        opportunity_result, opportunity_result_ref = _stage_result(
            index=index,
            store=store,
            stage_id=_OPPORTUNITY_STAGE_ID,
            stage="OPPORTUNITY_FORMATION",
            fingerprint=_implementation_fingerprint(
                "opportunity",
                _m3_sources("execution.py", "opportunities.py", "paths.py"),
            ),
            input_hashes=opportunity_hashes,
            raw_artifacts=(
                ranked_ref,
                cluster_ref,
                policy_ref,
                opportunity_recipe,
                invocation,
                stdout,
                stderr,
            ),
            started_at=started,
        )
        _append_stage_event(
            ledger,
            index,
            opportunity_result,
            opportunity_result_ref,
            design.effective_policy_hash,
        )
        stage_refs[_OPPORTUNITY_STAGE_ID] = opportunity_result_ref
        index = _persist_progress(
            resolved, index, stage_refs, stage_hashes, status="PASS"
        )

    current = orchestrator.get_state(index.run_id)
    if current is not None and current.state == "EVIDENCE_BUILD":
        orchestrator.transition(
            index.run_id,
            "EVIDENCE_BUILD",
            "SEARCHING",
            evidence_snapshot,
        )
    first_digest = replay_digest(replay_run(ledger, index.run_id))
    second_digest = replay_digest(replay_run(ledger, index.run_id))
    editable = sum(item.editability == "RTL_EDITABLE" for item in ranked.opportunities)
    if editable == 0:
        raise EvidenceExecutionError("M3 produced no executable RTL optimization opportunity")
    return M3EvidenceAnalysis(
        status="PASS",
        run_id=index.run_id,
        evidence_snapshot_hash=evidence_snapshot.snapshot_hash,
        ranking_hash=ranked.ranking_hash,
        path_count=len(enriched_paths),
        cluster_count=len(clusters),
        opportunity_count=len(ranked.opportunities),
        editable_opportunity_count=editable,
        stage_result_ids=(_EVIDENCE_STAGE_ID, _OPPORTUNITY_STAGE_ID),
        reused_stage_result_ids=tuple(sorted(reused)),
        replay_digest_before=first_digest,
        replay_digest_after=second_digest,
    )


__all__ = [
    "EvidenceExecutionError",
    "M3EvidenceAnalysis",
    "PathClusterCollection",
    "analyze_evidence_run",
    "normalize_evidence_stages",
]
