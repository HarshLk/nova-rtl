"""OpenSTA timing adapter and semantic setup/hold result parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

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
from nova_rtl.contracts.analysis import CriticalPathRecord
from nova_rtl.contracts.base import canonical_sha256

_FLOAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_START = re.compile(
    r"^Startpoint:\s+(?P<object>\S+)(?:[ \t]+\((?P<inline>[^\n)]+)\)"
    r"|[ \t]*\n[ \t]+\((?P<next>[^\n)]+)\))",
    re.MULTILINE,
)
_END = re.compile(
    r"^Endpoint:\s+(?P<object>\S+)(?:[ \t]+\((?P<inline>[^\n)]+)\)"
    r"|[ \t]*\n[ \t]+\((?P<next>[^\n)]+)\))",
    re.MULTILINE,
)
_PATH_GROUP = re.compile(r"^Path Group:\s+(?P<group>\S.*?)\s*$", re.MULTILINE)
_PATH_TYPE = re.compile(r"^Path Type:\s+(?P<kind>max|min)\s*$", re.MULTILINE)
_POINT = re.compile(
    rf"^\s*(?P<delay>{_FLOAT})\s+{_FLOAT}\s+(?:\^|v)?\s*"
    r"(?P<object>\S+)\s+\([^)]+\)\s*$",
    re.MULTILINE,
)
_MAX_ARITHMETIC_ROUNDING_NS = Decimal("0.000100")


class CriticalPathParseError(ValueError):
    """A detailed OpenSTA path report cannot form trustworthy path evidence."""


def _required_match(pattern: re.Pattern[str], block: str, label: str) -> re.Match[str]:
    match = pattern.search(block)
    if match is None:
        raise CriticalPathParseError(f"timing path is missing {label}")
    return match


def _first_decimal(block: str, label: str) -> Decimal:
    matches = re.findall(rf"^\s*({_FLOAT})\s+{re.escape(label)}\s*$", block, re.MULTILINE)
    if not matches:
        raise CriticalPathParseError(f"timing path is missing {label}")
    return Decimal(matches[0])


def _clock_name(description: str) -> str | None:
    for pattern in (r"\bclocked by\s+(\S+)", r"\bclock\s+(\S+)"):
        match = re.search(pattern, description)
        if match is not None:
            return match.group(1)
    return None


def _description(match: re.Match[str]) -> str:
    return match.group("inline") or match.group("next")


def _mapped_object(name: str, description: str) -> str:
    kind = "port" if " port" in description else "cell"
    return f"{kind}:{name}"


def _path_blocks(text: str) -> tuple[str, ...]:
    starts = tuple(re.finditer(r"^Startpoint:", text, re.MULTILINE))
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        summary = text.find("\nNOVA_OPENSTA_SUMMARY", start.start(), end)
        if summary >= 0:
            end = summary
        blocks.append(text[start.start() : end])
    return tuple(blocks)


def _parse_path(
    block: str,
    *,
    candidate_id: str,
    analysis_view_id: str,
    raw_report_artifact_id: str,
    clock_ids_by_name: Mapping[str, str],
) -> CriticalPathRecord:
    start = _required_match(_START, block, "startpoint")
    endpoint = _required_match(_END, block, "endpoint")
    path_group = _required_match(_PATH_GROUP, block, "path group").group("group")
    path_type = _required_match(_PATH_TYPE, block, "path type").group("kind")

    start_description = _description(start)
    endpoint_description = _description(endpoint)
    launch_name = _clock_name(start_description)
    capture_name = _clock_name(endpoint_description)
    if launch_name is None or launch_name not in clock_ids_by_name:
        raise CriticalPathParseError(f"unresolved launch clock: {launch_name or '<missing>'}")
    if capture_name is None or capture_name not in clock_ids_by_name:
        raise CriticalPathParseError(f"unresolved capture clock: {capture_name or '<missing>'}")

    arrival_value = _first_decimal(block, "data arrival time")
    required_value = _first_decimal(block, "data required time")
    slack_match = re.search(
        rf"^\s*({_FLOAT})\s+slack\s+\((?:MET|VIOLATED)\)\s*$",
        block,
        re.MULTILINE,
    )
    if slack_match is None:
        raise CriticalPathParseError("timing path is missing slack")
    slack_value = Decimal(slack_match.group(1))
    expected_slack = (
        required_value - arrival_value
        if path_type == "max"
        else arrival_value - required_value
    )
    arithmetic_error = abs(expected_slack - slack_value)
    if arithmetic_error > _MAX_ARITHMETIC_ROUNDING_NS:
        raise CriticalPathParseError("timing path slack arithmetic is inconsistent")
    arrival = float(arrival_value)
    slack = float(slack_value)
    # OpenSTA reports six decimal places from separately accumulated values.
    # Preserve raw values when they satisfy the canonical tolerance; otherwise
    # normalize only the final-place rounding discrepancy.
    if arithmetic_error < Decimal("0.000001"):
        required = float(required_value)
    elif path_type == "max":
        required = float(arrival_value + slack_value)
    else:
        required = float(arrival_value - slack_value)

    data_section = block.split("data arrival time", maxsplit=1)[0]
    points = tuple(_POINT.finditer(data_section))
    if not points:
        raise CriticalPathParseError("timing path contains no mapped object sequence")
    object_sequence = tuple(f"pin:{point.group('object')}" for point in points)
    cell_delay = round(sum(max(float(point.group("delay")), 0.0) for point in points), 6)
    net_delay = round(max(arrival - cell_delay, 0.0), 6)
    logic_depth = sum(float(point.group("delay")) > 0.0 for point in points)
    canonical_start = _mapped_object(start.group("object"), start_description)
    canonical_end = _mapped_object(endpoint.group("object"), endpoint_description)
    identity = canonical_sha256(
        {
            "analysis_view_id": analysis_view_id,
            "candidate_id": candidate_id,
            "capture_clock_id": clock_ids_by_name[capture_name],
            "endpoint": canonical_end,
            "launch_clock_id": clock_ids_by_name[launch_name],
            "object_sequence": object_sequence,
            "path_group": path_group,
            "startpoint": canonical_start,
        }
    ).removeprefix("sha256:")
    try:
        return CriticalPathRecord(
            path_id=f"path_{identity[:24]}",
            candidate_id=candidate_id,
            analysis_view_id=analysis_view_id,
            path_type=path_type.upper(),
            path_group=path_group,
            launch_clock_id=clock_ids_by_name[launch_name],
            capture_clock_id=clock_ids_by_name[capture_name],
            startpoint=canonical_start,
            endpoint=canonical_end,
            arrival_ns=arrival,
            required_ns=required,
            slack_ns=slack,
            cell_delay_ns=cell_delay,
            net_delay_ns=net_delay,
            logic_depth=logic_depth,
            max_fanout=0,
            object_sequence=object_sequence,
            source_span_refs=(),
            raw_report_artifact_id=raw_report_artifact_id,
        )
    except ValueError as error:
        raise CriticalPathParseError(f"invalid critical path record: {error}") from error


def parse_critical_paths(
    text: str,
    *,
    candidate_id: str,
    analysis_view_id: str,
    raw_report_artifact_id: str,
    clock_ids_by_name: Mapping[str, str],
    expected_path_type: Literal["MAX", "MIN"] | None = None,
) -> tuple[CriticalPathRecord, ...]:
    """Parse, validate and canonically order all detailed OpenSTA paths."""

    records = tuple(
        _parse_path(
            block,
            candidate_id=candidate_id,
            analysis_view_id=analysis_view_id,
            raw_report_artifact_id=raw_report_artifact_id,
            clock_ids_by_name=clock_ids_by_name,
        )
        for block in _path_blocks(text)
    )
    if not records:
        raise CriticalPathParseError("OpenSTA report contains no complete timing paths")
    if expected_path_type is not None and any(
        record.path_type != expected_path_type for record in records
    ):
        raise CriticalPathParseError(
            "OpenSTA timing path type does not match analysis view"
        )
    by_id = {record.path_id: record for record in records}
    if len(by_id) != len(records):
        raise CriticalPathParseError("OpenSTA report contains duplicate timing path identities")
    return tuple(by_id[path_id] for path_id in sorted(by_id))


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


__all__ = [
    "CriticalPathParseError",
    "OpenSTAAdapter",
    "parse_critical_paths",
    "parse_report",
]
