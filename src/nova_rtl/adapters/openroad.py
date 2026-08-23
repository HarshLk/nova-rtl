"""OpenROAD physical adapter and strict PPA/timing summary parser."""

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
    strict_int,
    summary_fields,
)


def parse_report(
    text: str,
    *,
    exit_code: int,
    context: AdapterParseContext,
) -> ParsedAdapterResult:
    """Parse required physical and timing evidence; a crash without it fails closed."""

    try:
        fields = summary_fields(
            text,
            start="NOVA_OPENROAD_SUMMARY",
            end="NOVA_OPENROAD_END",
        )
        check = fields.get("check")
        if check not in {"SETUP", "HOLD"}:
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                "OpenROAD summary check must be exactly SETUP or HOLD",
            )
        values = {
            "setup_wns_ns": finite_float(fields, "setup_wns_ns"),
            "setup_tns_ns": finite_float(fields, "setup_tns_ns"),
            "hold_wns_ns": finite_float(fields, "hold_wns_ns"),
            "hold_tns_ns": finite_float(fields, "hold_tns_ns"),
            "failing_endpoints": strict_int(fields, "failing_endpoints"),
            "physical_area_um2": finite_float(fields, "physical_area_um2"),
            "cell_count": strict_int(fields, "cell_count"),
            "register_count": strict_int(fields, "register_count"),
            "buffer_count": strict_int(fields, "buffer_count"),
            "power_total_uw": finite_float(fields, "power_total_uw"),
            "wirelength_um": finite_float(fields, "wirelength_um"),
            "congestion_overflow": finite_float(fields, "congestion_overflow"),
        }
        nonnegative = (
            "failing_endpoints",
            "physical_area_um2",
            "cell_count",
            "register_count",
            "buffer_count",
            "power_total_uw",
            "wirelength_um",
            "congestion_overflow",
        )
        if any(values[name] < 0 for name in nonnegative):
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                "physical counts and metrics must be nonnegative",
            )
    except ReportParseError as error:
        return infrastructure_result(context, code=error.code, message=str(error))
    metrics = metric_set(context, **values)
    timing_violates = (
        values["setup_wns_ns"] < 0 or values["setup_tns_ns"] < 0
        if check == "SETUP"
        else values["hold_wns_ns"] < 0 or values["hold_tns_ns"] < 0
    )
    timing_violates = timing_violates or values["failing_endpoints"] > 0
    congestion_violates = values["congestion_overflow"] > 0
    if timing_violates or congestion_violates:
        diagnostics = []
        if timing_violates:
            diagnostics.append(
                diagnostic(
                    context,
                    code="PHYSICAL_TIMING_VIOLATION",
                    message=f"OpenROAD completed with a measured {check.lower()} violation",
                )
            )
        if congestion_violates:
            diagnostics.append(
                diagnostic(
                    context,
                    code="PHYSICAL_CONGESTION_VIOLATION",
                    message="OpenROAD completed with nonzero congestion overflow",
                )
            )
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=tuple(diagnostics),
        )
    return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())


class OpenROADAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"openroad"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["OpenROADAdapter", "parse_report"]
