"""Yosys synthesis adapter and strict summary parser."""

from __future__ import annotations

import re

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
    """Parse the deterministic NOVA Yosys summary, failing closed on omissions."""

    try:
        if "NOVA_YOSYS_SUMMARY" in text:
            fields = summary_fields(
                text,
                start="NOVA_YOSYS_SUMMARY",
                end="NOVA_YOSYS_END",
            )
            cell_count = strict_int(fields, "cell_count")
            mapped_area = finite_float(fields, "mapped_area_um2")
            register_count = strict_int(fields, "register_count")
        else:
            cell_count, mapped_area, register_count = _native_mapped_statistics(text)
        if min(cell_count, register_count) < 0 or (mapped_area is not None and mapped_area < 0):
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                "synthesis counts and area must be nonnegative",
            )
    except ReportParseError as error:
        return infrastructure_result(context, code=error.code, message=str(error))
    metrics = metric_set(
        context,
        cell_count=cell_count,
        mapped_area_um2=mapped_area,
        register_count=register_count,
    )
    if "NOVA_YOSYS_DESIGN_FAIL" in text:
        return ParsedAdapterResult(
            status="FAIL",
            metrics=metrics,
            diagnostics=(
                diagnostic(
                    context,
                    code="SYNTHESIS_DESIGN_CHECK_FAILED",
                    message=(
                        "Yosys reported a complete synthesis result with a design check failure"
                    ),
                ),
            ),
        )
    return ParsedAdapterResult(status="PASS", metrics=metrics, diagnostics=())


def _native_mapped_statistics(text: str) -> tuple[int, float | None, int]:
    """Extract the final hierarchical ASAP7 mapping summary emitted by Yosys."""

    hierarchy_matches = tuple(
        re.finditer(
            r"^\s*(\d+)(?:\s+[0-9]+(?:\.[0-9]+)?)?\s+cells\s*$",
            text,
            re.MULTILINE,
        )
    )
    area_matches = tuple(
        re.finditer(
            r"^\s*Chip area for top module .+:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
            text,
            re.MULTILINE,
        )
    )
    count_based_area = "NOVA_YOSYS_AREA_FROM_LOCKED_CELL_COUNTS" in text
    if not hierarchy_matches or (not area_matches and not count_based_area):
        raise ReportParseError(
            "INFRASTRUCTURE_MISSING_REPORT_SECTION",
            "native Yosys output lacks final hierarchical cell count or chip area",
        )
    final_cells = hierarchy_matches[-1]
    final_area = area_matches[-1] if area_matches else None
    if final_area is not None and final_cells.start() > final_area.start():
        raise ReportParseError(
            "INFRASTRUCTURE_MALFORMED_REPORT",
            "native Yosys hierarchy summary follows its chip-area marker",
        )
    summary = text[final_cells.end() : final_area.start() if final_area is not None else len(text)]
    registers = sum(
        int(match.group(1))
        for match in re.finditer(
            r"^\s*(\d+)(?:\s+[0-9]+(?:\.[0-9]+)?)?\s+DFF\S*\s*$",
            summary,
            re.MULTILINE,
        )
    )
    if registers <= 0:
        raise ReportParseError(
            "INFRASTRUCTURE_MISSING_REPORT_SECTION",
            "native Yosys mapping summary contains no sequential cells",
        )
    mapped_area = float(final_area.group(1)) if final_area is not None else None
    if mapped_area is None:
        cell_areas = {
            "DFFASRHQNx1_ASAP7_75t_R": 0.37908,
            "DFFHQNx1_ASAP7_75t_R": 0.2916,
            "INVx1_ASAP7_75t_R": 0.04374,
            "NAND2x1_ASAP7_75t_R": 0.08748,
        }
        counts = {
            match.group(2): int(match.group(1))
            for match in re.finditer(
                r"^\s*(\d+)\s+(DFFASRHQNx1_ASAP7_75t_R|DFFHQNx1_ASAP7_75t_R|"
                r"INVx1_ASAP7_75t_R|NAND2x1_ASAP7_75t_R)\s*$",
                summary,
                re.MULTILINE,
            )
        }
        if sum(counts.values()) != int(final_cells.group(1)):
            raise ReportParseError(
                "INFRASTRUCTURE_MISSING_REPORT_SECTION",
                "native Yosys cell-area inventory does not cover all mapped cells",
            )
        mapped_area = sum(counts[name] * area for name, area in cell_areas.items())
    return (
        int(final_cells.group(1)),
        mapped_area,
        registers,
    )


class YosysAdapter(BaseToolAdapter):
    expected_tool_ids = frozenset({"yosys"})

    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        return parse_report(text, exit_code=exit_code, context=context)


__all__ = ["YosysAdapter", "parse_report"]
