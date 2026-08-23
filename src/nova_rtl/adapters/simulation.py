"""RTL simulation adapter and observable-workload outcome parser."""

from __future__ import annotations

from nova_rtl.adapters.base import (
    AdapterParseContext,
    BaseToolAdapter,
    ParsedAdapterResult,
    diagnostic,
    infrastructure_result,
    metric_set,
)


def parse_report(
    text: str,
    *,
    exit_code: int,
    context: AdapterParseContext,
) -> ParsedAdapterResult:
    metrics = metric_set(context)
    if "NOVA_OBSERVABLE_ACTIVITY_PASS" in text:
        return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())
    if "NOVA_SIMULATION_FAIL" in text and "FATAL:" in text:
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="SIMULATION_ASSERTION_FAILED",
                    message="simulation completed with an explicit benchmark assertion failure",
                ),
            ),
        )
    return infrastructure_result(
        context,
        code="INFRASTRUCTURE_MISSING_REPORT_SECTION",
        message="simulation output contains neither a complete pass nor explicit design failure",
    )


class SimulationAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"iverilog", "verilator"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["SimulationAdapter", "parse_report"]
