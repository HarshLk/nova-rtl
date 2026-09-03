"""EQY equivalence adapter and proof-outcome parser."""

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
    complete_pass = "[status] PASS" in text or "DONE (PASS, rc=0)" in text
    if complete_pass and "Successfully proved designs equivalent" in text:
        return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())
    if "[status] FAIL" in text and "counterexample" in text.lower():
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="FORMAL_COUNTEREXAMPLE",
                    message="EQY produced a counterexample and a complete FAIL status",
                ),
            ),
        )
    if "[status] UNKNOWN" in text and "unknown" in text.lower():
        return ParsedAdapterResult(
            status="INCONCLUSIVE",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="FORMAL_INCONCLUSIVE",
                    message="EQY completed with an unknown proof outcome",
                    severity="WARNING",
                ),
            ),
        )
    return infrastructure_result(
        context,
        code="INFRASTRUCTURE_MISSING_REPORT_SECTION",
        message="EQY log does not contain a complete recognized proof status",
    )


class EQYAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"eqy"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["EQYAdapter", "parse_report"]
