"""One real priority-mux candidate through materialization, proof, STA and physical gates."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal, Self

import yaml
from pydantic import field_validator, model_validator

from nova_rtl.analysis_views.comparability import (
    ApprovedIdentityRemap,
    ComparabilityResult,
    IncomparableResultsError,
    assert_comparable,
)
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.baseline.execution import analyze_run
from nova_rtl.baseline.flow import BaselineRunIndex, initialize_run, load_run_index
from nova_rtl.benchmark.validate import validate_benchmark
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import (
    ArtifactRef,
    HashRef,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import BenchmarkFile, BenchmarkSnapshot
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.manifest import ProjectManifest
from nova_rtl.contracts.optimization import CandidateRecord, OptimizationProposal
from nova_rtl.contracts.platform import ToolchainReceipt, ToolFingerprint
from nova_rtl.contracts.verification import (
    ConstraintBindingManifest,
    FormalModelContract,
    ProofResult,
)
from nova_rtl.evaluation.cascade import (
    EVALUATION_GATE_ORDER,
    EvaluationCascade,
    EvaluationResult,
    GateAssessment,
)
from nova_rtl.evaluation.feasibility import (
    CandidateFeasibilityEvidence,
    FeasibilityPolicy,
    is_feasible,
)
from nova_rtl.evidence.execution import analyze_evidence_run
from nova_rtl.evidence.opportunities import RankedOpportunitySet
from nova_rtl.evidence.source_map import SourceMapSnapshot
from nova_rtl.formal.compose import (
    compose_strict_equivalence,
    run_strict_equivalence,
    source_snapshot_hash,
)
from nova_rtl.optimization.authority import (
    PriorityMuxAuthorization,
    select_priority_mux_authority,
)
from nova_rtl.optimization.comparison import (
    M4StructuralComparison,
    compare_structural_invariants,
)
from nova_rtl.search.dag import replace_candidate
from nova_rtl.transforms.executor import MaterializationRequest, TransformExecutor
from nova_rtl.transforms.priority_mux import RestructurePriorityMux
from nova_rtl.transforms.registry import TransformRegistry
from nova_rtl.transforms.syntax import ParsedSlangAst, SlangSyntaxBackend

_BASELINE_STAGES = ("yosys", "opensta", "binding", "clock", "cdc", "formal-smoke", "openroad")


class OptimizationFlowError(RuntimeError):
    """The M4 vertical slice lacks trustworthy inputs or complete gate evidence."""


class M4CandidateBundle(StrictContract):
    """Self-hashed candidate, evaluation, proof, and candidate-run evidence packet."""

    schema_version: Literal[2] = 2
    parent_run_id: str
    parent_run_index_hash: HashRef
    candidate_run_id: str
    candidate_run_index_hash: HashRef
    m3_source_map_artifact: ArtifactRef
    m3_ranked_opportunities_artifact: ArtifactRef
    syntax_span_artifact: ArtifactRef
    proposal_artifact: ArtifactRef
    candidate: CandidateRecord
    evaluation: EvaluationResult
    feasibility_policy: FeasibilityPolicy
    feasibility_evidence: CandidateFeasibilityEvidence
    prephysical_proof: ProofResult
    final_proof: ProofResult
    structural_comparison: M4StructuralComparison
    view_comparisons: dict[str, ComparabilityResult]
    candidate_stage_result_artifacts: dict[str, ArtifactRef]
    status: Literal["PASS", "FAIL"]
    bundle_hash: HashRef

    @field_validator("candidate_stage_result_artifacts")
    @classmethod
    def stage_map_is_canonical(cls, value: dict[str, ArtifactRef]) -> dict[str, ArtifactRef]:
        return dict(sorted(value.items()))

    @field_validator("view_comparisons")
    @classmethod
    def comparisons_are_canonical(
        cls, value: dict[str, ComparabilityResult]
    ) -> dict[str, ComparabilityResult]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def identities_are_coherent_and_self_hashed(self) -> Self:
        if self.candidate.run_id != self.parent_run_id:
            raise ValueError("candidate lineage must bind the parent optimization run")
        if self.evaluation.candidate_id != self.candidate.candidate_id:
            raise ValueError("evaluation candidate identity does not match bundle")
        if self.feasibility_evidence.candidate_id != self.candidate.candidate_id:
            raise ValueError("feasibility candidate identity does not match bundle")
        if self.prephysical_proof.candidate_id != self.candidate.candidate_id:
            raise ValueError("prephysical proof candidate identity does not match bundle")
        if self.final_proof.candidate_id != self.candidate.candidate_id:
            raise ValueError("final proof candidate identity does not match bundle")
        if not self.structural_comparison.safe:
            raise ValueError("candidate bundle contains a forbidden structural delta")
        expected_comparisons = {
            f"{tool}_{view_id}"
            for view_id in self.feasibility_policy.required_analysis_view_ids
            for tool in ("openroad", "opensta")
        }
        if set(self.view_comparisons) != expected_comparisons:
            raise ValueError("candidate bundle has an incomplete view comparison set")
        expected_status = (
            "PASS"
            if self.evaluation.status == "PASS"
            and self.candidate.classification in {"FEASIBLE_PARETO", "SELECTED"}
            else "FAIL"
        )
        if self.status != expected_status:
            raise ValueError("bundle status does not match candidate evaluation")
        if self.bundle_hash != canonical_sha256(self, exclude=frozenset({"bundle_hash"})):
            raise ValueError("M4 candidate bundle hash is not canonical")
        return self


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _load_project(root: Path) -> ProjectManifest:
    try:
        return ProjectManifest.model_validate(
            yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise OptimizationFlowError(f"candidate project manifest is invalid: {error}") from error


def _load_stage_results(index: BaselineRunIndex, run_directory: Path) -> dict[str, StageResult]:
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    results: dict[str, StageResult] = {}
    for stage_id, reference in index.stage_result_artifacts.items():
        try:
            result = StageResult.model_validate_json(store.open_verified(reference).read())
            for raw in result.raw_artifacts:
                store.open_verified(raw).close()
        except (ArtifactStoreError, ValueError) as error:
            raise OptimizationFlowError(f"invalid stage evidence {stage_id}: {error}") from error
        if result.stage_result_id != stage_id or result.candidate_id != index.candidate_id:
            raise OptimizationFlowError(f"stage result identity mismatch: {stage_id}")
        results[stage_id] = result
    return results


def _stage_contract(
    *,
    run_directory: Path,
    result: StageResult,
    contract_type: type[ConstraintBindingManifest | ClockInventory | CDCInventory],
) -> ConstraintBindingManifest | ClockInventory | CDCInventory:
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    reference = next(
        (item for item in result.raw_artifacts if item.artifact_id.endswith("_contract")),
        None,
    )
    if reference is None:
        raise OptimizationFlowError(
            f"{result.stage_result_id} lacks its canonical structural contract"
        )
    try:
        return contract_type.model_validate_json(store.open_verified(reference).read())
    except (ArtifactStoreError, ValueError) as error:
        raise OptimizationFlowError(
            f"{result.stage_result_id} structural contract is invalid: {error}"
        ) from error


def _load_m3_authority(
    index: BaselineRunIndex, run_directory: Path
) -> tuple[RankedOpportunitySet, ArtifactRef, SourceMapSnapshot, ArtifactRef]:
    reference = index.stage_result_artifacts.get("stage_opportunity_formation")
    if reference is None:
        analyze_evidence_run(run_directory, requested_stages=("evidence", "opportunities"))
        index = load_run_index(run_directory)
        reference = index.stage_result_artifacts.get("stage_opportunity_formation")
    if reference is None:
        raise OptimizationFlowError("M3 opportunity evidence is unavailable")
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    result = StageResult.model_validate_json(store.open_verified(reference).read())
    ranked = next(
        (
            item
            for item in result.raw_artifacts
            if item.artifact_id.endswith("ranked_opportunities")
        ),
        None,
    )
    if ranked is None:
        raise OptimizationFlowError("M3 opportunity stage lacks ranked opportunity evidence")
    ranked_set = RankedOpportunitySet.model_validate_json(store.open_verified(ranked).read())
    source_stage = index.stage_result_artifacts.get("stage_evidence_graph")
    if source_stage is None:
        raise OptimizationFlowError("M3 source-map evidence is unavailable")
    source_result = StageResult.model_validate_json(store.open_verified(source_stage).read())
    source_ref = next(
        (item for item in source_result.raw_artifacts if item.artifact_id == "evidence_source_map"),
        None,
    )
    if source_ref is None:
        raise OptimizationFlowError("M3 source-map stage lacks canonical source-map evidence")
    source_map = SourceMapSnapshot.model_validate_json(store.open_verified(source_ref).read())
    if ranked_set.evidence_snapshot_hash not in {
        item.snapshot_hash
        for opportunity in ranked_set.opportunities
        for item in opportunity.evidence_refs
    }:
        raise OptimizationFlowError("M3 opportunity and source-map evidence are unbound")
    return ranked_set, ranked, source_map, source_ref


def _proposal(
    *,
    authorization: PriorityMuxAuthorization,
    parsed: ParsedSlangAst,
    parent_candidate_id: str,
) -> OptimizationProposal:
    opportunity = authorization.opportunity
    span = authorization.source_span
    proposal_digest = canonical_sha256(
        {
            "opportunity_id": opportunity.opportunity_id,
            "span": span.model_dump(mode="json"),
            "slang_ast": parsed.ast_hash,
            "operation": "RESTRUCTURE_PRIORITY_MUX",
        }
    ).removeprefix("sha256:")
    return OptimizationProposal(
        proposal_id=f"proposal_{proposal_digest[:24]}",
        parent_candidate_id=parent_candidate_id,
        opportunity_id=opportunity.opportunity_id,
        diagnosis_refs=(f"diagnosis_{proposal_digest[:24]}",),
        target={
            "hierarchy": span.owner_hierarchy,
            "source_span_id": span.source_span_id,
            "cone_fingerprint": f"cone:v1:{proposal_digest[:24]}",
        },
        transformation={
            "operation": "RESTRUCTURE_PRIORITY_MUX",
            "family": "LOGIC_RESTRUCTURING",
            "parameters": {"max_branches": 8},
        },
        preconditions=("NO_PROTECTED_NODE_IN_EDIT_SET", "SINGLE_CLOCK_DOMAIN"),
        correctness={
            "contract": "STRICT_SEQ_EQUIV",
            "proof_scope": span.owner_hierarchy,
            "reset_model": "UNCHANGED",
        },
        prediction={
            "timing_direction": "IMPROVE",
            "area_direction": "SMALL_INCREASE",
            "confidence": 0.8,
        },
        evidence_refs=(authorization.evidence_ref,),
        abort_conditions=("FORMAL_NON_PASS", "PROTECTED_INVARIANT_DELTA"),
    )


def _updated_snapshot(project_root: Path, changed_path: str) -> BenchmarkSnapshot:
    snapshot_path = project_root / "benchmark-snapshot.json"
    snapshot = BenchmarkSnapshot.model_validate_json(snapshot_path.read_bytes())
    generated: list[BenchmarkFile] = []
    for item in snapshot.generated_files:
        if item.relative_path == changed_path:
            data = (project_root / changed_path).read_bytes()
            item = BenchmarkFile(
                relative_path=item.relative_path,
                sha256=_hash_bytes(data),
                size_bytes=len(data),
                media_type=item.media_type,
            )
        generated.append(item)
    rtl_identities = {
        item.relative_path: item.sha256
        for item in generated
        if item.relative_path.startswith("rtl/")
    }
    payload = snapshot.model_dump(mode="python", exclude={"snapshot_hash"})
    payload["generated_files"] = tuple(generated)
    payload["expectations"] = snapshot.expectations
    payload["source_hash"] = canonical_sha256(dict(sorted(rtl_identities.items())))
    provisional = BenchmarkSnapshot.model_construct(
        **payload,
        snapshot_hash="sha256:" + "0" * 64,
    )
    candidate = BenchmarkSnapshot(
        **payload,
        snapshot_hash=canonical_sha256(
            provisional,
            exclude=frozenset({"snapshot_hash"}),
        ),
    )
    snapshot_path.write_bytes(canonical_json_bytes(candidate) + b"\n")
    validation = validate_benchmark(candidate, candidate.expectations, artifact_root=project_root)
    if validation.status != "PASS":
        raise OptimizationFlowError("candidate benchmark snapshot failed structural validation")
    return candidate


def _prepare_candidate_project(
    *,
    parent_root: Path,
    materialized_root: Path,
    changed_path: str,
    output: Path,
) -> BenchmarkSnapshot:
    if output.exists():
        return BenchmarkSnapshot.model_validate_json(
            (output / "benchmark-snapshot.json").read_bytes()
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        shutil.copytree(parent_root, staging, dirs_exist_ok=True, symlinks=False)
        destination = staging / changed_path
        destination.chmod(0o644)
        destination.write_bytes((materialized_root / changed_path).read_bytes())
        snapshot = _updated_snapshot(staging, changed_path)
        os.replace(staging, output)
        return snapshot
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _formal_wrapper() -> str:
    instances = "\n".join(
        f"  timing_opportunity_lane #(.FAMILY({index})) u{index} "
        f"(.lane_input(lane_input), .lane_output(lane_output_{index}));"
        for index in range(5)
    )
    outputs = ", ".join(f"lane_output_{index}" for index in range(5))
    return f"""module m4_priority_mux_composition(
    input logic [31:0] lane_input,
    output logic [31:0] {outputs}
);
{instances}
endmodule
"""


def _prepare_formal_inputs(
    parent_root: Path, candidate_root: Path, target_path: str, output: Path
) -> tuple[Path, Path, tuple[str, ...]]:
    gold = output / "gold"
    gate = output / "gate"
    wrapper = _formal_wrapper()
    for root, source_root in ((gold, parent_root), (gate, candidate_root)):
        root.mkdir(parents=True, exist_ok=True)
        root.joinpath("timing_opportunity_lane.sv").write_bytes(
            (source_root / target_path).read_bytes()
        )
        root.joinpath("m4_priority_mux_composition.sv").write_text(wrapper, encoding="utf-8")
    return (
        gold,
        gate,
        (
            "m4_priority_mux_composition.sv",
            "timing_opportunity_lane.sv",
        ),
    )


def _formal_model(
    gold: Path,
    gate: Path,
    paths: tuple[str, ...],
    candidate_id: str,
) -> FormalModelContract:
    gold_hash = source_snapshot_hash(gold, paths)
    gate_hash = source_snapshot_hash(gate, paths)
    return FormalModelContract(
        formal_model_contract_id=f"formal_model_m4_{candidate_id}",
        candidate_id=candidate_id,
        functional_rtl_hash=gold_hash,
        parameter_hash=canonical_sha256({"families": (0, 1, 2, 3, 4)}),
        gold_snapshot_hash=gold_hash,
        gate_snapshot_hash=gate_hash,
        property_manifest_hash=canonical_sha256({"property": "STRICT_MODULE_EQUIVALENCE"}),
        master_clock_model="INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
        generated_clock_model="DERIVED_FROM_PROTECTED_DIVIDER_STATE",
        multiclock_enabled=True,
        reset_assumption_hash=canonical_sha256({"reset": "NO_STATE_IN_PROOF_SCOPE"}),
        environment_assumption_hash=canonical_sha256({"inputs": "SHARED_SYMBOLIC"}),
        proof_scope_policy="WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
        behavioral_elaboration_delta="NONE",
    )


def _toolchain(repository_root: Path) -> dict[str, ToolFingerprint]:
    path = repository_root / ".nova-tools" / "toolchain-receipt.json"
    try:
        receipt = ToolchainReceipt.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise OptimizationFlowError(
            f"verified toolchain receipt is unavailable: {error}"
        ) from error
    return {item.tool_id: item for item in receipt.tool_fingerprints}


def _view_evidence(
    parent_index: BaselineRunIndex,
    candidate_index: BaselineRunIndex,
    parent_results: dict[str, StageResult],
    candidate_results: dict[str, StageResult],
    *,
    parent_run_directory: Path,
    candidate_run_directory: Path,
) -> tuple[
    FeasibilityPolicy,
    dict[str, object],
    M4StructuralComparison,
    dict[str, ComparabilityResult],
]:
    required = tuple(sorted(view.analysis_view_id for view in candidate_index.analysis_views))
    candidate_view_set_hash = canonical_sha256(
        {"views": tuple(canonical_sha256(view) for view in candidate_index.analysis_views)}
    )
    baseline_view_set_hash = canonical_sha256(
        {"views": tuple(canonical_sha256(view) for view in parent_index.analysis_views)}
    )
    if baseline_view_set_hash != candidate_view_set_hash:
        raise OptimizationFlowError("candidate analysis-view set differs from baseline")

    baseline_binding = _stage_contract(
        run_directory=parent_run_directory,
        result=parent_results["stage_binding"],
        contract_type=ConstraintBindingManifest,
    )
    candidate_binding = _stage_contract(
        run_directory=candidate_run_directory,
        result=candidate_results["stage_binding"],
        contract_type=ConstraintBindingManifest,
    )
    baseline_clocks = _stage_contract(
        run_directory=parent_run_directory,
        result=parent_results["stage_clock"],
        contract_type=ClockInventory,
    )
    candidate_clocks = _stage_contract(
        run_directory=candidate_run_directory,
        result=candidate_results["stage_clock"],
        contract_type=ClockInventory,
    )
    baseline_cdc = _stage_contract(
        run_directory=parent_run_directory,
        result=parent_results["stage_cdc"],
        contract_type=CDCInventory,
    )
    candidate_cdc = _stage_contract(
        run_directory=candidate_run_directory,
        result=candidate_results["stage_cdc"],
        contract_type=CDCInventory,
    )
    assert isinstance(baseline_binding, ConstraintBindingManifest)
    assert isinstance(candidate_binding, ConstraintBindingManifest)
    assert isinstance(baseline_clocks, ClockInventory)
    assert isinstance(candidate_clocks, ClockInventory)
    assert isinstance(baseline_cdc, CDCInventory)
    assert isinstance(candidate_cdc, CDCInventory)
    structural = compare_structural_invariants(
        baseline_binding=baseline_binding,
        candidate_binding=candidate_binding,
        baseline_clocks=baseline_clocks,
        candidate_clocks=candidate_clocks,
        baseline_cdc=baseline_cdc,
        candidate_cdc=candidate_cdc,
    )
    project = _load_project(candidate_index.project_root)
    policy = FeasibilityPolicy.build(
        required_analysis_view_ids=required,
        baseline_analysis_view_set_hash=baseline_view_set_hash,
        baseline_constraint_source_hash=parent_index.constraint_contract_artifact.sha256,
        baseline_generated_clock_graph_hash=structural.clock_semantic_hash,
        max_area_growth_percent=project.optimization.max_area_growth_percent,
    )
    complete: dict[str, bool] = {}
    hard_pass: dict[str, bool] = {}
    metrics_present: dict[str, bool] = {}
    metrics: dict[str, object] = {}
    comparisons: dict[str, ComparabilityResult] = {}
    for view in candidate_index.analysis_views:
        view_id = view.analysis_view_id
        sta = candidate_results[f"stage_opensta_{view_id}"]
        road = candidate_results[f"stage_openroad_{view_id}"]
        for stage_name, baseline_result, candidate_result in (
            (f"opensta_{view_id}", parent_results[f"stage_opensta_{view_id}"], sta),
            (
                f"openroad_{view_id}",
                parent_results[f"stage_openroad_{view_id}"],
                road,
            ),
        ):
            baseline_binding_hash = baseline_result.input_hashes.constraint_binding
            candidate_binding_hash = candidate_result.input_hashes.constraint_binding
            if baseline_binding_hash is None or candidate_binding_hash is None:
                raise OptimizationFlowError(
                    f"candidate view {view_id} has incomplete binding identity"
                )
            remaps = ()
            if baseline_binding_hash != candidate_binding_hash:
                remaps = (
                    ApprovedIdentityRemap.build(
                        identity_name="constraint_binding",
                        baseline_identity_hash=baseline_binding_hash,
                        candidate_identity_hash=candidate_binding_hash,
                        semantic_identity_hash=structural.binding_semantic_hash,
                        justification=("IDENTICAL_RESOLVED_SELECTORS_AND_ENDPOINT_COVERAGE"),
                    ),
                )
            try:
                comparisons[stage_name] = assert_comparable(
                    baseline_result,
                    candidate_result,
                    "TIMING",
                    approved_remaps=remaps,
                )
            except IncomparableResultsError as error:
                raise OptimizationFlowError(
                    f"candidate view {view_id} is not comparable: {error}"
                ) from error
        complete[view_id] = (
            sta.status == "PASS"
            and road.status == "PASS"
            and f"opensta_{view_id}" in comparisons
            and f"openroad_{view_id}" in comparisons
        )
        if view.check == "SETUP":
            limit = view.hard_limits["setup_wns_ns"]
            value = road.metrics.setup_wns_ns
            hard_pass[view_id] = value is not None and value >= limit
            metrics_present[view_id] = all(
                item is not None
                for item in (
                    road.metrics.setup_wns_ns,
                    road.metrics.setup_tns_ns,
                    road.metrics.physical_area_um2,
                    road.metrics.cell_count,
                )
            )
        else:
            limit = view.hard_limits["hold_wns_ns"]
            value = road.metrics.hold_wns_ns
            hard_pass[view_id] = value is not None and value >= limit
            metrics_present[view_id] = all(
                item is not None
                for item in (
                    road.metrics.hold_wns_ns,
                    road.metrics.hold_tns_ns,
                    road.metrics.physical_area_um2,
                    road.metrics.cell_count,
                )
            )
        metrics[view_id] = road.metrics
    baseline_area = max(
        result.metrics.physical_area_um2 or 0.0
        for stage, result in parent_results.items()
        if stage.startswith("stage_openroad_")
    )
    candidate_area = max(
        result.metrics.physical_area_um2 or 0.0
        for stage, result in candidate_results.items()
        if stage.startswith("stage_openroad_")
    )
    area_growth = (
        0.0 if baseline_area == 0 else 100.0 * (candidate_area - baseline_area) / baseline_area
    )
    facts: dict[str, object] = {
        "analysis_view_set_hash": candidate_view_set_hash,
        "constraint_source_hash": candidate_index.constraint_contract_artifact.sha256,
        "generated_clock_graph_hash": structural.clock_semantic_hash,
        "effective_constraint_binding": structural.binding_status,
        "unresolved_constraint_selectors": structural.unresolved_constraint_selectors,
        "new_or_unapproved_cdc_crossings": (structural.new_or_unapproved_cdc_crossings),
        "changed_approved_cdc_structures": (
            structural.changed_approved_cdc_structures + structural.removed_approved_cdc_structures
        ),
        "unconstrained_endpoints": structural.unconstrained_endpoints,
        "area_growth_percent": area_growth,
        "view_complete": complete,
        "view_hard_limits_pass": hard_pass,
        "view_metrics_present": metrics_present,
        "per_view_metrics": metrics,
    }
    return policy, facts, structural, dict(sorted(comparisons.items()))


def _assessment(
    gate_id: str,
    passed: bool,
    stage_ids: tuple[str, ...],
    code: str,
) -> GateAssessment:
    return GateAssessment(
        gate_id=gate_id,
        status="PASS" if passed else "FAIL",
        stage_result_ids=stage_ids,
        diagnostic_codes=() if passed else (code,),
    )


def optimize_strict_vertical_slice(
    run_directory: Path,
    *,
    repository_root: Path,
) -> tuple[Path, M4CandidateBundle]:
    """Execute the one-candidate M4 priority-mux strict-equivalence vertical slice."""

    resolved = run_directory.resolve()
    repository_root = repository_root.resolve()
    parent_index = load_run_index(resolved)
    if parent_index.status != "PASS":
        raise OptimizationFlowError("M4 optimization requires a completed baseline run")
    ranked, ranked_ref, source_map, source_map_ref = _load_m3_authority(parent_index, resolved)
    parent_index = load_run_index(resolved)
    project = _load_project(parent_index.project_root)
    tools = _toolchain(repository_root)
    slang = tools.get("slang")
    eqy = tools.get("eqy")
    yosys = tools.get("yosys")
    if slang is None or eqy is None or yosys is None:
        raise OptimizationFlowError("M4 requires pinned Slang, EQY, and Yosys fingerprints")
    syntax = SlangSyntaxBackend(Path(slang.executable), slang.executable_sha256)  # type: ignore[union-attr]
    parsed = syntax.parse_files(
        repository_root=parent_index.project_root,
        source_paths=project.rtl.files,
        include_directories=project.rtl.include_dirs,
        top=project.top,
        timeout_seconds=120,
    )
    capability = RestructurePriorityMux()
    authorization = select_priority_mux_authority(
        project_root=parent_index.project_root,
        editable_path_patterns=project.optimization.editable_path_patterns,
        protected_path_patterns=project.protection.path_patterns,
        parsed=parsed,
        capability=capability,
        ranked=ranked,
        source_map=source_map,
    )
    span = authorization.source_span
    context = authorization.context
    parent_store = ArtifactStore(resolved / "artifacts")
    syntax_ref = parent_store.put_named_bytes(
        canonical_json_bytes(
            {
                "source_span": span.model_dump(mode="json"),
                "ast_hash": parsed.ast_hash,
                "opportunity_id": authorization.opportunity.opportunity_id,
                "target_domain": authorization.opportunity.target_domain,
                "protected_neighbor_ids": authorization.opportunity.protected_neighbors,
                "m3_evidence_ref": authorization.evidence_ref.model_dump(mode="json"),
                "m3_source_map_hash": source_map.source_map_hash,
            }
        ),
        artifact_id="m4_syntax_span",
        media_type="application/json",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    proposal = _proposal(
        authorization=authorization,
        parsed=parsed,
        parent_candidate_id=parent_index.candidate_id,
    )
    proposal_ref = parent_store.put_named_bytes(
        canonical_json_bytes(proposal),
        artifact_id="m4_priority_mux_proposal",
        media_type="application/json",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    executor = TransformExecutor(
        registry=TransformRegistry((capability,)),
        artifact_store=parent_store,
        candidates_root=resolved / "m4" / "materialized",
    )
    materialized = executor.materialize(
        MaterializationRequest(
            run_id=parent_index.run_id,
            parent_candidate_id=parent_index.candidate_id,
            parent_root=parent_index.project_root,
            source_paths=tuple(project.rtl.files),
            proposal=proposal,
            authorized_span=span,
            protected_spans=authorization.protected_spans,
            parsed_ast=parsed,
            transform_context=context,
            lineage_depth=1,
            created_at=datetime.now(UTC),
        )
    )
    candidate_id = materialized.candidate.candidate_id
    bundle_path = resolved / "m4" / "candidates" / candidate_id / "candidate-bundle.json"
    if bundle_path.is_file():
        bundle = M4CandidateBundle.model_validate_json(bundle_path.read_bytes())
        verify_candidate_bundle(bundle_path, repository_root=repository_root)
        return bundle_path, bundle
    candidate_project = resolved / "m4" / "projects" / candidate_id
    _prepare_candidate_project(
        parent_root=parent_index.project_root,
        materialized_root=materialized.workspace,
        changed_path=span.relative_path,
        output=candidate_project,
    )
    initialized = initialize_run(
        candidate_project / "project.yaml",
        runs_root=resolved / "m4" / "candidate-runs",
        candidate_id=candidate_id,
    )
    gold, gate, proof_paths = _prepare_formal_inputs(
        parent_index.project_root,
        candidate_project,
        span.relative_path,
        resolved / "m4" / "formal-inputs" / candidate_id,
    )
    formal_model = _formal_model(gold, gate, proof_paths, candidate_id)
    proof_plan = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=proof_paths,
        top="m4_priority_mux_composition",
        formal_model=formal_model,
    )
    prephysical_proof: ProofResult | None = None

    def strict_pre_timing(_: BaselineRunIndex) -> None:
        nonlocal prephysical_proof
        prephysical_proof = run_strict_equivalence(
            plan=proof_plan,
            gold_root=gold,
            gate_root=gate,
            workspace_root=resolved / "m4" / "proof-work" / candidate_id,
            artifact_store=parent_store,
            eqy_fingerprint=eqy,  # type: ignore[arg-type]
            yosys_fingerprint=yosys,  # type: ignore[arg-type]
            run_id=parent_index.run_id,
            candidate_id=candidate_id,
            proof_label="prephysical",
        )
        if prephysical_proof.outcome != "PASS":
            raise OptimizationFlowError("strict prephysical proof did not pass")

    candidate_index = analyze_run(
        initialized.run_directory,
        requested_stages=_BASELINE_STAGES,
        pre_timing_hook=strict_pre_timing,
    )
    if prephysical_proof is None:
        raise OptimizationFlowError("strict prephysical proof was not executed")
    final_proof = run_strict_equivalence(
        plan=proof_plan,
        gold_root=gold,
        gate_root=gate,
        workspace_root=resolved / "m4" / "proof-work" / candidate_id,
        artifact_store=parent_store,
        eqy_fingerprint=eqy,  # type: ignore[arg-type]
        yosys_fingerprint=yosys,  # type: ignore[arg-type]
        run_id=parent_index.run_id,
        candidate_id=candidate_id,
        proof_label="final",
    )
    parent_results = _load_stage_results(parent_index, resolved)
    candidate_results = _load_stage_results(candidate_index, initialized.run_directory)
    policy, facts, structural, view_comparisons = _view_evidence(
        parent_index,
        candidate_index,
        parent_results,
        candidate_results,
        parent_run_directory=resolved,
        candidate_run_directory=initialized.run_directory,
    )
    evidence = CandidateFeasibilityEvidence.build(
        candidate_id=candidate_id,
        source_hash=materialized.candidate.source_hash,
        mandatory_proof_outcome=final_proof.outcome,
        proof_contract=final_proof.contract,
        analysis_view_set_hash=facts["analysis_view_set_hash"],
        constraint_source_hash=facts["constraint_source_hash"],
        effective_constraint_binding=facts["effective_constraint_binding"],
        unresolved_constraint_selectors=facts["unresolved_constraint_selectors"],
        generated_clock_graph_hash=facts["generated_clock_graph_hash"],
        new_or_unapproved_cdc_crossings=facts["new_or_unapproved_cdc_crossings"],
        changed_approved_cdc_structures=facts["changed_approved_cdc_structures"],
        unconstrained_endpoints=facts["unconstrained_endpoints"],
        area_growth_percent=facts["area_growth_percent"],
        view_complete=facts["view_complete"],
        view_hard_limits_pass=facts["view_hard_limits_pass"],
        view_metrics_present=facts["view_metrics_present"],
        no_hard_domain_regression=all(facts["view_hard_limits_pass"].values()),  # type: ignore[union-attr]
    )
    feasible = is_feasible(materialized.candidate, evidence, policy)
    all_candidate_pass = all(result.status == "PASS" for result in candidate_results.values())
    stage_ids = tuple(
        sorted(
            {
                *candidate_results,
                "stage_m4_strict_prephysical",
                "stage_m4_strict_final",
            }
        )
    )
    per_view_metrics = facts["per_view_metrics"]
    summary_payload = {
        "candidate_id": candidate_id,
        "source_hash": materialized.candidate.source_hash,
        "required_analysis_view_ids": policy.required_analysis_view_ids,
        "passed_stage_result_ids": stage_ids,
        "proof_result_id": final_proof.proof_result_id,
        "proof_outcome": final_proof.outcome,
        "proof_contract": final_proof.contract,
        "binding_manifest_id": f"binding_{candidate_id}",
        "binding_status": structural.binding_status,
        "clock_inventory_id": "stage_clock",
        "clock_inventory_status": "COMPLETE",
        "cdc_inventory_id": "stage_cdc",
        "cdc_inventory_status": "UNCHANGED",
    }
    hard_summary = {
        **summary_payload,
        "summary_hash": canonical_sha256(summary_payload),
    }
    candidate = replace_candidate(
        materialized.candidate,
        stage_result_ids=stage_ids,
        per_view_metrics=per_view_metrics,
        proof_result_id=final_proof.proof_result_id,
        binding_manifest_id=f"binding_{candidate_id}",
        clock_inventory_id="stage_clock",
        cdc_inventory_id="stage_cdc",
        hard_gate_summary=hard_summary,
        classification="FEASIBLE_PARETO" if feasible else "VALID_NEGATIVE_RESULT",
        terminal_disposition="FEASIBLE_PARETO" if feasible else "POLICY_NONPASS",
    )
    structural_stages = ("stage_binding", "stage_clock", "stage_cdc")
    sta_stages = tuple(
        sorted(item for item in candidate_results if item.startswith("stage_opensta_"))
    )
    physical_stages = tuple(
        sorted(item for item in candidate_results if item.startswith("stage_openroad_"))
    )
    gate_inputs = {
        "0": _assessment("0", True, (), "PROPOSAL_POLICY_FAIL"),
        "0.5": _assessment("0.5", True, (), "TRANSFORM_FEASIBILITY_FAIL"),
        "1": _assessment(
            "1",
            candidate_results["stage_yosys"].status == "PASS",
            ("stage_yosys",),
            "RTL_PARSE_FAIL",
        ),
        "2": _assessment(
            "2",
            all(candidate_results[item].status == "PASS" for item in structural_stages)
            and structural.safe,
            structural_stages,
            "STRUCTURAL_INVARIANT_FAIL",
        ),
        "3": _assessment(
            "3",
            candidate_results["stage_yosys"].status == "PASS",
            ("stage_yosys",),
            "FAST_SYNTH_FAIL",
        ),
        "4": _assessment(
            "4",
            prephysical_proof.outcome == "PASS",
            ("stage_m4_strict_prephysical",),
            "STRICT_FORMAL_NONPASS",
        ),
        "5": _assessment(
            "5",
            all(facts["view_complete"].values())  # type: ignore[union-attr]
            and all(facts["view_hard_limits_pass"].values()),  # type: ignore[union-attr]
            sta_stages,
            "FULL_STA_NONPASS",
        ),
        "6": _assessment(
            "6",
            all_candidate_pass and evidence.area_growth_percent <= policy.max_area_growth_percent,
            physical_stages,
            "PHYSICAL_POLICY_NONPASS",
        ),
        "7": _assessment(
            "7",
            final_proof.outcome == "PASS" and feasible,
            ("stage_m4_strict_final",),
            "FINAL_FEASIBILITY_NONPASS",
        ),
    }
    cascade = EvaluationCascade(
        {
            gate: (lambda _candidate, selected=gate: gate_inputs[selected])
            for gate in EVALUATION_GATE_ORDER
        }
    )
    evaluation = cascade.evaluate(candidate)
    payload = {
        "schema_version": 2,
        "parent_run_id": parent_index.run_id,
        "parent_run_index_hash": parent_index.index_hash,
        "candidate_run_id": candidate_index.run_id,
        "candidate_run_index_hash": candidate_index.index_hash,
        "m3_source_map_artifact": source_map_ref,
        "m3_ranked_opportunities_artifact": ranked_ref,
        "syntax_span_artifact": syntax_ref,
        "proposal_artifact": proposal_ref,
        "candidate": candidate,
        "evaluation": evaluation,
        "feasibility_policy": policy,
        "feasibility_evidence": evidence,
        "prephysical_proof": prephysical_proof,
        "final_proof": final_proof,
        "structural_comparison": structural,
        "view_comparisons": view_comparisons,
        "candidate_stage_result_artifacts": dict(candidate_index.stage_result_artifacts),
        "status": "PASS" if evaluation.status == "PASS" and feasible else "FAIL",
    }
    provisional = M4CandidateBundle.model_construct(**payload, bundle_hash="sha256:" + "0" * 64)
    bundle = M4CandidateBundle(
        **payload,
        bundle_hash=canonical_sha256(provisional, exclude=frozenset({"bundle_hash"})),
    )
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(canonical_json_bytes(bundle) + b"\n")
    verify_candidate_bundle(bundle_path, repository_root=repository_root)
    return bundle_path, bundle


def verify_candidate_bundle(bundle_path: Path, *, repository_root: Path) -> M4CandidateBundle:
    """Verify a stored candidate packet and every referenced immutable artifact."""

    try:
        bundle = M4CandidateBundle.model_validate_json(bundle_path.read_bytes())
    except (OSError, ValueError) as error:
        raise OptimizationFlowError(f"candidate bundle is invalid: {error}") from error
    parent_run = bundle_path.resolve().parents[3]
    parent_index = load_run_index(parent_run)
    if parent_index.run_id != bundle.parent_run_id or parent_index.index_hash != (
        bundle.parent_run_index_hash
    ):
        raise OptimizationFlowError("candidate bundle parent run identity changed")
    parent_store = ArtifactStore.open_existing(parent_run / "artifacts")
    for reference in (
        bundle.m3_source_map_artifact,
        bundle.m3_ranked_opportunities_artifact,
        bundle.syntax_span_artifact,
        bundle.proposal_artifact,
        bundle.candidate.rtl_snapshot_artifact,
        bundle.candidate.patch_artifact,
        *bundle.prephysical_proof.raw_artifacts,
        *bundle.final_proof.raw_artifacts,
    ):
        parent_store.open_verified(reference).close()
    candidate_run = parent_run / "m4" / "candidate-runs" / bundle.candidate_run_id
    candidate_index = load_run_index(candidate_run)
    if candidate_index.index_hash != bundle.candidate_run_index_hash:
        raise OptimizationFlowError("candidate run index changed")
    candidate_store = ArtifactStore.open_existing(candidate_run / "artifacts")
    if dict(candidate_index.stage_result_artifacts) != dict(
        bundle.candidate_stage_result_artifacts
    ):
        raise OptimizationFlowError("candidate stage result set changed")
    for stage_id, reference in bundle.candidate_stage_result_artifacts.items():
        result = StageResult.model_validate_json(candidate_store.open_verified(reference).read())
        if (
            result.stage_result_id != stage_id
            or result.candidate_id != bundle.candidate.candidate_id
        ):
            raise OptimizationFlowError(f"candidate stage identity mismatch: {stage_id}")
        for raw in result.raw_artifacts:
            candidate_store.open_verified(raw).close()
    if not is_feasible(bundle.candidate, bundle.feasibility_evidence, bundle.feasibility_policy):
        raise OptimizationFlowError("candidate no longer satisfies hard feasibility")
    if bundle.evaluation.status != "PASS" or bundle.status != "PASS":
        raise OptimizationFlowError("candidate evaluation is not passing")
    _toolchain(repository_root)
    return bundle


def inspect_candidate(bundle_path: Path, *, repository_root: Path) -> dict[str, object]:
    """Return the compact user-facing view only after full packet verification."""

    bundle = verify_candidate_bundle(bundle_path, repository_root=repository_root)
    return {
        "status": bundle.status,
        "candidate_id": bundle.candidate.candidate_id,
        "classification": bundle.candidate.classification,
        "operation": "RESTRUCTURE_PRIORITY_MUX",
        "source_hash": bundle.candidate.source_hash,
        "patch_hash": bundle.candidate.patch_artifact.sha256,
        "proof_outcome": bundle.final_proof.outcome,
        "required_views": bundle.feasibility_policy.required_analysis_view_ids,
        "area_growth_percent": bundle.feasibility_evidence.area_growth_percent,
        "terminal_gate": bundle.evaluation.terminal_gate_id,
        "bundle_hash": bundle.bundle_hash,
    }


__all__ = [
    "M4CandidateBundle",
    "OptimizationFlowError",
    "inspect_candidate",
    "optimize_strict_vertical_slice",
    "verify_candidate_bundle",
]
