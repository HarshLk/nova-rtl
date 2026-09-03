"""Content-addressed initialization and inspection for the tiny baseline flow."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal, Self

import yaml
from pydantic import Field, field_validator, model_validator

from nova_rtl.analysis_views.aggregation import aggregate_required_views
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_digest, replay_run
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.benchmark.validate import validate_benchmark
from nova_rtl.contracts.analysis import AnalysisViewContract, CornerArtifactIdentity
from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    HashRef,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import BenchmarkSnapshot
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.manifest import DesignContract, ProjectManifest
from nova_rtl.contracts.platform import PlatformAnalysisViews
from nova_rtl.orchestrator.runner import plan_resume
from nova_rtl.orchestrator.scheduler import (
    BoundedScheduler,
    JobKindLimits,
    SchedulerPolicy,
)
from nova_rtl.orchestrator.state import RunOrchestrator

_INDEX_FILE = "run-index.json"
_RESUME_FILE = "resume-manifest.json"


class BaselineFlowError(RuntimeError):
    """Baseline initialization, execution, or inspection failed closed."""


class BaselineRunIndex(StrictContract):
    """Mutable local locator whose referenced evidence remains immutable."""

    schema_version: Literal[1] = 1
    run_id: EntityId
    candidate_id: EntityId
    profile: EntityId
    project_root: Path
    status: Literal["INITIALIZED", "BASELINING", "PASS", "FAIL"]
    benchmark_snapshot_hash: HashRef
    expected_master_clocks: int = Field(strict=True, gt=0)
    expected_generated_clocks: int = Field(strict=True, gt=0)
    project_manifest_artifact: ArtifactRef
    benchmark_snapshot_artifact: ArtifactRef
    design_contract_artifact: ArtifactRef
    rtl_bundle_artifact: ArtifactRef
    simulation_testbench_artifact: ArtifactRef
    formal_smoke_source_artifact: ArtifactRef
    formal_property_source_artifact: ArtifactRef | None = None
    constraint_contract_artifact: ArtifactRef
    expected_clock_inventory_artifact: ArtifactRef
    expected_cdc_inventory_artifact: ArtifactRef
    formal_manifest_artifact: ArtifactRef
    design_contract_hash: HashRef
    platform_lock_hash: HashRef
    analysis_views: tuple[AnalysisViewContract, ...] = Field(min_length=2)
    stage_result_artifacts: dict[EntityId, ArtifactRef]
    created_at: datetime
    updated_at: datetime
    index_hash: HashRef

    @field_validator("project_root")
    @classmethod
    def project_root_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or value != value.resolve():
            raise ValueError("project_root must be absolute and normalized")
        return value

    @model_validator(mode="after")
    def identity_is_coherent(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if tuple(self.stage_result_artifacts) != tuple(sorted(self.stage_result_artifacts)):
            raise ValueError("stage_result_artifacts must have deterministic key ordering")
        expected = canonical_sha256(self, exclude=frozenset({"index_hash"}))
        if self.index_hash != expected:
            raise ValueError("index_hash does not match the canonical run index")
        return self


class InitializedRun(StrictContract):
    run_id: EntityId
    run_directory: Path
    design_contract_hash: HashRef


class BaselineEvidence(StrictContract):
    run_id: EntityId
    profile: EntityId
    status: Literal["PASS", "FAIL"]
    expected_master_clocks: int = Field(strict=True, gt=0)
    expected_generated_clocks: int = Field(strict=True, gt=0)
    required_views_complete: bool
    raw_artifacts_resolvable: bool
    stage_result_ids: tuple[EntityId, ...]
    resume_reusable_stage_ids: tuple[EntityId, ...]
    resume_rerun_stage_ids: tuple[EntityId, ...]
    replay_digest_before: HashRef
    replay_digest_after: HashRef


def _hash_bytes(data: bytes) -> str:
    from hashlib import sha256

    return f"sha256:{sha256(data).hexdigest()}"


def _read_project(project_path: Path) -> tuple[Path, ProjectManifest, bytes]:
    resolved = project_path.resolve(strict=True)
    try:
        decoded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        manifest = ProjectManifest.model_validate(decoded)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        raise BaselineFlowError(f"project manifest is invalid: {resolved}: {error}") from error
    return resolved.parent, manifest, canonical_json_bytes(manifest)


def _load_snapshot(project_root: Path) -> tuple[BenchmarkSnapshot, bytes]:
    path = project_root / "benchmark-snapshot.json"
    try:
        snapshot = BenchmarkSnapshot.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise BaselineFlowError(f"benchmark snapshot is invalid: {path}: {error}") from error
    validation = validate_benchmark(snapshot, snapshot.expectations, artifact_root=project_root)
    if validation.status != "PASS":
        codes = ", ".join(item.code for item in validation.diagnostics)
        raise BaselineFlowError(f"benchmark snapshot validation failed: {codes}")
    return snapshot, canonical_json_bytes(snapshot)


def _put_input(
    store: ArtifactStore,
    data: bytes,
    *,
    artifact_id: str,
    media_type: str,
    classification: str,
) -> ArtifactRef:
    return store.put_named_bytes(
        data,
        artifact_id=artifact_id,
        media_type=media_type,
        classification=classification,
        producer_stage_result_id=None,
    )


def _rtl_bundle(project_root: Path, project: ProjectManifest) -> bytes:
    headers = tuple(
        path
        for include_dir in project.rtl.include_dirs
        for path in sorted((project_root / include_dir).rglob("*.svh"))
    )
    chunks = [path.read_bytes() for path in headers]
    for relative in project.rtl.files:
        content = (project_root / relative).read_bytes()
        content = content.replace(b'`include "benchmark_parameters.svh"', b"")
        chunks.extend(
            (
                f"\n// NOVA_SOURCE {relative}\n".encode(),
                content,
                b"\n",
            )
        )
    return b"".join(chunks)


def _analysis_view_contracts(
    project_root: Path,
    project: ProjectManifest,
    constraint_hash: str,
) -> tuple[AnalysisViewContract, ...]:
    path = project_root / "config/analysis/views.yaml"
    try:
        locked = PlatformAnalysisViews.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise BaselineFlowError(f"locked analysis views are invalid: {path}: {error}") from error
    manifest_by_id = {item.id: item for item in project.analysis_views}
    contracts = []
    for view in locked.views:
        declared = manifest_by_id.get(view.analysis_view_id)
        if declared is None or declared.check != view.check:
            raise BaselineFlowError(
                f"project analysis view differs from lock: {view.analysis_view_id}"
            )
        liberty_hash = canonical_sha256({"liberty_artifact_hashes": view.liberty_artifact_hashes})
        contracts.append(
            AnalysisViewContract(
                analysis_view_id=view.analysis_view_id,
                mode="FUNCTIONAL",
                check=view.check,
                required=view.required,
                liberty_corner=CornerArtifactIdentity(
                    id=view.liberty_corner_id,
                    artifact_hash=liberty_hash,
                ),
                rc_corner=CornerArtifactIdentity(
                    id=view.rc_corner_id,
                    artifact_hash=view.rc_artifact_hash,
                ),
                sdc_hash=constraint_hash,
                operating_condition=declared.operating_condition,
                derate_policy_hash=_hash_bytes(
                    (project_root / declared.derate_policy).read_bytes()
                ),
                clock_uncertainty_policy_hash=_hash_bytes(
                    (project_root / declared.clock_uncertainty_policy).read_bytes()
                ),
                hard_limits=declared.hard_limits,
                required_stages=view.required_stages,
                power_activity_contract_id=None,
            )
        )
    return tuple(contracts)


def _design_contract(
    *,
    run_id: str,
    project_root: Path,
    project: ProjectManifest,
    project_hash: str,
    source_artifacts: tuple[ArtifactRef, ...],
    constraint_artifact: ArtifactRef,
    analysis_views: tuple[AnalysisViewContract, ...],
    created_at: datetime,
) -> DesignContract:
    payload = {
        "schema_version": 1,
        "design_contract_id": f"design_{run_id.removeprefix('run_')}",
        "run_id": run_id,
        "project_manifest_hash": project_hash,
        "functional_source_artifacts": tuple(sorted(source_artifacts, key=lambda item: item.uri)),
        "top": project.top,
        "parameters": project.compilation_profiles.functional.parameters,
        "defines": tuple(
            sorted(
                {
                    *project.rtl.defines,
                    *project.compilation_profiles.functional.defines,
                }
            )
        ),
        "functional_compilation_profile_hash": canonical_sha256(
            project.compilation_profiles.functional
        ),
        "formal_compilation_profile_hash": canonical_sha256(project.compilation_profiles.formal),
        "constraint_snapshot_artifact": constraint_artifact,
        "constraint_snapshot_hash": constraint_artifact.sha256,
        "analysis_view_ids": tuple(sorted(item.analysis_view_id for item in analysis_views)),
        "analysis_view_set_hash": canonical_sha256(
            {"analysis_views": tuple(canonical_sha256(item) for item in analysis_views)}
        ),
        "power_activity_contract_id": None,
        "power_activity_contract_hash": None,
        "platform_lock_hash": project.technology.platform_lock_hash,
        "cdc_policy_id": "cdc_patterns_v1",
        "cdc_policy_hash": _hash_bytes(
            (project_root / project.cdc.approved_pattern_registry).read_bytes()
        ),
        "protection_policy_id": "protection_policy_v1",
        "protection_policy_hash": canonical_sha256(project.protection),
        "formal_policy_id": "formal_policy_v1",
        "formal_policy_hash": canonical_sha256(project.formal),
        "effective_policy_id": "nova_default",
        "effective_policy_hash": _hash_bytes(
            (project_root / "config/policy/default.yaml").read_bytes()
        ),
        "organizer_decision_hash": _hash_bytes(
            (project_root / "config/challenge/organizer_decisions.yaml").read_bytes()
        ),
        "created_at": created_at,
    }
    provisional = DesignContract.model_construct(
        **payload,
        contract_hash=f"sha256:{'0' * 64}",
    )
    contract_hash = canonical_sha256(
        provisional,
        exclude=frozenset({"contract_hash"}),
    )
    return DesignContract(**payload, contract_hash=contract_hash)


def _scheduler_policy() -> SchedulerPolicy:
    common = {
        "cpu_cores": 2,
        "memory_bytes": 16 * 1024 * 1024 * 1024,
        "wall_time_ms": 15 * 60 * 1000,
    }
    return SchedulerPolicy(
        kind_limits={
            "SYNTHESIS_STA": JobKindLimits(max_concurrency=1, **common),
            "FORMAL": JobKindLimits(max_concurrency=1, **common),
            "OPENROAD": JobKindLimits(max_concurrency=1, **common),
            "PLANNER": JobKindLimits(max_concurrency=1, **common),
        },
        max_candidates=40,
        max_tokens=30_000,
        max_retry_depth=1,
    )


def _write_index(run_directory: Path, payload: dict[str, object]) -> BaselineRunIndex:
    provisional = BaselineRunIndex.model_construct(
        **payload,
        index_hash=f"sha256:{'0' * 64}",
    )
    index = BaselineRunIndex(
        **payload,
        index_hash=canonical_sha256(provisional, exclude=frozenset({"index_hash"})),
    )
    temporary = run_directory / f".{_INDEX_FILE}.tmp"
    temporary.write_bytes(canonical_json_bytes(index))
    os.replace(temporary, run_directory / _INDEX_FILE)
    return index


def initialize_run(project_path: Path, *, runs_root: Path = Path("runs")) -> InitializedRun:
    """Normalize and snapshot a generated project into M1 persistence authorities."""

    project_root, project, project_bytes = _read_project(project_path)
    snapshot, snapshot_bytes = _load_snapshot(project_root)
    if project.constraints.expected_generated_clocks_per_master != (
        snapshot.expected_generated_per_master
    ):
        raise BaselineFlowError("project and benchmark generated-clock counts differ")
    project_hash = _hash_bytes(project_bytes)
    run_digest = canonical_sha256(
        {
            "project_manifest_hash": project_hash,
            "benchmark_snapshot_hash": snapshot.snapshot_hash,
        }
    ).removeprefix("sha256:")
    run_id = f"run_{run_digest[:24]}"
    root = runs_root.absolute()
    run_directory = root / run_id
    if run_directory.exists():
        existing = load_run_index(run_directory)
        if (
            existing.benchmark_snapshot_hash != snapshot.snapshot_hash
            or existing.project_manifest_artifact.sha256 != project_hash
        ):
            raise BaselineFlowError(f"existing run identity conflicts: {run_directory}")
        _publish_latest(root, run_directory)
        return InitializedRun(
            run_id=run_id,
            run_directory=run_directory,
            design_contract_hash=existing.design_contract_hash,
        )

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{run_id}.", dir=root))
    try:
        store = ArtifactStore(staging / "artifacts")
        project_ref = _put_input(
            store,
            project_bytes,
            artifact_id="input_project_manifest",
            media_type="application/json",
            classification="INTERNAL",
        )
        snapshot_ref = _put_input(
            store,
            snapshot_bytes,
            artifact_id="input_benchmark_snapshot",
            media_type="application/json",
            classification="INTERNAL",
        )
        source_refs = tuple(
            _put_input(
                store,
                (project_root / relative).read_bytes(),
                artifact_id=f"input_rtl_{index:03d}",
                media_type="text/x-systemverilog",
                classification="RESTRICTED_RTL",
            )
            for index, relative in enumerate(project.rtl.files)
        )
        constraint_ref = _put_input(
            store,
            (project_root / project.constraints.sdc).read_bytes(),
            artifact_id="input_constraints",
            media_type="application/x-sdc",
            classification="INTERNAL",
        )
        views = _analysis_view_contracts(project_root, project, constraint_ref.sha256)
        created_at = datetime.now(UTC)
        design = _design_contract(
            run_id=run_id,
            project_root=project_root,
            project=project,
            project_hash=project_hash,
            source_artifacts=source_refs,
            constraint_artifact=constraint_ref,
            analysis_views=views,
            created_at=created_at,
        )
        design_ref = _put_input(
            store,
            canonical_json_bytes(design),
            artifact_id="input_design_contract",
            media_type="application/json",
            classification="INTERNAL",
        )
        auxiliary_inputs = {
            "rtl_bundle_artifact": _put_input(
                store,
                _rtl_bundle(project_root, project),
                artifact_id="input_rtl_bundle",
                media_type="text/x-systemverilog",
                classification="RESTRICTED_RTL",
            ),
            "simulation_testbench_artifact": _put_input(
                store,
                (project_root / "sim/tb_nebula.sv").read_bytes(),
                artifact_id="input_simulation_testbench",
                media_type="text/x-systemverilog",
                classification="INTERNAL",
            ),
            "formal_smoke_source_artifact": _put_input(
                store,
                (project_root / "rtl/cdc/reset_synchronizer.sv").read_bytes(),
                artifact_id="input_formal_smoke_source",
                media_type="text/x-systemverilog",
                classification="RESTRICTED_RTL",
            ),
            "formal_property_source_artifact": _put_input(
                store,
                (project_root / "formal/cdc_protocol_properties.sv").read_bytes(),
                artifact_id="input_formal_property_source",
                media_type="text/x-systemverilog",
                classification="RESTRICTED_RTL",
            ),
            "constraint_contract_artifact": _put_input(
                store,
                (project_root / "expected/constraint_contract.json").read_bytes(),
                artifact_id="input_constraint_contract",
                media_type="application/json",
                classification="INTERNAL",
            ),
            "expected_clock_inventory_artifact": _put_input(
                store,
                (project_root / "expected/clock_inventory.json").read_bytes(),
                artifact_id="input_clock_inventory",
                media_type="application/json",
                classification="INTERNAL",
            ),
            "expected_cdc_inventory_artifact": _put_input(
                store,
                (project_root / "expected/cdc_inventory.json").read_bytes(),
                artifact_id="input_cdc_inventory",
                media_type="application/json",
                classification="INTERNAL",
            ),
            "formal_manifest_artifact": _put_input(
                store,
                (project_root / "formal/harnesses.json").read_bytes(),
                artifact_id="input_formal_manifest",
                media_type="application/json",
                classification="INTERNAL",
            ),
        }
        ledger = ExperimentLedger(staging / "experiment-ledger.sqlite3", artifact_store=store)
        orchestrator = RunOrchestrator(
            ledger=ledger,
            artifact_store=store,
            policy_hash=design.effective_policy_hash,
        )
        orchestrator.transition(run_id, "CREATED", "INGESTING", design)
        BoundedScheduler(staging / "scheduler.sqlite3", _scheduler_policy())
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "candidate_id": "baseline",
            "profile": snapshot.profile,
            "project_root": project_root,
            "status": "INITIALIZED",
            "benchmark_snapshot_hash": snapshot.snapshot_hash,
            "expected_master_clocks": snapshot.expected_master_clocks,
            "expected_generated_clocks": snapshot.expected_generated_total,
            "project_manifest_artifact": project_ref,
            "benchmark_snapshot_artifact": snapshot_ref,
            "design_contract_artifact": design_ref,
            **auxiliary_inputs,
            "design_contract_hash": design.contract_hash,
            "platform_lock_hash": project.technology.platform_lock_hash,
            "analysis_views": views,
            "stage_result_artifacts": {},
            "created_at": created_at,
            "updated_at": created_at,
        }
        _write_index(staging, payload)
        os.replace(staging, run_directory)
    except Exception:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise
    _publish_latest(root, run_directory)
    return InitializedRun(
        run_id=run_id,
        run_directory=run_directory,
        design_contract_hash=design.contract_hash,
    )


def _publish_latest(root: Path, run_directory: Path) -> None:
    latest = root / "latest"
    temporary = root / ".latest.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(run_directory.name, target_is_directory=True)
    if latest.exists() and not latest.is_symlink():
        raise BaselineFlowError(f"runs/latest is not a managed symlink: {latest}")
    os.replace(temporary, latest)


def load_run_index(run_directory: Path) -> BaselineRunIndex:
    path = run_directory.resolve() / _INDEX_FILE
    try:
        return BaselineRunIndex.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise BaselineFlowError(f"run index is invalid: {path}: {error}") from error


def _load_stage_results(
    index: BaselineRunIndex,
    store: ArtifactStore,
) -> tuple[StageResult, ...]:
    results = []
    for stage_id, reference in index.stage_result_artifacts.items():
        try:
            result = StageResult.model_validate_json(store.open_verified(reference).read())
        except (ArtifactStoreError, ValueError) as error:
            raise BaselineFlowError(f"stage result is invalid: {stage_id}: {error}") from error
        if result.stage_result_id != stage_id:
            raise BaselineFlowError(f"stage result identity mismatch: {stage_id}")
        for raw in result.raw_artifacts:
            store.open_verified(raw).close()
        results.append(result)
    return tuple(results)


def inspect_baseline_run(run_directory: Path) -> BaselineEvidence:
    """Verify all persisted stage, view, resume, and replay evidence without tools."""

    resolved = run_directory.resolve()
    index = load_run_index(resolved)
    store = ArtifactStore.open_existing(resolved / "artifacts")
    results = _load_stage_results(index, store)
    grouped: dict[str, dict[str, StageResult]] = {}
    for result in results:
        if result.analysis_view_id is not None:
            grouped.setdefault(result.analysis_view_id, {})[result.stage] = result
    aggregate_required_views(grouped, index.analysis_views)
    resume = plan_resume(resolved)
    ledger = ExperimentLedger.open_existing(
        resolved / "experiment-ledger.sqlite3", artifact_store=store
    )
    first = replay_digest(replay_run(ledger, index.run_id))
    second = replay_digest(replay_run(ledger, index.run_id))
    stage_ids = tuple(index.stage_result_artifacts)
    return BaselineEvidence(
        run_id=index.run_id,
        profile=index.profile,
        status=index.status if index.status in {"PASS", "FAIL"} else "FAIL",
        expected_master_clocks=index.expected_master_clocks,
        expected_generated_clocks=index.expected_generated_clocks,
        required_views_complete=True,
        raw_artifacts_resolvable=True,
        stage_result_ids=stage_ids,
        resume_reusable_stage_ids=tuple(resume["reusable"]),
        resume_rerun_stage_ids=tuple(resume["rerun"]),
        replay_digest_before=first,
        replay_digest_after=second,
    )


__all__ = [
    "BaselineEvidence",
    "BaselineFlowError",
    "BaselineRunIndex",
    "InitializedRun",
    "initialize_run",
    "inspect_baseline_run",
    "load_run_index",
]
