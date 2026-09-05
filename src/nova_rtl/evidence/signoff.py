"""Fail-closed M3 evidence and opportunity sign-off."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from hashlib import sha256
from pathlib import Path

import zstandard

from nova_rtl.adapters.opensta import parse_critical_paths
from nova_rtl.analysis_views.aggregation import is_complete_measured_timing_violation
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_digest, replay_run
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.baseline.flow import BaselineFlowError, load_run_index
from nova_rtl.contracts.analysis import (
    CDCInventory,
    ClockInventory,
    EvidenceGraphSnapshot,
    M3SignoffReport,
)
from nova_rtl.contracts.base import ArtifactRef, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.benchmark import M2SignoffReport
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.manifest import DesignContract, ProjectManifest
from nova_rtl.evidence.execution import (
    EvidenceExecutionError,
    PathClusterCollection,
    _critical_path_report_hash,
    _evidence_sources,
    _implementation_fingerprint,
    _opportunity_sources,
    validate_m3_run_boundary,
)
from nova_rtl.evidence.graph import (
    CriticalPathCollection,
    EvidenceGraphBuildInputs,
    EvidenceGraphDocument,
    build_evidence_graph,
)
from nova_rtl.evidence.models import (
    AnalysisViewEvidenceIdentity,
    build_evidence_input_identity,
)
from nova_rtl.evidence.opportunities import (
    CAUSE_TO_TRANSFORM,
    EvidenceGraphView,
    OpportunityPolicy,
    RankedOpportunitySet,
    opportunity_policy_from_project,
    rank_opportunities,
)
from nova_rtl.evidence.paths import cluster_paths
from nova_rtl.evidence.source_map import (
    SourceMapSnapshot,
    build_source_map,
    enrich_critical_paths,
)

_EVIDENCE_STAGE_ID = "stage_evidence_graph"
_OPPORTUNITY_STAGE_ID = "stage_opportunity_formation"


class M3SignoffError(BaselineFlowError):
    """The evidence graph and opportunity packet cannot be trusted."""


def _hash_bytes(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


def _git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise M3SignoffError("Git is required for M3 sign-off")
    return Path(executable).resolve(strict=True)


def _git_environment(executable: Path) -> dict[str, str]:
    return {
        "PATH": f"{executable.parent}:/usr/local/bin:/usr/bin:/bin",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LANG": "C",
        "LC_ALL": "C",
    }


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = _git_executable()
    completed = subprocess.run(
        (str(executable), "-C", str(repository_root), *arguments),
        env=_git_environment(executable),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise M3SignoffError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    if Path(__file__).resolve(strict=True) != root / "src/nova_rtl/evidence/signoff.py":
        raise M3SignoffError("loaded NOVA package is outside the requested NOVA repository")
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise M3SignoffError("requested NOVA repository is not the Git repository root")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M3SignoffError("M3 sign-off requires a clean final commit")
    commit = _git_output(root, "rev-parse", "--verify", "HEAD")
    tree = _git_output(root, "rev-parse", "--verify", "HEAD^{tree}")
    if any(
        len(value) != 40 or any(character not in "0123456789abcdef" for character in value)
        for value in (commit, tree)
    ):
        raise M3SignoffError("M3 sign-off could not resolve canonical Git identities")
    return commit, tree


def _require_checkpoint_unchanged(
    repository_root: Path, expected_commit: str, expected_tree: str
) -> None:
    commit, tree = _clean_checkpoint(repository_root)
    if commit != expected_commit or tree != expected_tree:
        raise M3SignoffError("repository checkpoint changed during M3 sign-off")


def _require_git_ancestor(
    repository_root: Path, ancestor: str, descendant: str
) -> None:
    executable = _git_executable()
    completed = subprocess.run(
        (
            str(executable),
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ),
        env=_git_environment(executable),
        shell=False,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise M3SignoffError("signed M2 checkpoint is not an ancestor of M3")


def _load_contract(store: ArtifactStore, reference: ArtifactRef, model):  # type: ignore[no-untyped-def]
    try:
        return model.model_validate_json(store.open_verified(reference).read())
    except (ArtifactStoreError, ValueError) as error:
        raise M3SignoffError(f"invalid artifact: {reference.artifact_id}") from error


def _raw(result: StageResult, suffix: str) -> ArtifactRef:
    matches = tuple(
        item for item in result.raw_artifacts if item.artifact_id.endswith(suffix)
    )
    if len(matches) != 1:
        raise M3SignoffError(
            f"stage {result.stage_result_id} must preserve exactly one {suffix} artifact"
        )
    return matches[0]


def _stage(
    index, store: ArtifactStore, stage_id: str  # type: ignore[no-untyped-def]
) -> tuple[StageResult, ArtifactRef]:
    reference = index.stage_result_artifacts.get(stage_id)
    if reference is None:
        raise M3SignoffError(f"full run is missing {stage_id}")
    result = _load_contract(store, reference, StageResult)
    if result.stage_result_id != stage_id or not (
        result.status == "PASS" or is_complete_measured_timing_violation(result)
    ):
        raise M3SignoffError(f"M3 stage is not a passing canonical result: {stage_id}")
    for artifact in result.raw_artifacts:
        store.open_verified(artifact).close()
    return result, reference


def _m2_dependency(
    report_path: Path,
    *,
    repository_root: Path,
    current_commit: str,
) -> tuple[M2SignoffReport, str]:
    try:
        content = report_path.resolve(strict=True).read_bytes()
        report = M2SignoffReport.model_validate_json(content)
    except (OSError, ValueError) as error:
        raise M3SignoffError("M2 sign-off packet is missing or invalid") from error
    _require_git_ancestor(repository_root, report.commit_sha, current_commit)
    tree = _git_output(
        repository_root, "rev-parse", "--verify", f"{report.commit_sha}^{{tree}}"
    )
    if tree != report.implementation_tree_hash:
        raise M3SignoffError("M2 packet does not match its recorded Git checkpoint")
    return report, _hash_bytes(content)


def _publish_report_atomic(destination: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    except OSError as error:
        raise M3SignoffError("failed to publish M3 sign-off report atomically") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _clock_maps(clock_inventory: ClockInventory) -> tuple[dict[str, str], dict[str, str]]:
    names: dict[str, str] = {}
    domains: dict[str, str] = {}
    for clock in (*clock_inventory.master_clocks, *clock_inventory.generated_clocks):
        names[clock.clock_id] = clock.clock_id
        names[clock.source_object.rsplit("/", 1)[-1]] = clock.clock_id
        domains[clock.clock_id] = clock.domain_id
    return names, domains


def _critical_path_collection(
    evidence_input_hash: str, records: tuple
) -> CriticalPathCollection:  # type: ignore[type-arg]
    ordered = tuple(sorted(records, key=lambda item: item.path_id))
    payload = {
        "schema_version": 1,
        "evidence_input_hash": evidence_input_hash,
        "records": tuple(item.model_dump(mode="json") for item in ordered),
    }
    return CriticalPathCollection(
        evidence_input_hash=evidence_input_hash,
        records=ordered,
        collection_hash=canonical_sha256(payload),
    )


def _reconstruct_evidence(
    *,
    index,  # type: ignore[no-untyped-def]
    store: ArtifactStore,
    project: ProjectManifest,
    evidence_result: StageResult,
    evidence_reference: ArtifactRef,
    opportunity_result: StageResult,
) -> tuple[
    EvidenceGraphSnapshot,
    SourceMapSnapshot,
    CriticalPathCollection,
    PathClusterCollection,
    OpportunityPolicy,
    RankedOpportunitySet,
]:
    yosys, _ = _stage(index, store, "stage_yosys")
    binding, _ = _stage(index, store, "stage_binding")
    clock_result, _ = _stage(index, store, "stage_clock")
    cdc_result, _ = _stage(index, store, "stage_cdc")
    binding_ref = _raw(binding, "_contract")
    clock_ref = _raw(clock_result, "_contract")
    cdc_ref = _raw(cdc_result, "_contract")
    clock_inventory = _load_contract(store, clock_ref, ClockInventory)
    cdc_inventory = _load_contract(store, cdc_ref, CDCInventory)
    design_json_ref = _raw(yosys, "mapped_design_json")
    netlist_ref = _raw(yosys, "mapped_netlist_v")
    clock_names, clock_domains = _clock_maps(clock_inventory)

    parsed_paths = []
    view_identities = []
    path_reports: list[tuple[str, str, str, str]] = []
    tns_by_view: dict[str, float] = {}
    wns_by_view: dict[str, float] = {}
    for view in sorted(index.analysis_views, key=lambda item: item.analysis_view_id):
        sta, sta_ref = _stage(index, store, f"stage_opensta_{view.analysis_view_id}")
        openroad, openroad_ref = _stage(
            index, store, f"stage_openroad_{view.analysis_view_id}"
        )
        physical_metrics = openroad.metrics
        required_physical = (
            physical_metrics.physical_area_um2,
            physical_metrics.wirelength_um,
            physical_metrics.congestion_overflow,
            physical_metrics.cell_count,
            physical_metrics.register_count,
            physical_metrics.buffer_count,
        )
        if any(value is None for value in required_physical):
            raise M3SignoffError(
                f"OpenROAD result lacks physical metrics for {view.analysis_view_id}"
            )
        stdout = _raw(sta, "_stdout")
        path_type = "MAX" if view.check == "SETUP" else "MIN"
        path_reports.append(
            (view.analysis_view_id, sta_ref.sha256, path_type, stdout.sha256)
        )
        parsed_paths.extend(
            parse_critical_paths(
                store.open_verified(stdout).read().decode("utf-8"),
                candidate_id=index.candidate_id,
                analysis_view_id=view.analysis_view_id,
                raw_report_artifact_id=stdout.artifact_id,
                clock_ids_by_name=clock_names,
                expected_path_type=path_type,
            )
        )
        measured_tns = (
            sta.metrics.setup_tns_ns if view.check == "SETUP" else sta.metrics.hold_tns_ns
        )
        if measured_tns is None:
            raise M3SignoffError(
                f"OpenSTA result lacks measured TNS for {view.analysis_view_id}"
            )
        tns_by_view[view.analysis_view_id] = measured_tns
        measured_wns = (
            sta.metrics.setup_wns_ns if view.check == "SETUP" else sta.metrics.hold_wns_ns
        )
        if measured_wns is None:
            raise M3SignoffError(
                f"OpenSTA result lacks measured WNS for {view.analysis_view_id}"
            )
        wns_by_view[view.analysis_view_id] = measured_wns
        view_identities.append(
            AnalysisViewEvidenceIdentity(
                analysis_view_id=view.analysis_view_id,
                analysis_view_hash=canonical_sha256(view),
                opensta_stage_result_hash=sta_ref.sha256,
                openroad_stage_result_hash=openroad_ref.sha256,
                openroad_metrics_hash=canonical_sha256(physical_metrics),
                physical_area_um2=physical_metrics.physical_area_um2,
                wirelength_um=physical_metrics.wirelength_um,
                congestion_overflow=physical_metrics.congestion_overflow,
                physical_cell_count=physical_metrics.cell_count,
                physical_register_count=physical_metrics.register_count,
                physical_buffer_count=physical_metrics.buffer_count,
            )
        )
    requested_names = tuple(
        sorted(
            {
                name
                for path in parsed_paths
                for name in (*path.object_sequence, path.startpoint, path.endpoint)
            }
        )
    )
    cdc_endpoints = tuple(
        sorted(
            {
                endpoint
                for crossing in cdc_inventory.crossings
                for endpoint in (crossing.source_object, crossing.destination_object)
            }
        )
    )
    try:
        mapped_design = json.loads(store.open_verified(design_json_ref).read())
        mapped_netlist = store.open_verified(netlist_ref).read().decode("utf-8")
        rtl_bundle = store.open_verified(index.rtl_bundle_artifact).read().decode("utf-8")
    except (ArtifactStoreError, UnicodeError, json.JSONDecodeError) as error:
        raise M3SignoffError("M3 synthesis or RTL evidence is malformed") from error
    expected_source_map = build_source_map(
        mapped_design,
        mapped_netlist=mapped_netlist,
        rtl_bundle=rtl_bundle,
        candidate_id=index.candidate_id,
        rtl_snapshot_hash=index.benchmark_snapshot_hash,
        synthesis_structure_hash=design_json_ref.sha256,
        protected_modules=project.protection.modules,
        protected_path_patterns=project.protection.path_patterns,
        requested_object_names=requested_names,
        requested_endpoint_names=cdc_endpoints,
    )
    enriched_paths = enrich_critical_paths(parsed_paths, expected_source_map)
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
    expected_paths = _critical_path_collection(identity.identity_hash, tuple(enriched_paths))

    snapshot = _load_contract(store, _raw(evidence_result, "_snapshot"), EvidenceGraphSnapshot)
    source_map = _load_contract(
        store, snapshot.source_map_artifact, SourceMapSnapshot
    )
    path_collection = _load_contract(
        store, snapshot.path_record_artifact, CriticalPathCollection
    )
    if source_map != expected_source_map or path_collection != expected_paths:
        raise M3SignoffError("source map or critical paths differ from raw M2 evidence")
    graph_inputs = EvidenceGraphBuildInputs(
        input_identity=identity,
        source_map=expected_source_map,
        critical_paths=enriched_paths,
        clock_inventory=clock_inventory,
        cdc_inventory=cdc_inventory,
    )
    try:
        compressed_graph = store.open_verified(snapshot.graph_artifact).read()
        document = EvidenceGraphDocument.model_validate_json(
            zstandard.ZstdDecompressor().decompress(
                compressed_graph, max_output_size=256 * 1024 * 1024
            )
        )
    except (ArtifactStoreError, ValueError, zstandard.ZstdError) as error:
        raise M3SignoffError("compressed evidence graph is invalid") from error
    with tempfile.TemporaryDirectory(prefix="nova-m3-reconstruct-") as temporary:
        expected_store = ArtifactStore(Path(temporary) / "artifacts")
        expected_snapshot = build_evidence_graph(
            graph_inputs,
            artifact_store=expected_store,
            producer_stage_result_id=_EVIDENCE_STAGE_ID,
        )
        expected_document = EvidenceGraphDocument.model_validate_json(
            zstandard.ZstdDecompressor().decompress(
                expected_store.open_verified(expected_snapshot.graph_artifact).read(),
                max_output_size=256 * 1024 * 1024,
            )
        )
    if snapshot != expected_snapshot or document != expected_document:
        raise M3SignoffError("evidence graph differs from raw M2 evidence reconstruction")
    graph = EvidenceGraphView(snapshot=snapshot, document=document)

    cluster_collection = _load_contract(
        store,
        _raw(opportunity_result, "_path_clusters"),
        PathClusterCollection,
    )
    expected_clusters = cluster_paths(
        enriched_paths,
        evidence_snapshot_hash=snapshot.snapshot_hash,
        source_map=source_map,
        clock_domain_by_id=clock_domains,
        cdc_inventory=cdc_inventory,
        tns_by_analysis_view=tns_by_view,
        wns_by_analysis_view=wns_by_view,
    )
    expected_cluster_collection = PathClusterCollection(
        evidence_snapshot_hash=snapshot.snapshot_hash,
        clusters=tuple(sorted(expected_clusters, key=lambda item: item.cluster_id)),
        collection_hash=canonical_sha256(
            {
                "schema_version": 1,
                "evidence_snapshot_hash": snapshot.snapshot_hash,
                "clusters": tuple(
                    item.model_dump(mode="json")
                    for item in sorted(expected_clusters, key=lambda item: item.cluster_id)
                ),
            }
        ),
    )
    if cluster_collection != expected_cluster_collection:
        raise M3SignoffError("path clusters differ from reconstructed graph evidence")
    policy = _load_contract(store, _raw(opportunity_result, "_policy"), OpportunityPolicy)
    expected_policy = opportunity_policy_from_project(project.optimization)
    if policy != expected_policy:
        raise M3SignoffError("opportunity policy differs from signed project policy")
    ranked = _load_contract(
        store,
        _raw(opportunity_result, "_ranked_opportunities"),
        RankedOpportunitySet,
    )
    if ranked != rank_opportunities(expected_clusters, graph, policy):
        raise M3SignoffError("ranked opportunities differ from deterministic reconstruction")

    expected_evidence_extensions = {
        "analysis_view_set": canonical_sha256(
            {
                "analysis_views": tuple(
                    item.model_dump(mode="json") for item in view_identities
                )
            }
        ),
        "cdc_inventory": cdc_inventory.inventory_hash,
        "clock_inventory": clock_inventory.clock_graph_hash,
        "critical_path_records": _critical_path_report_hash(path_reports),
        "protection_policy": protection_hash,
        "synthesis_structure": design_json_ref.sha256,
    }
    evidence_recipe = _raw(evidence_result, "_recipe")
    if (
        evidence_result.input_hashes.extensions != expected_evidence_extensions
        or evidence_result.input_hashes.tool_recipe != evidence_recipe.sha256
        or evidence_result.input_hashes.constraints
        != index.constraint_contract_artifact.sha256
        or evidence_result.tool_fingerprint
        != _implementation_fingerprint("evidence", _evidence_sources())
    ):
        raise M3SignoffError("evidence stage execution identity is stale or incomplete")
    opportunity_recipe = _raw(opportunity_result, "_recipe")
    expected_opportunity_extensions = {
        "evidence_graph": snapshot.snapshot_hash,
        "protection_policy": policy.policy_hash,
        "transform_registry": canonical_sha256(dict(sorted(CAUSE_TO_TRANSFORM.items()))),
    }
    if (
        opportunity_result.input_hashes.extensions != expected_opportunity_extensions
        or opportunity_result.input_hashes.tool_recipe != opportunity_recipe.sha256
        or opportunity_result.input_hashes.parent_stage_result != evidence_reference.sha256
        or opportunity_result.tool_fingerprint
        != _implementation_fingerprint("opportunity", _opportunity_sources())
    ):
        raise M3SignoffError("opportunity stage execution identity is stale or incomplete")
    return snapshot, source_map, path_collection, cluster_collection, policy, ranked


def _build_report(
    run_directory: Path,
    m2_report: M2SignoffReport,
    m2_packet_hash: str,
    *,
    commit_sha: str,
    implementation_tree_hash: str,
) -> M3SignoffReport:
    index = load_run_index(run_directory)
    if (
        index.status != "PASS"
        or index.profile != "full"
        or index.expected_master_clocks != 5
        or index.expected_generated_clocks != 105
    ):
        raise M3SignoffError("M3 sign-off requires the complete calibrated full M2 run")
    if index.run_id != m2_report.run_id:
        raise M3SignoffError("M2 packet and M3 run identities differ")
    if index.benchmark_snapshot_hash != m2_report.benchmark_snapshot_hash:
        raise M3SignoffError("M2 packet and M3 benchmark snapshots differ")
    if index.design_contract_hash != m2_report.design_contract_hash:
        raise M3SignoffError("M2 packet and M3 design contracts differ")
    if index.platform_lock_hash != m2_report.platform_lock_hash:
        raise M3SignoffError("M2 packet and M3 platform locks differ")
    current_view_hashes = dict(
        sorted(
            (view.analysis_view_id, canonical_sha256(view))
            for view in index.analysis_views
        )
    )
    if current_view_hashes != m2_report.analysis_view_hashes:
        raise M3SignoffError("M2 packet and M3 analysis views differ")
    for stage_id, signed_hash in m2_report.stage_result_hashes.items():
        current = index.stage_result_artifacts.get(stage_id)
        if current is None or current.sha256 != signed_hash:
            raise M3SignoffError(f"signed M2 stage changed before M3: {stage_id}")

    store = ArtifactStore.open_existing(run_directory / "artifacts")
    project = _load_contract(store, index.project_manifest_artifact, ProjectManifest)
    design = _load_contract(store, index.design_contract_artifact, DesignContract)
    try:
        validate_m3_run_boundary(index, project, design)
    except EvidenceExecutionError as error:
        raise M3SignoffError(str(error)) from error
    evidence_result, evidence_ref = _stage(index, store, _EVIDENCE_STAGE_ID)
    opportunity_result, opportunity_ref = _stage(index, store, _OPPORTUNITY_STAGE_ID)
    (
        snapshot,
        source_map,
        paths,
        clusters,
        policy,
        ranked,
    ) = _reconstruct_evidence(
        index=index,
        store=store,
        project=project,
        evidence_result=evidence_result,
        evidence_reference=evidence_ref,
        opportunity_result=opportunity_result,
    )
    editability_counts = dict(
        sorted(Counter(item.editability for item in ranked.opportunities).items())
    )
    root_cause_counts = dict(
        sorted(
            Counter(
                cause.category
                for cluster in clusters.clusters
                for cause in cluster.root_causes
            ).items()
        )
    )
    editable_count = editability_counts.get("RTL_EDITABLE", 0)
    if editable_count <= 0:
        raise M3SignoffError("full M3 evidence contains no executable RTL opportunity")
    ledger_path = run_directory / "experiment-ledger.sqlite3"
    ledger = ExperimentLedger.open_existing(ledger_path, artifact_store=store)
    replay = replay_digest(replay_run(ledger, index.run_id))
    graph_artifact = _raw(evidence_result, "evidence_graph_zstd")
    source_artifact = _raw(evidence_result, "evidence_source_map")
    path_artifact = _raw(evidence_result, "evidence_critical_paths")
    cluster_artifact = _raw(opportunity_result, "_path_clusters")
    ranked_artifact = _raw(opportunity_result, "_ranked_opportunities")
    input_payload = {
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m2_commit_sha": m2_report.commit_sha,
        "m2_packet_hash": m2_packet_hash,
        "m2_report_hash": m2_report.report_hash,
        "run_index_hash": index.index_hash,
        "evidence_stage_result_hash": evidence_ref.sha256,
        "opportunity_stage_result_hash": opportunity_ref.sha256,
        "evidence_input_hash": paths.evidence_input_hash,
        "evidence_snapshot_hash": snapshot.snapshot_hash,
        "graph_artifact_hash": graph_artifact.sha256,
        "source_map_artifact_hash": source_artifact.sha256,
        "path_record_artifact_hash": path_artifact.sha256,
        "cluster_artifact_hash": cluster_artifact.sha256,
        "ranked_opportunity_artifact_hash": ranked_artifact.sha256,
        "replay_digest": replay,
        "ledger_hash": _hash_bytes(ledger_path.read_bytes()),
    }
    payload = {
        "schema_version": 1,
        "status": "PASS",
        **input_payload,
        "run_id": index.run_id,
        "profile": "full",
        "expected_master_clocks": 5,
        "expected_generated_clocks": 105,
        "source_map_hash": source_map.source_map_hash,
        "path_collection_hash": paths.collection_hash,
        "cluster_collection_hash": clusters.collection_hash,
        "opportunity_policy_hash": policy.policy_hash,
        "ranking_hash": ranked.ranking_hash,
        "path_count": len(paths.records),
        "cluster_count": len(clusters.clusters),
        "opportunity_count": len(ranked.opportunities),
        "editable_opportunity_count": editable_count,
        "root_cause_counts": root_cause_counts,
        "editability_counts": editability_counts,
        "input_set_hash": canonical_sha256(input_payload),
    }
    return M3SignoffReport(**payload, report_hash=canonical_sha256(payload))


def _verify_observed_report(
    observed: M3SignoffReport,
    *,
    run_directory: Path,
    m2_packet: Path,
    repository_root: Path,
) -> M3SignoffReport:
    commit, tree = _clean_checkpoint(repository_root)
    if observed.commit_sha != commit or observed.implementation_tree_hash != tree:
        raise M3SignoffError("M3 sign-off report is bound to a different commit")
    m2_report, m2_packet_hash = _m2_dependency(
        m2_packet,
        repository_root=repository_root,
        current_commit=commit,
    )
    expected = _build_report(
        run_directory,
        m2_report,
        m2_packet_hash,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    if observed != expected:
        raise M3SignoffError("M3 sign-off report differs from reconstructed evidence")
    return observed


def run_m3_signoff(
    run_directory: Path,
    m2_packet: Path,
    *,
    repository_root: Path = Path("."),
) -> tuple[Path, M3SignoffReport]:
    """Construct, verify, and atomically publish the M3 packet."""

    repository = repository_root.resolve(strict=True)
    run = run_directory.resolve(strict=True)
    commit, tree = _clean_checkpoint(repository)
    m2_report, m2_packet_hash = _m2_dependency(
        m2_packet,
        repository_root=repository,
        current_commit=commit,
    )
    report = _build_report(
        run,
        m2_report,
        m2_packet_hash,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    verified = _verify_observed_report(
        report,
        run_directory=run,
        m2_packet=m2_packet,
        repository_root=repository,
    )
    destination = run / "m3-signoff.json"
    _require_checkpoint_unchanged(repository, commit, tree)
    _publish_report_atomic(destination, canonical_json_bytes(verified))
    return destination, verified


def verify_m3_signoff(
    report_path: Path,
    *,
    m2_packet: Path,
    repository_root: Path = Path("."),
) -> M3SignoffReport:
    """Independently reconstruct and verify every M3 packet identity."""

    try:
        path = report_path.resolve(strict=True)
        observed = M3SignoffReport.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise M3SignoffError("M3 sign-off report is missing or invalid") from error
    return _verify_observed_report(
        observed,
        run_directory=path.parent,
        m2_packet=m2_packet,
        repository_root=repository_root.resolve(strict=True),
    )


__all__ = [
    "M3SignoffError",
    "run_m3_signoff",
    "verify_m3_signoff",
]
