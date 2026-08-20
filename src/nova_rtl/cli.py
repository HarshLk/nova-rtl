"""Command-line entry point for the NOVA-RTL scaffold."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from nova_rtl import __version__
from nova_rtl.contracts.platform import PlatformLockRequest
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
        lock = create_platform_lock(
            PlatformLockRequest(
                orfs_root=supplied_root,
                policy=selected_policy,
                source_manifest_hash=loaded.content_identity_hash,
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
