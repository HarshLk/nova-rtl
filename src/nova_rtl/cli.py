"""Command-line entry point for the NOVA-RTL scaffold."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from nova_rtl import __version__
from nova_rtl.platform.doctor import DEFAULT_REQUIRED_TOOLS, run_doctor

app = typer.Typer(
    name="nova",
    help="Evidence-grounded RTL timing optimization framework.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
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
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the canonical JSON report."),
    ] = False,
) -> None:
    """Check required EDA executables and an optional platform lock."""

    report = run_doctor(
        required_tools=tuple(tool) if tool else DEFAULT_REQUIRED_TOOLS,
        platform_lock=platform_lock,
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
