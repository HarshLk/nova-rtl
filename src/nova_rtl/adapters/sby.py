"""SymbiYosys adapter and bounded proof-outcome parser."""

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
    if "DONE (PASS," in text:
        return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())
    if "DONE (FAIL," in text and "counterexample" in text.lower():
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="FORMAL_PROPERTY_FAILED",
                    message="SymbiYosys completed with a counterexample",
                ),
            ),
        )
    if "DONE (UNKNOWN," in text and "unknown" in text.lower():
        return ParsedAdapterResult(
            status="INCONCLUSIVE",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="FORMAL_INCONCLUSIVE",
                    message="SymbiYosys completed with an unknown outcome",
                    severity="WARNING",
                ),
            ),
        )
    return infrastructure_result(
        context,
        code="INFRASTRUCTURE_MISSING_REPORT_SECTION",
        message="SymbiYosys log does not contain a complete DONE status",
    )


class SymbiYosysAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"sby"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["SymbiYosysAdapter", "parse_report"]
