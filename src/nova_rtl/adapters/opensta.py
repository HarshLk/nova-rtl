"""OpenSTA timing adapter and semantic setup/hold result parser."""

from __future__ import annotations

from nova_rtl.adapters.base import (
    AdapterParseContext,
    BaseToolAdapter,
    ParsedAdapterResult,
    ReportParseError,
    diagnostic,
    finite_float,
    infrastructure_result,
    metric_set,
    required_field,
    strict_int,
    summary_fields,
)


def parse_report(
    text: str,
    *,
    exit_code: int,
    context: AdapterParseContext,
) -> ParsedAdapterResult:
    """Parse a complete OpenSTA summary and classify negative slack as a design failure."""

    try:
        fields = summary_fields(
            text,
            start="NOVA_OPENSTA_SUMMARY",
            end="NOVA_OPENSTA_END",
        )
        check = required_field(fields, "check")
        if check not in {"SETUP", "HOLD"}:
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                f"unsupported timing check: {check}",
            )
        failing = strict_int(fields, "failing_endpoints")
        delay = finite_float(fields, "critical_path_delay_ns")
        if failing < 0 or delay <= 0:
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                "timing counts must be nonnegative and path delay must be positive",
            )
        metric_values: dict[str, int | float | None] = {
            "failing_endpoints": failing,
            "critical_path_delay_ns": delay,
        }
        if check == "SETUP":
            wns = finite_float(fields, "setup_wns_ns")
            tns = finite_float(fields, "setup_tns_ns")
            metric_values.update(
                setup_wns_ns=wns,
                setup_tns_ns=tns,
                estimated_fmax_mhz=finite_float(fields, "estimated_fmax_mhz"),
            )
        else:
            wns = finite_float(fields, "hold_wns_ns")
            tns = finite_float(fields, "hold_tns_ns")
            metric_values.update(hold_wns_ns=wns, hold_tns_ns=tns)
    except ReportParseError as error:
        return infrastructure_result(context, code=error.code, message=str(error))
    metrics = metric_set(context, **metric_values)
    violates = wns < 0 or tns < 0 or failing > 0
    if violates:
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code=f"TIMING_{check}_VIOLATION",
                    message=(
                        f"{check.lower()} timing failed: WNS={wns:g} ns, "
                        f"TNS={tns:g} ns, endpoints={failing}"
                    ),
                ),
            ),
        )
    return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())


class OpenSTAAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"opensta"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["OpenSTAAdapter", "parse_report"]
