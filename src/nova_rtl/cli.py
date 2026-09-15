"""Command-line entry point for the NOVA-RTL scaffold."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from nova_rtl import __version__
from nova_rtl.baseline.execution import analyze_run
from nova_rtl.baseline.flow import BaselineFlowError, initialize_run, inspect_baseline_run
from nova_rtl.baseline.signoff import M2SignoffError, run_m2_signoff, verify_m2_signoff
from nova_rtl.benchmark.calibrate import CalibrationError, run_full_calibration
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.benchmark.validate import validate_benchmark
from nova_rtl.contracts.planning import CouncilResult
from nova_rtl.contracts.platform import PlatformLockRequest
from nova_rtl.evidence.execution import EvidenceExecutionError, analyze_evidence_run
from nova_rtl.evidence.signoff import M3SignoffError, run_m3_signoff, verify_m3_signoff
from nova_rtl.optimization.council_showcase import run_council_showcase
from nova_rtl.optimization.council_signoff import (
    M8SignoffError,
    run_m8_signoff,
    verify_m8_signoff,
)
from nova_rtl.optimization.flow import (
    OptimizationFlowError,
    inspect_candidate,
    optimize_strict_vertical_slice,
    verify_candidate_bundle,
)
from nova_rtl.optimization.planner_flow import (
    M6PlannerFlowError,
    run_single_agent_planning,
)
from nova_rtl.optimization.planner_signoff import (
    M6SignoffError,
    run_m6_signoff,
    verify_m6_signoff,
)
from nova_rtl.optimization.search_flow import (
    M5SearchFlowError,
    run_deterministic_search,
)
from nova_rtl.optimization.signoff import (
    M4SignoffError,
    run_m4_signoff,
    verify_m4_signoff,
)
from nova_rtl.platform.activation import (
    ToolchainVerificationError,
    create_toolchain_receipt,
    render_shell_environment,
    verify_toolchain,
)
from nova_rtl.platform.doctor import DEFAULT_REQUIRED_TOOLS, run_doctor
from nova_rtl.platform.hydration import (
    HydrationError,
    LoadedToolchainSourceManifest,
    hydrate_toolchain,
    load_toolchain_source_manifest,
)
from nova_rtl.platform.lock import (
    PlatformLockError,
    create_platform_lock,
    load_platform_selection_policy,
    publish_platform_outputs,
    verify_platform_lock,
)
from nova_rtl.platform.signoff import M0SignoffRequest, SignoffError, run_m0_signoff
from nova_rtl.platform.smoke import SmokeError
from nova_rtl.recovery.search import SearchRecoveryError, recover_search_bundle
from nova_rtl.recovery.showcase import (
    PathMigrationRecoveryError,
    inspect_failure,
)
from nova_rtl.recovery.signoff import M7SignoffError, run_m7_signoff, verify_m7_signoff
from nova_rtl.reports.bundle import ReportIntegrityError, verify_report_bundle
from nova_rtl.reports.replay import OfflineReplayError, verify_offline_replay
from nova_rtl.search.signoff import M5SignoffError, run_m5_signoff, verify_m5_signoff
from nova_rtl.signoff.m1 import (
    M1SignoffError,
    M1SignoffRequest,
    run_m1_signoff,
    verify_m1_signoff_packet,
)
from nova_rtl.ui.app import render_text_dashboard
from nova_rtl.ui.view_models import build_view_model, build_view_model_from_replay

app = typer.Typer(
    name="nova",
    help="Evidence-grounded RTL timing optimization framework.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
toolchain_app = typer.Typer(
    name="toolchain",
    help="Hydrate, verify, and activate the pinned portable toolchain.",
    no_args_is_help=True,
)
app.add_typer(toolchain_app, name="toolchain")
platform_app = typer.Typer(
    name="platform",
    help="Discover and lock a verified physical-design platform.",
    no_args_is_help=True,
)
app.add_typer(platform_app, name="platform")
m0_app = typer.Typer(
    name="m0",
    help="Execute and publish the reproducible M0 sign-off gate.",
    no_args_is_help=True,
)
app.add_typer(m0_app, name="m0")
benchmark_app = typer.Typer(
    name="benchmark",
    help="Generate and validate deterministic RTL benchmark snapshots.",
    no_args_is_help=True,
)
app.add_typer(benchmark_app, name="benchmark")
m2_app = typer.Typer(
    name="m2",
    help="Build and verify the trustworthy full-baseline M2 sign-off packet.",
    no_args_is_help=True,
)
app.add_typer(m2_app, name="m2")
m3_app = typer.Typer(
    name="m3",
    help="Build and verify the source-mapped evidence and opportunity sign-off packet.",
    no_args_is_help=True,
)
app.add_typer(m3_app, name="m3")
m4_app = typer.Typer(
    name="m4",
    help="Build and verify the strict-equivalence candidate milestone packet.",
    no_args_is_help=True,
)
app.add_typer(m4_app, name="m4")
m5_app = typer.Typer(
    name="m5",
    help="Build and verify the bounded deterministic-search milestone packet.",
    no_args_is_help=True,
)
app.add_typer(m5_app, name="m5")
m6_app = typer.Typer(
    name="m6",
    help="Build and verify the constrained single-agent planner milestone packet.",
    no_args_is_help=True,
)
app.add_typer(m6_app, name="m6")
m7_app = typer.Typer(
    name="m7",
    help="Build and verify the deterministic failure-recovery milestone packet.",
    no_args_is_help=True,
)
app.add_typer(m7_app, name="m7")
m8_app = typer.Typer(
    name="m8",
    help="Build and verify the bounded role-scoped council milestone packet.",
    no_args_is_help=True,
)
app.add_typer(m8_app, name="m8")
m1_app = typer.Typer(
    name="m1",
    help="Execute and verify the contracts, artifacts, ledger, and replay sign-off gate.",
    no_args_is_help=True,
)
app.add_typer(m1_app, name="m1")
candidate_app = typer.Typer(
    name="candidate",
    help="Inspect immutable optimized candidate evidence.",
    no_args_is_help=True,
)
app.add_typer(candidate_app, name="candidate")
failure_app = typer.Typer(
    name="failure",
    help="Inspect deterministic failure and recovery evidence.",
    no_args_is_help=True,
)
app.add_typer(failure_app, name="failure")
council_app = typer.Typer(
    name="council",
    help="Inspect bounded role-scoped council evidence.",
    no_args_is_help=True,
)
app.add_typer(council_app, name="council")


def _tool_root(
    manifest_path: Path, project_root: Path
) -> tuple[LoadedToolchainSourceManifest, Path]:
    loaded = load_toolchain_source_manifest(manifest_path)
    return loaded, project_root.absolute() / loaded.manifest.tool_root_name


def _toolchain_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "error": str(error)}, separators=(",", ":"), sort_keys=True
            )
        )
    else:
        typer.echo(f"NOVA toolchain: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _platform_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "error": str(error)}, separators=(",", ":"), sort_keys=True
            )
        )
    else:
        typer.echo(f"NOVA platform lock: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _signoff_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M0", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M0 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _benchmark_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA benchmark generation: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _baseline_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA baseline: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m2_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M2", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M2 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m3_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M3", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M3 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _optimization_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M4", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M4 optimization: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m4_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M4", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M4 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m5_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M5", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M5 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m6_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M6", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M6 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _m7_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M7", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M7 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


def _candidate_bundle_path(candidate: str, runs_root: Path) -> Path:
    supplied = Path(candidate)
    if supplied.is_file():
        return supplied.resolve()
    matches = tuple(
        sorted(
            runs_root.resolve().glob(
                f"**/m4/candidates/{candidate}/candidate-bundle.json"
            )
        )
    )
    if len(matches) != 1:
        raise OptimizationFlowError(
            f"candidate ID must resolve to exactly one bundle under {runs_root}: {candidate}"
        )
    return matches[0]


@council_app.command("inspect")
def council_inspect(
    council_result: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            readable=True,
            metavar="COUNCIL_RESULT",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate and display one persisted bounded-council result."""

    try:
        result = CouncilResult.model_validate_json(council_result.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": result.status,
        "council_result_id": result.council_result_id,
        "selected_role_ids": result.selected_role_ids,
        "proposal_ids": result.final_ordered_proposal_ids,
        "total_tokens": result.total_tokens,
        "total_latency_ms": result.total_latency_ms,
        "trace_completeness_percent": result.trace_completeness_percent,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA council: {result.status}: {result.council_result_id}"
    )


