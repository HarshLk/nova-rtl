"""Deterministic JSON Schema registry, exporter, and drift checker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from nova_rtl.contracts.analysis import (
    AnalysisViewContract,
    CDCInventory,
    ClockInventory,
    CriticalPathRecord,
    EvidenceGraphSnapshot,
    FrequencySweepContract,
    M3SignoffReport,
    PowerActivityContract,
)
from nova_rtl.contracts.base import (
    ArtifactRef,
    Diagnostic,
    EvidenceRef,
    MetricSet,
    StageInputHashes,
    StrictContract,
)
from nova_rtl.contracts.benchmark import (
    BenchmarkConfig,
    BenchmarkConstraintContract,
    BenchmarkFormalManifest,
    BenchmarkPowerWorkload,
    BenchmarkSnapshot,
    BenchmarkValidation,
    BenchmarkValidationEvidence,
    CalibrationReport,
    M2SignoffReport,
)
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.execution import PreparedCommand, RawToolResult, StageResult, ToolJob
from nova_rtl.contracts.manifest import DesignContract, ProjectManifest
from nova_rtl.contracts.optimization import (
    CandidateRecord,
    M4SignoffReport,
    M4ViewComparison,
    MappedStructuralEffect,
    OptimizationOpportunity,
    OptimizationProposal,
)
from nova_rtl.contracts.planning import (
    ContextRequest,
    CouncilRequest,
    CouncilResult,
    CouncilTrace,
    CritiqueDisposition,
    CritiqueReport,
    DiagnosisReport,
    M6GateEvidence,
    M6SignoffReport,
    PlannerRequest,
    PlannerResult,
    ProposalShortlist,
    ProviderResult,
    RoleContextPack,
    TransformRecommendation,
)
from nova_rtl.contracts.platform import (
    DoctorCheck,
    DoctorReport,
    PlatformLock,
    ToolFingerprint,
)
from nova_rtl.contracts.recovery import (
    CandidateFailureFingerprint,
    FailureEvent,
    M7GateEvidence,
    M7SignoffReport,
    RecoveryAdvice,
    RecoveryDecision,
    RecoveryRequest,
    RecoveryRoutePlan,
    RepairDirective,
    SearchRecoveryReport,
)
from nova_rtl.contracts.reporting import (
    ExperimentRecord,
    M5GateEvidence,
    M5SignoffReport,
    ParetoRecord,
    ReportBundle,
    SearchRequest,
    SearchResult,
)
from nova_rtl.contracts.verification import (
    CompositionClosureManifest,
    ConstraintBindingManifest,
    FormalModelContract,
    ProofResult,
)
from nova_rtl.evaluation.cascade import GateEvent


@dataclass(frozen=True)
class SchemaRegistration:
    model: type[StrictContract]
    version: int


SCHEMA_REGISTRY: dict[str, SchemaRegistration] = {
    "artifact-ref": SchemaRegistration(ArtifactRef, version=1),
    "benchmark-constraint-contract": SchemaRegistration(
        BenchmarkConstraintContract, version=1
    ),
    "benchmark-config": SchemaRegistration(BenchmarkConfig, version=1),
    "benchmark-formal-manifest": SchemaRegistration(BenchmarkFormalManifest, version=1),
    "benchmark-power-workload": SchemaRegistration(BenchmarkPowerWorkload, version=1),
    "benchmark-snapshot": SchemaRegistration(BenchmarkSnapshot, version=1),
    "benchmark-validation": SchemaRegistration(BenchmarkValidation, version=1),
    "benchmark-validation-evidence": SchemaRegistration(
        BenchmarkValidationEvidence, version=1
    ),
    "calibration-report": SchemaRegistration(CalibrationReport, version=1),
    "m2-signoff-report": SchemaRegistration(M2SignoffReport, version=1),
    "evidence-ref": SchemaRegistration(EvidenceRef, version=1),
    "diagnostic": SchemaRegistration(Diagnostic, version=1),
    "doctor-check": SchemaRegistration(DoctorCheck, version=1),
    "stage-input-hashes": SchemaRegistration(StageInputHashes, version=1),
    "project-manifest": SchemaRegistration(ProjectManifest, version=2),
    "platform-lock": SchemaRegistration(PlatformLock, version=1),
    "design-contract": SchemaRegistration(DesignContract, version=1),
    "analysis-view-contract": SchemaRegistration(AnalysisViewContract, version=1),
    "power-activity-contract": SchemaRegistration(PowerActivityContract, version=1),
    "frequency-sweep-contract": SchemaRegistration(FrequencySweepContract, version=1),
    "gate-event": SchemaRegistration(GateEvent, version=1),
    "tool-fingerprint": SchemaRegistration(ToolFingerprint, version=1),
    "doctor-report": SchemaRegistration(DoctorReport, version=1),
    "tool-job": SchemaRegistration(ToolJob, version=1),
    "prepared-command": SchemaRegistration(PreparedCommand, version=1),
    "raw-tool-result": SchemaRegistration(RawToolResult, version=1),
    "critical-path-record": SchemaRegistration(CriticalPathRecord, version=1),
    "clock-inventory": SchemaRegistration(ClockInventory, version=1),
    "cdc-inventory": SchemaRegistration(CDCInventory, version=1),
    "evidence-graph-snapshot": SchemaRegistration(EvidenceGraphSnapshot, version=1),
    "m3-signoff-report": SchemaRegistration(M3SignoffReport, version=1),
    "metric-set": SchemaRegistration(MetricSet, version=1),
    "stage-result": SchemaRegistration(StageResult, version=2),
    "optimization-opportunity": SchemaRegistration(OptimizationOpportunity, version=1),
    "optimization-proposal": SchemaRegistration(OptimizationProposal, version=2),
    "candidate-record": SchemaRegistration(CandidateRecord, version=1),
    "m4-signoff-report": SchemaRegistration(M4SignoffReport, version=2),
    "m5-gate-evidence": SchemaRegistration(M5GateEvidence, version=1),
    "m5-signoff-report": SchemaRegistration(M5SignoffReport, version=1),
    "m6-gate-evidence": SchemaRegistration(M6GateEvidence, version=1),
    "m6-signoff-report": SchemaRegistration(M6SignoffReport, version=1),
    "m4-view-comparison": SchemaRegistration(M4ViewComparison, version=1),
    "mapped-structural-effect": SchemaRegistration(MappedStructuralEffect, version=1),
    "composition-closure-manifest": SchemaRegistration(
        CompositionClosureManifest, version=1
    ),
    "constraint-binding-manifest": SchemaRegistration(
        ConstraintBindingManifest, version=1
    ),
    "formal-model-contract": SchemaRegistration(FormalModelContract, version=1),
    "proof-result": SchemaRegistration(ProofResult, version=1),
    "planner-request": SchemaRegistration(PlannerRequest, version=1),
    "planner-result": SchemaRegistration(PlannerResult, version=1),
    "context-request": SchemaRegistration(ContextRequest, version=1),
    "role-context-pack": SchemaRegistration(RoleContextPack, version=1),
    "provider-result": SchemaRegistration(ProviderResult, version=1),
    "diagnosis-report": SchemaRegistration(DiagnosisReport, version=1),
    "transform-recommendation": SchemaRegistration(TransformRecommendation, version=1),
    "critique-report": SchemaRegistration(CritiqueReport, version=1),
    "critique-disposition": SchemaRegistration(CritiqueDisposition, version=1),
    "proposal-shortlist": SchemaRegistration(ProposalShortlist, version=1),
    "council-trace": SchemaRegistration(CouncilTrace, version=1),
    "council-request": SchemaRegistration(CouncilRequest, version=1),
    "council-result": SchemaRegistration(CouncilResult, version=1),
    "failure-event": SchemaRegistration(FailureEvent, version=1),
    "repair-directive": SchemaRegistration(RepairDirective, version=1),
    "candidate-failure-fingerprint": SchemaRegistration(
        CandidateFailureFingerprint, version=1
    ),
    "recovery-route-plan": SchemaRegistration(RecoveryRoutePlan, version=1),
    "recovery-request": SchemaRegistration(RecoveryRequest, version=1),
    "recovery-advice": SchemaRegistration(RecoveryAdvice, version=1),
    "recovery-decision": SchemaRegistration(RecoveryDecision, version=1),
    "search-recovery-report": SchemaRegistration(SearchRecoveryReport, version=1),
    "m7-gate-evidence": SchemaRegistration(M7GateEvidence, version=1),
    "m7-signoff-report": SchemaRegistration(M7SignoffReport, version=1),
    "experiment-record": SchemaRegistration(ExperimentRecord, version=1),
    "pareto-record": SchemaRegistration(ParetoRecord, version=1),
    "search-request": SchemaRegistration(SearchRequest, version=1),
    "search-result": SchemaRegistration(SearchResult, version=1),
    "report-bundle": SchemaRegistration(ReportBundle, version=1),
    "run-event": SchemaRegistration(RunEvent, version=1),
}


class SchemaDriftError(RuntimeError):
    """Committed schema snapshots differ from the canonical registry."""


def _schema_filename(name: str, registration: SchemaRegistration) -> str:
    return f"{name}.v{registration.version}.schema.json"


def _schema_bytes(name: str, registration: SchemaRegistration) -> bytes:
    model_field = registration.model.model_fields.get("schema_version")
    if model_field is not None and model_field.default != registration.version:
        raise ValueError(
            f"registry version mismatch for {name}: "
            f"model={model_field.default}, registry={registration.version}"
        )
    schema = registration.model.model_json_schema(mode="validation")
    return (
        json.dumps(
            schema,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _expected_schemas() -> dict[str, bytes]:
    return {
        _schema_filename(name, registration): _schema_bytes(name, registration)
        for name, registration in sorted(SCHEMA_REGISTRY.items())
    }


def _write_atomic(path: Path, content: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def export_all_schemas(output_dir: Path) -> tuple[Path, ...]:
    """Write every registered schema as stable, version-qualified JSON bytes."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for filename, content in _expected_schemas().items():
        path = output_dir / filename
        if not path.exists() or path.read_bytes() != content:
            _write_atomic(path, content)
        paths.append(path)
    return tuple(paths)