@council_app.command("run")
def council_run(
    output: Annotated[Path, typer.Option("--output", file_okay=False)],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the reproducible offline minimum-council showcase."""

    try:
        path, result = run_council_showcase(output)
    except (OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": result.status,
        "council_result_id": result.council_result_id,
        "selected_role_ids": result.selected_role_ids,
        "proposal_ids": result.final_ordered_proposal_ids,
        "result": str(path),
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M8 council showcase: {result.status}: {path}"
    )


@m8_app.command("signoff")
def m8_signoff(
    council_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="COUNCIL_DIRECTORY"),
    ],
    m7_packet: Annotated[Path, typer.Option("--m7-packet", exists=True, readable=True)],
    path_migration_report: Annotated[
        Path, typer.Option("--path-migration-report", exists=True, readable=True)
    ],
    m6_packet: Annotated[Path, typer.Option("--m6-packet", exists=True, readable=True)],
    m5_packet: Annotated[Path, typer.Option("--m5-packet", exists=True, readable=True)],
    m4_packet: Annotated[Path, typer.Option("--m4-packet", exists=True, readable=True)],
    m3_packet: Annotated[Path, typer.Option("--m3-packet", exists=True, readable=True)],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create the commit-bound M8 council sign-off packet."""

    try:
        path, report = run_m8_signoff(
            council_directory,
            m7_packet=m7_packet,
            path_migration_report=path_migration_report,
            m6_packet=m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M8SignoffError, OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M8 sign-off: PASS: {path}"
    )


@m8_app.command("verify")
def m8_verify(
    report: Annotated[Path, typer.Argument(exists=True, readable=True, metavar="REPORT")],
    council_directory: Annotated[
        Path,
        typer.Option("--council-directory", exists=True, file_okay=False, readable=True),
    ],
    m7_packet: Annotated[Path, typer.Option("--m7-packet", exists=True, readable=True)],
    path_migration_report: Annotated[
        Path, typer.Option("--path-migration-report", exists=True, readable=True)
    ],
    m6_packet: Annotated[Path, typer.Option("--m6-packet", exists=True, readable=True)],
    m5_packet: Annotated[Path, typer.Option("--m5-packet", exists=True, readable=True)],
    m4_packet: Annotated[Path, typer.Option("--m4-packet", exists=True, readable=True)],
    m3_packet: Annotated[Path, typer.Option("--m3-packet", exists=True, readable=True)],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Independently reconstruct the M8 packet at the current commit."""

    try:
        verified = verify_m8_signoff(
            report,
            council_directory=council_directory,
            m7_packet=m7_packet,
            path_migration_report=path_migration_report,
            m6_packet=m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M8SignoffError, OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {"status": verified.status, "report_hash": verified.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M8 verification: PASS: {verified.report_hash}"
    )


@app.command("optimize")
def optimize(
    run_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="RUN_DIRECTORY"),
    ],
    planner: Annotated[str, typer.Option("--planner")] = "heuristic",
    provider_response: Annotated[
        Path | None,
        typer.Option("--provider-response", exists=True, file_okay=True, readable=True),
    ] = None,
    max_candidates: Annotated[int, typer.Option("--max-candidates", min=1, max=40)] = 1,
    operations: Annotated[str, typer.Option("--operations")] = "AUTO",
    seed: Annotated[int, typer.Option("--seed", min=0)] = 20260808,
    formal_budget: Annotated[int | None, typer.Option("--formal-budget", min=1)] = None,
    physical_budget: Annotated[int | None, typer.Option("--physical-budget", min=1)] = None,
    token_budget: Annotated[int, typer.Option("--token-budget", min=1)] = 1,
    latency_budget_ms: Annotated[
        int, typer.Option("--latency-budget-ms", min=1)
    ] = 3_600_000,
    stagnation_window: Annotated[
        int, typer.Option("--stagnation-window", min=1)
    ] = 8,
    recovery: Annotated[str, typer.Option("--recovery")] = "off",
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute M4's vertical slice or M5's bounded deterministic search."""

    try:
        if planner.lower() not in {"heuristic", "single_agent"}:
            raise OptimizationFlowError(
                "planner must be heuristic or single_agent"
            )
        if recovery not in {"off", "deterministic"}:
            raise OptimizationFlowError("--recovery must be off or deterministic")
        if planner.lower() == "heuristic" and provider_response is not None:
            raise OptimizationFlowError(
                "--provider-response is valid only for single_agent planning"
            )
        requested = (
            ("RESTRUCTURE_PRIORITY_MUX",)
            if operations == "AUTO" and max_candidates == 1
            else (
                (
                    "BALANCE_BOOLEAN_TREE",
                    "FACTOR_COMMON_PREDICATE",
                    "FSM_DECODE_RESTRUCTURE",
                    "RESTRUCTURE_PRIORITY_MUX",
                )
                if operations == "AUTO"
                else tuple(
                    item.strip().upper()
                    for item in operations.split(",")
                    if item.strip()
                )
            )
        )
        if (
            planner.lower() == "heuristic"
            and max_candidates == 1
            and requested == ("RESTRUCTURE_PRIORITY_MUX",)
        ):
            path, bundle = optimize_strict_vertical_slice(
                run_directory, repository_root=repository_root
            )
            payload = {
                "status": bundle.status,
                "milestone": "M4",
                "candidate_id": bundle.candidate.candidate_id,
                "classification": bundle.candidate.classification,
                "bundle": str(path),
                "bundle_hash": bundle.bundle_hash,
            }
        else:
            formal = min(max_candidates, formal_budget or max_candidates)
            physical = min(formal, physical_budget or min(formal, 4))
            path, search = run_deterministic_search(
                run_directory,
                repository_root=repository_root,
                operations=requested,
                max_candidates=max_candidates,
                formal_budget=formal,
                physical_budget=physical,
                token_budget=token_budget,
                latency_budget_ms=latency_budget_ms,
                deterministic_seed=seed,
                stagnation_window=stagnation_window,
            )
            recovery_report_path = None
            recovery_report = None
            if recovery == "deterministic":
                recovery_report_path, recovery_report = recover_search_bundle(
                    path,
                    repository_root=repository_root,
                )
            if planner.lower() == "single_agent":
                path, planning = run_single_agent_planning(
                    path,
                    repository_root=repository_root,
                    provider_response=provider_response,
                )
                payload = {
                    "status": planning.status,
                    "milestone": "M6",
                    "planner_mode": "SINGLE_AGENT",
                    "fallback_count": planning.fallback_count,
                    "proposal_ids": tuple(
                        item.proposal_id for item in planning.proposals
                    ),
                    "bundle": str(path),
                    "bundle_hash": planning.bundle_hash,
                }
            else:
                payload = {
                    "status": search.status,
                    "milestone": "M5",
                    "search_status": search.search_result.status,
                    "selected_candidate_id": search.search_result.selected_candidate_id,
                    "candidate_ids": search.search_result.ordered_candidate_ids,
                    "valid_negative_candidate_ids": search.valid_negative_candidate_ids,
                    "bundle": str(path),
                    "bundle_hash": search.bundle_hash,
                    "recovery_report": (
                        str(recovery_report_path) if recovery_report_path else None
                    ),
                    "recovery_decision_id": (
                        recovery_report.decision.recovery_decision_id
                        if recovery_report
                        else None
                    ),
                }
    except (
        BaselineFlowError,
        M5SearchFlowError,
        M6PlannerFlowError,
        OptimizationFlowError,
        PathMigrationRecoveryError,
        SearchRecoveryError,
        OSError,
        ValidationError,
        ValueError,
    ) as error:
        _optimization_failure(error, json_output)
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA {payload['milestone']} optimization: {payload['status']}: {path}"
    )


@failure_app.command("inspect")
def failure_inspect(
    failure_id: Annotated[str, typer.Argument(metavar="FAILURE_ID")],
    runs_root: Annotated[Path, typer.Option("--runs-root", file_okay=False)] = Path("runs"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resolve and verify one deterministic recovery report."""

    try:
        path, report = inspect_failure(failure_id, runs_root)
    except (PathMigrationRecoveryError, OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": report.status,
        "failure_event_id": report.failure.failure_event_id,
        "failure_family": report.failure.failure_family,
        "action": report.decision.action,
        "report": str(path),
        "report_hash": report.report_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA failure recovery: PASS: {failure_id} -> {report.decision.action}"
    )


@candidate_app.command("inspect")
def candidate_inspect(
    candidate: Annotated[str, typer.Argument(metavar="CANDIDATE_ID_OR_BUNDLE")],
    runs_root: Annotated[Path, typer.Option("--runs-root", file_okay=False)] = Path("runs"),
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect a candidate only after resolving and verifying all evidence."""

    try:
        path = _candidate_bundle_path(candidate, runs_root)
        payload = inspect_candidate(path, repository_root=repository_root)
    except (OptimizationFlowError, OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else (
            f"NOVA candidate: {payload['status']}: "
            f"{payload['candidate_id']} ({payload['proof_outcome']})"
        )
    )


@app.command("verify")
def candidate_verify(
    candidate: Annotated[str, typer.Argument(metavar="CANDIDATE_ID_OR_BUNDLE")],
    runs_root: Annotated[Path, typer.Option("--runs-root", file_okay=False)] = Path("runs"),
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify an immutable candidate bundle independently without rerunning tools."""

    try:
        path = _candidate_bundle_path(candidate, runs_root)
        bundle = verify_candidate_bundle(path, repository_root=repository_root)
    except (OptimizationFlowError, OSError, ValidationError, ValueError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": bundle.status,
        "candidate_id": bundle.candidate.candidate_id,
        "bundle_hash": bundle.bundle_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA candidate verification: PASS: {bundle.candidate.candidate_id}"
    )


@m4_app.command("signoff")
def m4_signoff(
    candidate_bundle: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="CANDIDATE_BUNDLE"),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create and independently verify the final commit-bound M4 packet."""

    try:
        path, report = run_m4_signoff(
            candidate_bundle,
            m3_packet,
            repository_root=repository_root,
        )
    except (M4SignoffError, OSError, ValidationError, ValueError) as error:
        _m4_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M4 sign-off: PASS: {path}"
    )


@m4_app.command("verify")
def m4_verify(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT"),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconstruct and verify every identity in an M4 packet."""

    try:
        report = verify_m4_signoff(
            report_path,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M4SignoffError, OSError, ValidationError, ValueError) as error:
        _m4_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M4 verification: PASS: {report_path.resolve()}"
    )


@m5_app.command("signoff")
def m5_signoff(
    search_bundle: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="SEARCH_BUNDLE"),
    ],
    m4_packet: Annotated[
        Path,
        typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute and publish the final commit-bound M5 search packet."""

    try:
        path, report = run_m5_signoff(
            search_bundle,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M5SignoffError, OSError, ValidationError, ValueError) as error:
        _m5_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M5 sign-off: PASS: {path}"
    )


@m5_app.command("verify")
def m5_verify(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT"),
    ],
    m4_packet: Annotated[
        Path,
        typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconstruct and verify every identity in an M5 packet."""

    try:
        report = verify_m5_signoff(
            report_path,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M5SignoffError, OSError, ValidationError, ValueError) as error:
        _m5_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M5 verification: PASS: {report_path.resolve()}"
    )


@m6_app.command("signoff")
def m6_signoff(
    planner_run: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="PLANNER_RUN"),
    ],
    m5_packet: Annotated[
        Path,
        typer.Option("--m5-packet", exists=True, dir_okay=False, readable=True),
    ],
    m4_packet: Annotated[
        Path,
        typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute and publish the final commit-bound M6 planner packet."""

    try:
        path, report = run_m6_signoff(
            planner_run,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M6SignoffError, OSError, ValidationError, ValueError) as error:
        _m6_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M6 sign-off: PASS: {path}"
    )


@m6_app.command("verify")
def m6_verify(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT"),
    ],
    planner_run: Annotated[
        Path,
        typer.Option("--planner-run", exists=True, dir_okay=False, readable=True),
    ],
    m5_packet: Annotated[
        Path,
        typer.Option("--m5-packet", exists=True, dir_okay=False, readable=True),
    ],
    m4_packet: Annotated[
        Path,
        typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True),
    ],
    m3_packet: Annotated[
        Path,
        typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconstruct every M6 identity without invoking the model provider."""

    try:
        report = verify_m6_signoff(
            report_path,
            planner_run=planner_run,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M6SignoffError, OSError, ValidationError, ValueError) as error:
        _m6_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M6 verification: PASS: {report_path.resolve()}"
    )


@m7_app.command("signoff")
def m7_signoff(
    path_migration_report: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, readable=True, metavar="PATH_MIGRATION_REPORT"
        ),
    ],
    m6_packet: Annotated[
        Path, typer.Option("--m6-packet", exists=True, dir_okay=False, readable=True)
    ],
    m5_packet: Annotated[
        Path, typer.Option("--m5-packet", exists=True, dir_okay=False, readable=True)
    ],
    m4_packet: Annotated[
        Path, typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True)
    ],
    m3_packet: Annotated[
        Path, typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True)
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute and publish the final commit-bound M7 recovery packet."""

    try:
        path, report = run_m7_signoff(
            path_migration_report,
            m6_packet=m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M7SignoffError, OSError, ValidationError, ValueError) as error:
        _m7_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M7 sign-off: PASS: {path}"
    )


@m7_app.command("verify")
def m7_verify(
    report_path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT")
    ],
    path_migration_report: Annotated[
        Path,
        typer.Option(
            "--path-migration-report", exists=True, dir_okay=False, readable=True
        ),
    ],
    m6_packet: Annotated[
        Path, typer.Option("--m6-packet", exists=True, dir_okay=False, readable=True)
    ],
    m5_packet: Annotated[
        Path, typer.Option("--m5-packet", exists=True, dir_okay=False, readable=True)
    ],
    m4_packet: Annotated[
        Path, typer.Option("--m4-packet", exists=True, dir_okay=False, readable=True)
    ],
    m3_packet: Annotated[
        Path, typer.Option("--m3-packet", exists=True, dir_okay=False, readable=True)
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconstruct M7 from immutable recovery and ancestor evidence."""

    try:
        report = verify_m7_signoff(
            report_path,
            path_migration_report=path_migration_report,
            m6_packet=m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
        )
    except (M7SignoffError, OSError, ValidationError, ValueError) as error:
        _m7_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M7 verification: PASS: {report_path.resolve()}"
    )