def check_all_schemas(output_dir: Path) -> tuple[Path, ...]:
    """Fail when committed schema files are missing, unexpected, or byte-drifted."""

    expected = _expected_schemas()
    actual_paths = {path.name: path for path in output_dir.glob("*.schema.json")}
    missing = sorted(set(expected) - set(actual_paths))
    unexpected = sorted(set(actual_paths) - set(expected))
    drifted = sorted(
        filename
        for filename in set(expected) & set(actual_paths)
        if actual_paths[filename].read_bytes() != expected[filename]
    )
    problems: list[str] = []
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected: {', '.join(unexpected)}")
    if drifted:
        problems.append(f"drifted: {', '.join(drifted)}")
    if problems:
        raise SchemaDriftError("; ".join(problems))
    return tuple(actual_paths[name] for name in sorted(actual_paths))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.check:
            paths = check_all_schemas(args.output_dir)
            print(f"schema check passed: {len(paths)} schemas")
        else:
            paths = export_all_schemas(args.output_dir)
            print(f"exported {len(paths)} schemas to {args.output_dir}")
    except (OSError, SchemaDriftError, ValueError) as error:
        print(f"schema export error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_REGISTRY",
    "SchemaDriftError",
    "SchemaRegistration",
    "check_all_schemas",
    "export_all_schemas",
    "main",
]