@m2_app.command("signoff")
def m2_signoff(
    run_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="RUN_DIRECTORY"),
    ],
    calibration_directory: Annotated[
        Path,
        typer.Option(
            "--calibration-directory",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    m1_packet: Annotated[
        Path,
        typer.Option(
            "--m1-packet",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create and immediately verify the final commit-bound M2 packet."""

    try:
        path, report = run_m2_signoff(
            run_directory,
            calibration_directory,
            m1_packet,
            repository_root=repository_root,
        )
    except (M2SignoffError, OSError, ValidationError, ValueError) as error:
        _m2_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M2 sign-off: PASS: {path}"
    )


@m2_app.command("verify")
def m2_verify(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT"),
    ],
    calibration_directory: Annotated[
        Path,
        typer.Option(
            "--calibration-directory",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    m1_packet: Annotated[
        Path,
        typer.Option(
            "--m1-packet",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recompute and verify every identity in an M2 packet."""

    try:
        report = verify_m2_signoff(
            report_path,
            calibration_directory=calibration_directory,
            m1_packet=m1_packet,
            repository_root=repository_root,
        )
    except (M2SignoffError, OSError, ValidationError, ValueError) as error:
        _m2_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M2 verification: PASS: {report_path.resolve()}"
    )


@m3_app.command("signoff")
def m3_signoff(
    run_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="RUN_DIRECTORY"),
    ],
    m2_packet: Annotated[
        Path,
        typer.Option("--m2-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create and immediately verify the final commit-bound M3 packet."""

    try:
        path, report = run_m3_signoff(
            run_directory,
            m2_packet,
            repository_root=repository_root,
        )
    except (M3SignoffError, OSError, ValidationError, ValueError) as error:
        _m3_failure(error, json_output)
    payload = {"status": report.status, "report": str(path), "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M3 sign-off: PASS: {path}"
    )


@m3_app.command("verify")
def m3_verify(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="REPORT"),
    ],
    m2_packet: Annotated[
        Path,
        typer.Option("--m2-packet", exists=True, dir_okay=False, readable=True),
    ],
    repository_root: Annotated[
        Path,
        typer.Option("--repository-root", exists=True, file_okay=False, readable=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconstruct and verify every identity in an M3 packet."""

    try:
        report = verify_m3_signoff(
            report_path,
            m2_packet=m2_packet,
            repository_root=repository_root,
        )
    except (M3SignoffError, OSError, ValidationError, ValueError) as error:
        _m3_failure(error, json_output)
    payload = {"status": report.status, "report_hash": report.report_hash}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M3 verification: PASS: {report_path.resolve()}"
    )


@app.command("init")
def baseline_init(
    project: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, metavar="PROJECT"),
    ],
    runs_root: Annotated[Path, typer.Option("--runs-root", file_okay=False)] = Path("runs"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Snapshot a generated benchmark into a durable content-addressed run."""

    try:
        initialized = initialize_run(project, runs_root=runs_root)
    except (BaselineFlowError, OSError, ValidationError, ValueError) as error:
        _baseline_failure(error, json_output)
    payload = {
        "status": "INITIALIZED",
        "run_id": initialized.run_id,
        "run_directory": str(initialized.run_directory),
        "design_contract_hash": initialized.design_contract_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA baseline initialized: {initialized.run_directory}"
    )


@app.command("analyze")
def baseline_analyze(
    run_directory: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            readable=True,
            metavar="RUN_DIRECTORY",
        ),
    ],
    stages: Annotated[
        str,
        typer.Option(
            "--stages",
            help=(
                "Comma-separated complete baseline stage set, or "
                "evidence,opportunities for M3."
            ),
        ),
    ] = "yosys,opensta,binding,clock,cdc,formal-smoke,openroad",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute or resume a complete baseline or M3 evidence stage set."""

    try:
        selected = tuple(item.strip().lower() for item in stages.split(",") if item.strip())
        if frozenset(selected) == frozenset({"evidence", "opportunities"}):
            evidence = analyze_evidence_run(run_directory, requested_stages=selected)
        else:
            analyze_run(run_directory, requested_stages=selected)
            evidence = inspect_baseline_run(run_directory)
    except (
        BaselineFlowError,
        EvidenceExecutionError,
        OSError,
        ValidationError,
        ValueError,
    ) as error:
        _baseline_failure(error, json_output)
    payload = evidence.model_dump(mode="json")
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA baseline analysis: {evidence.status}: {run_directory.resolve()}"
    )


@benchmark_app.command("generate")
def benchmark_generate(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ] = Path("benchmark/generator/benchmark.yaml"),
    profile: Annotated[str, typer.Option("--profile")] = "full",
    output: Annotated[
        Path,
        typer.Option("--output", help="New immutable benchmark snapshot directory."),
    ] = Path("benchmark/build/generated"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generate one profile and atomically publish its content-addressed snapshot."""

    try:
        resolved = load_benchmark_config(config, profile)
        snapshot = generate_benchmark(resolved, output)
        validation = validate_benchmark(snapshot, snapshot.expectations)
    except (FileNotFoundError, OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        _benchmark_failure(error, json_output)
    payload = {
        "status": validation.status,
        "profile": snapshot.profile,
        "expected_master_clocks": snapshot.expected_master_clocks,
        "expected_generated_per_master": snapshot.expected_generated_per_master,
        "expected_generated_total": snapshot.expected_generated_total,
        "config_hash": snapshot.config_hash,
        "template_hash": snapshot.template_hash,
        "source_hash": snapshot.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "output": str(output.resolve()),
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA benchmark generation: {validation.status}: {output.resolve()}"
    )


@benchmark_app.command("calibrate")
def benchmark_calibrate(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ] = Path("benchmark/generator/benchmark.yaml"),
    output: Annotated[
        Path,
        typer.Option("--output", help="New immutable full calibration evidence directory."),
    ] = Path("benchmark/build/full-calibration"),
    max_samples: Annotated[
        int,
        typer.Option("--max-samples", min=1, max=12),
    ] = 12,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Calibrate only the full profile's workload scale against locked ASAP7 Yosys."""

    try:
        resolved = load_benchmark_config(config, "full")
        result = run_full_calibration(resolved, output, max_samples=max_samples)
    except (
        CalibrationError,
        FileExistsError,
        OSError,
        ValidationError,
        ValueError,
        yaml.YAMLError,
    ) as error:
        _benchmark_failure(error, json_output)
    payload = {
        "status": result.report.status,
        "selected_workload_scale": result.report.selected_workload_scale,
        "samples": [
            {
                "workload_scale": sample.workload_scale,
                "mapped_cell_count": sample.mapped_cell_count,
                "config_hash": sample.config_hash,
                "source_hash": sample.source_hash,
                "recipe_hash": sample.recipe_hash,
                "tool_build_hash": sample.tool_fingerprint.build_hash,
            }
            for sample in result.report.samples
        ],
        "report_hash": result.report.report_hash,
        "report_artifact": result.report_artifact.model_dump(mode="json"),
        "validation_hash": (
            result.validation.evidence_hash if result.validation is not None else None
        ),
        "timing_violation_family_ids": (
            result.validation.timing_violation_family_ids
            if result.validation is not None
            else ()
        ),
        "output": str(result.output),
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else (
            "NOVA full benchmark calibration: "
            f"{result.report.status}: scale={result.report.selected_workload_scale}: "
            f"{result.output}"
        )
    )


def _m1_signoff_failure(error: Exception, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"status": "FAIL", "milestone": "M1", "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"NOVA M1 sign-off: FAIL: {error}", err=True)
    raise typer.Exit(2)


@toolchain_app.command("hydrate")
def toolchain_hydrate(
    manifest: Annotated[
        Path, typer.Option("--manifest", exists=True, dir_okay=False, readable=True)
    ],
    project_root: Annotated[Path | None, typer.Option("--project-root", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit deterministic JSON.")] = False,
) -> None:
    """Acquire the reviewed sources and publish a receipt only after every probe passes."""

    try:
        loaded, root = _tool_root(manifest, project_root or Path.cwd())
        hydrate_toolchain(loaded, root)
        receipt = create_toolchain_receipt(loaded.manifest, root)
    except (HydrationError, ToolchainVerificationError) as error:
        _toolchain_failure(error, json_output)
    payload = {"status": "PASS", "receipt": str(receipt)}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) if json_output else str(receipt)
    )


@m0_app.command("signoff")
def m0_signoff(
    toolchain_manifest: Annotated[
        Path,
        typer.Option("--toolchain-manifest", exists=True, dir_okay=False, readable=True),
    ] = Path("config/platform/toolchain-sources.json"),
    selection_policy: Annotated[
        Path,
        typer.Option("--selection-policy", exists=True, dir_okay=False, readable=True),
    ] = Path("config/platform/platform-selection-policy.yaml"),
    platform_lock: Annotated[
        Path,
        typer.Option("--platform-lock", exists=True, dir_okay=False, readable=True),
    ] = Path("config/platform/platform.lock.yaml"),
    analysis_views: Annotated[
        Path,
        typer.Option("--analysis-views", exists=True, dir_okay=False, readable=True),
    ] = Path("config/analysis/views.yaml"),
    organizer_decisions: Annotated[
        Path,
        typer.Option("--organizer-decisions", exists=True, dir_okay=False, readable=True),
    ] = Path("config/challenge/organizer_decisions.yaml"),
    default_policy: Annotated[
        Path,
        typer.Option("--default-policy", exists=True, dir_okay=False, readable=True),
    ] = Path("config/policy/default.yaml"),
    cdc_patterns: Annotated[
        Path,
        typer.Option("--cdc-patterns", exists=True, dir_okay=False, readable=True),
    ] = Path("config/policy/cdc_patterns.yaml"),
    reset_assumptions: Annotated[
        Path,
        typer.Option("--reset-assumptions", exists=True, dir_okay=False, readable=True),
    ] = Path("config/formal/reset_assumptions.yaml"),
    smoke_rtl: Annotated[
        Path,
        typer.Option("--smoke-rtl", exists=True, dir_okay=False, readable=True),
    ] = Path("tests/integration/tiny/rtl/two_flop_smoke.sv"),
    smoke_constraints: Annotated[
        Path,
        typer.Option("--smoke-constraints", exists=True, dir_okay=False, readable=True),
    ] = Path("tests/integration/tiny/constraints/two_flop_smoke.sdc"),
    output: Annotated[
        Path,
        typer.Option("--output", help="New immutable M0 evidence directory."),
    ] = Path("runs/m0-signoff"),
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", file_okay=False),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run all M0 gates and atomically publish their evidence packet."""

    try:
        report_path, report = run_m0_signoff(
            M0SignoffRequest(
                project_root=project_root or Path.cwd(),
                toolchain_manifest=toolchain_manifest,
                selection_policy=selection_policy,
                platform_lock=platform_lock,
                analysis_views=analysis_views,
                organizer_decisions=organizer_decisions,
                default_policy=default_policy,
                cdc_patterns=cdc_patterns,
                reset_assumptions=reset_assumptions,
                smoke_rtl=smoke_rtl,
                smoke_constraints=smoke_constraints,
                output_directory=output,
            )
        )
    except (
        HydrationError,
        OSError,
        SignoffError,
        SmokeError,
        ToolchainVerificationError,
        ValueError,
    ) as error:
        _signoff_failure(error, json_output)
    payload = {
        "status": report.status,
        "milestone": report.milestone,
        "report": str(report_path.resolve()),
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M0 sign-off: PASS: {report_path.resolve()}"
    )


@m1_app.command("signoff")
def m1_signoff(
    schemas: Annotated[
        Path,
        typer.Option("--schemas", file_okay=False),
    ] = Path("schemas/canonical"),
    replay_fixture: Annotated[
        Path,
        typer.Option("--replay-fixture", file_okay=False),
    ] = Path("tests/fixtures/runs/minimal"),
    output: Annotated[
        Path,
        typer.Option("--output", help="New immutable M1 evidence directory."),
    ] = Path("runs/m1-signoff"),
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", file_okay=False),
    ] = None,
    python_executable: Annotated[
        Path | None,
        typer.Option("--python", exists=True, dir_okay=False),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run every M1 gate and atomically publish its evidence packet."""

    try:
        root = (project_root or Path.cwd()).resolve()

        def rooted(path: Path) -> Path:
            return path if path.is_absolute() else root / path

        report_path, report = run_m1_signoff(
            M1SignoffRequest(
                project_root=root,
                schema_directory=rooted(schemas),
                replay_fixture=rooted(replay_fixture),
                output_directory=rooted(output),
                python_executable=python_executable or Path(sys.executable),
            )
        )
    except (M1SignoffError, OSError, ValueError) as error:
        _m1_signoff_failure(error, json_output)
    payload = {
        "status": report.status,
        "milestone": report.milestone,
        "report": str(report_path.resolve()),
        "implementation_commit": report.implementation_commit,
        "replay_digest": report.replay_digest,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M1 sign-off: PASS: {report_path.resolve()}"
    )


@m1_app.command("verify")
def m1_verify(
    packet: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True),
    ],
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", file_okay=False),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify an existing M1 packet without rerunning tests or external tools."""

    try:
        report = verify_m1_signoff_packet(
            packet,
            project_root=project_root or Path.cwd(),
        )
    except (M1SignoffError, OSError, ValueError) as error:
        _m1_signoff_failure(error, json_output)
    payload = {
        "status": report.status,
        "milestone": report.milestone,
        "implementation_commit": report.implementation_commit,
        "replay_digest": report.replay_digest,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA M1 verification: PASS: {packet.resolve()}"
    )


@toolchain_app.command("verify")
def toolchain_verify(
    manifest: Annotated[
        Path, typer.Option("--manifest", exists=True, dir_okay=False, readable=True)
    ],
    project_root: Annotated[Path | None, typer.Option("--project-root", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit deterministic JSON.")] = False,
) -> None:
    """Verify installed bytes and probes without any network access."""

    try:
        loaded, root = _tool_root(manifest, project_root or Path.cwd())
        verified = verify_toolchain(loaded.manifest, root)
    except (HydrationError, ToolchainVerificationError) as error:
        _toolchain_failure(error, json_output)
    payload = {"status": "PASS", "receipt": str(verified.receipt_path)}
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else "NOVA toolchain: PASS"
    )


@toolchain_app.command("env")
def toolchain_env(
    manifest: Annotated[
        Path, typer.Option("--manifest", exists=True, dir_okay=False, readable=True)
    ],
    project_root: Annotated[Path | None, typer.Option("--project-root", file_okay=False)] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit canonical environment JSON.")
    ] = False,
) -> None:
    """Verify offline, then emit sourceable POSIX exports for the verified install."""

    try:
        loaded, root = _tool_root(manifest, project_root or Path.cwd())
        verified = verify_toolchain(loaded.manifest, root)
        if json_output:
            payload = {
                "status": "PASS",
                "environment": {
                    name: (
                        {
                            "operation": "SET_LITERAL",
                            "literal_value": verified.literal_environment[name],
                        }
                        if verified.environment_operations[name] == "SET_LITERAL"
                        else {
                            "operation": verified.environment_operations[name],
                            "paths": list(verified.canonical_environment[name]),
                        }
                    )
                    for name in verified.environment_operations
                },
            }
            typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        else:
            typer.echo(
                render_shell_environment(
                    verified.canonical_environment,
                    verified.environment_operations,
                    verified.literal_environment,
                ),
                nl=False,
            )
    except (HydrationError, ToolchainVerificationError) as error:
        _toolchain_failure(error, json_output)


@platform_app.command("lock")
def platform_lock(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            exists=True,
            file_okay=False,
            readable=True,
            help="Verified hydrated ORFS component root.",
        ),
    ],
    policy: Annotated[
        Path,
        typer.Option("--policy", exists=True, dir_okay=False, readable=True),
    ],
    toolchain_manifest: Annotated[
        Path,
        typer.Option(
            "--toolchain-manifest",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = Path("config/platform/toolchain-sources.json"),
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", file_okay=False),
    ] = None,
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False, help="Destination platform lock."),
    ] = Path("config/platform/platform.lock.yaml"),
    views_output: Annotated[
        Path,
        typer.Option("--views-output", dir_okay=False, help="Destination analysis views."),
    ] = Path("config/analysis/views.yaml"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Seal exact ASAP7 collateral from the verified hydrated ORFS checkout."""

    try:
        loaded, tool_root = _tool_root(toolchain_manifest, project_root or Path.cwd())
        verified = verify_toolchain(loaded.manifest, tool_root)
        selected_policy = load_platform_selection_policy(policy)
        supplied_root = root.resolve(strict=True)
        expected_root = (verified.root / "components" / selected_policy.orfs_component_id).resolve(
            strict=True
        )
        if supplied_root != expected_root:
            raise PlatformLockError("--root must be the verified hydrated ORFS component")
        source = next(
            (
                component
                for component in loaded.manifest.components
                if component.component_id == selected_policy.orfs_component_id
            ),
            None,
        )
        if source is None or source.source_kind != "GIT":
            raise PlatformLockError("toolchain manifest has no verified ORFS Git source")
        if source.git_commit != selected_policy.orfs_commit:
            raise PlatformLockError(
                "selection policy ORFS commit does not match toolchain manifest"
            )
        verified_orfs_tree_identity = verified.component_tree_identities.get(
            selected_policy.orfs_component_id
        )
        if verified_orfs_tree_identity is None:
            raise PlatformLockError("verified toolchain has no ORFS tree identity")
        lock = create_platform_lock(
            PlatformLockRequest(
                orfs_root=supplied_root,
                policy=selected_policy,
                source_manifest_hash=loaded.content_identity_hash,
                verified_orfs_tree_identity=verified_orfs_tree_identity,
                host=loaded.manifest.host,
                tool_fingerprints=tuple(verified.tool_fingerprints.values()),
                generated_at=datetime.now(UTC),
            )
        )
        verification = verify_platform_lock(lock)
        if verification.status != "PASS":
            details = ", ".join(f"{issue.code}:{issue.subject}" for issue in verification.issues)
            raise PlatformLockError(f"new platform lock failed verification: {details}")
        publish_platform_outputs(lock, output, views_output)
    except (
        HydrationError,
        OSError,
        PlatformLockError,
        ToolchainVerificationError,
        ValueError,
    ) as error:
        _platform_failure(error, json_output)
    payload = {
        "status": "PASS",
        "platform": lock.platform_id,
        "platform_lock": str(output.resolve()),
        "analysis_views": str(views_output.resolve()),
        "content_identity_hash": lock.content_identity_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"NOVA platform lock: PASS: {output.resolve()}"
    )


@app.command()
def version() -> None:
    """Print the installed NOVA-RTL version."""

    typer.echo(f"NOVA-RTL {__version__}")


@app.command("report")
def report_command(
    report_bundle: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, readable=True, metavar="REPORT_BUNDLE"),
    ],
    evidence_root: Annotated[
        Path,
        typer.Option("--evidence-root", exists=True, file_okay=False, readable=True),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify and inspect an auditable M9 report bundle."""

    try:
        bundle = verify_report_bundle(report_bundle, evidence_root=evidence_root)
        model = build_view_model(bundle)
    except (OSError, ValidationError, ValueError, ReportIntegrityError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": "PASS",
        "report_bundle_id": bundle.report_bundle_id,
        "bundle_hash": bundle.bundle_hash,
        "view_count": len(model.views),
        "model_hash": model.model_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else render_text_dashboard(model)
    )


@app.command("replay")
def replay_command(
    replay_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="REPLAY_DIRECTORY"),
    ],
    offline: Annotated[bool, typer.Option("--offline")] = True,
    verify_all_artifacts: Annotated[
        bool, typer.Option("--verify-all-artifacts")
    ] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Replay a sealed report without network or EDA execution."""

    if not offline or not verify_all_artifacts:
        _optimization_failure(
            OfflineReplayError("M9 replay requires offline, complete artifact verification"),
            json_output,
        )
    try:
        manifest = verify_offline_replay(replay_directory)
        model = build_view_model_from_replay(replay_directory)
    except (OSError, ValidationError, ValueError, OfflineReplayError) as error:
        _optimization_failure(error, json_output)
    payload = {
        "status": "PASS",
        "replay_id": manifest.replay_id,
        "manifest_hash": manifest.manifest_hash,
        "model_hash": model.model_hash,
        "external_calls": 0,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else render_text_dashboard(model)
    )


@app.command("demo")
def demo_command(
    replay_directory: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, metavar="REPLAY_DIRECTORY"),
    ],
    headless: Annotated[bool, typer.Option("--headless")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Open or render the seven-view evaluator demo from sealed replay evidence."""

    try:
        model = build_view_model_from_replay(replay_directory)
    except (OSError, ValidationError, ValueError, OfflineReplayError) as error:
        _optimization_failure(error, json_output)
    if not headless:
        typer.echo(
            "Interactive rendering uses the same canonical view model as this portable CLI."
        )
    payload = {
        "status": "PASS",
        "mode": "headless" if headless else "interactive-portable",
        "run_id": model.run_id,
        "view_count": len(model.views),
        "model_hash": model.model_hash,
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else render_text_dashboard(model)
    )


@app.command()
def doctor(
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Logical dependency to check; repeat for multiple tools."),
    ] = None,
    platform_lock: Annotated[
        Path | None,
        typer.Option("--platform-lock", help="Optional platform lock to fingerprint."),
    ] = None,
    toolchain_manifest: Annotated[
        Path | None,
        typer.Option(
            "--toolchain-manifest",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Verify and use only executables from this hydrated toolchain manifest.",
        ),
    ] = None,
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", file_okay=False, help="Project containing .nova-tools."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the canonical JSON report."),
    ] = False,
) -> None:
    """Check required EDA executables and an optional platform lock."""

    verified = None
    if toolchain_manifest is not None:
        try:
            loaded, root = _tool_root(toolchain_manifest, project_root or Path.cwd())
            verified = verify_toolchain(loaded.manifest, root)
        except (HydrationError, ToolchainVerificationError) as error:
            _toolchain_failure(error, json_output)
    report = run_doctor(
        required_tools=tuple(tool) if tool else DEFAULT_REQUIRED_TOOLS,
        platform_lock=platform_lock,
        hydrated_tools=verified.tool_paths if verified is not None else None,
        probe_environment=verified.execution_environment() if verified is not None else None,
        platform_artifact_root=(
            verified.root / "components" / "orfs" if verified is not None else None
        ),
    )
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        for check in report.checks:
            typer.echo(f"[{check.status}] {check.name}: {check.message}")
        typer.echo(f"NOVA doctor: {report.status}")
    raise typer.Exit(report.exit_code)


def main() -> None:
    """Run the Typer application."""

    app()


if __name__ == "__main__":
    main()
