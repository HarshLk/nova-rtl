"""Evidence-bound authorization of M4 transform targets."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import OptimizationOpportunity
from nova_rtl.evidence.models import SourceSpanRecord
from nova_rtl.evidence.opportunities import RankedOpportunitySet
from nova_rtl.evidence.source_map import SourceMapSnapshot
from nova_rtl.transforms.priority_mux import (
    PriorityMuxContext,
    PriorityMuxError,
    RestructurePriorityMux,
)
from nova_rtl.transforms.syntax import ParsedSlangAst, SyntaxNodeRange


class TransformAuthorizationError(RuntimeError):
    """M3 evidence does not authorize a proposed M4 source edit."""


@dataclass(frozen=True, slots=True)
class PriorityMuxAuthorization:
    """One exact syntax node authorized by one immutable M3 opportunity."""

    opportunity: OptimizationOpportunity
    source_span: SourceSpanRecord
    evidence_ref: EvidenceRef
    syntax_node: SyntaxNodeRange
    context: PriorityMuxContext
    protected_spans: tuple[SourceSpanRecord, ...]


def source_span_text(source: str, span: SourceSpanRecord) -> str:
    """Recreate the exact source-map slice represented by ``span``."""

    lines = source.splitlines()
    selected = lines[span.start_line - 1 : span.end_line]
    if not selected:
        raise TransformAuthorizationError("M3 source span selects no current RTL text")
    selected[0] = selected[0][span.start_column - 1 :]
    if len(selected) == 1:
        selected[0] = selected[0][: max(span.end_column - span.start_column + 1, 0)]
    else:
        selected[-1] = selected[-1][: span.end_column]
    return "\n".join(selected)


def _node_text(source: str, node: SyntaxNodeRange) -> str:
    lines = source.splitlines(keepends=True)
    if node.end_line > len(lines):
        raise TransformAuthorizationError("Slang syntax node exceeds current RTL text")
    start = sum(len(item) for item in lines[: node.start_line - 1]) + node.start_column - 1
    end = sum(len(item) for item in lines[: node.end_line - 1]) + node.end_column - 1
    return source[start:end]


def _contains(span: SourceSpanRecord, node: SyntaxNodeRange) -> bool:
    return (
        span.relative_path == node.relative_path
        and (span.start_line, span.start_column) <= (node.start_line, node.start_column)
        and (span.end_line, span.end_column) >= (node.end_line, node.end_column)
    )


def select_priority_mux_authority(
    *,
    project_root: Path,
    editable_path_patterns: tuple[str, ...],
    protected_path_patterns: tuple[str, ...],
    parsed: ParsedSlangAst,
    capability: RestructurePriorityMux,
    ranked: RankedOpportunitySet,
    source_map: SourceMapSnapshot,
) -> PriorityMuxAuthorization:
    """Select only a priority mux directly authorized by the same M3 opportunity."""

    if source_map.rtl_snapshot_hash != next(iter(source_map.source_spans)).rtl_snapshot_hash:
        raise TransformAuthorizationError("M3 source-map snapshot identity is inconsistent")
    spans = {item.source_span_id: item for item in source_map.source_spans}
    protected_spans = tuple(item for item in source_map.source_spans if item.protected)
    nodes = tuple(
        sorted(
            (item for item in parsed.nodes if item.kind == "ConditionalStatement"),
            key=lambda item: (
                item.relative_path,
                item.start_line,
                item.start_column,
                -item.end_line,
                -item.end_column,
            ),
        )
    )
    for opportunity in ranked.opportunities:
        if (
            opportunity.editability != "RTL_EDITABLE"
            or "RESTRUCTURE_PRIORITY_MUX" not in opportunity.eligible_transform_families
            or opportunity.protected_neighbors
        ):
            continue
        opportunity_spans = []
        for span_id in opportunity.source_spans:
            span = spans.get(span_id)
            if span is None:
                raise TransformAuthorizationError(
                    f"M3 opportunity references missing source span: {span_id}"
                )
            opportunity_spans.append(span)
        evidence = {item.evidence_id: item for item in opportunity.evidence_refs}
        for node in nodes:
            if not any(
                fnmatch.fnmatch(node.relative_path, pattern) for pattern in editable_path_patterns
            ) or any(
                fnmatch.fnmatch(node.relative_path, pattern) for pattern in protected_path_patterns
            ):
                continue
            authorized = sorted(
                (
                    item
                    for item in opportunity_spans
                    if not item.protected and _contains(item, node)
                ),
                key=lambda item: (
                    item.start_line != node.start_line
                    or item.start_column != node.start_column
                    or item.end_line != node.end_line
                    or item.end_column != node.end_column,
                    -item.mapping_confidence,
                    item.source_span_id,
                ),
            )
            for span in authorized:
                reference = evidence.get(span.source_span_id)
                if reference is None or reference.kind != "SOURCE_SPAN":
                    raise TransformAuthorizationError(
                        "M3 authorized span lacks its immutable evidence reference"
                    )
                source_path = project_root / node.relative_path
                try:
                    source = source_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise TransformAuthorizationError(
                        f"cannot read M3-authorized RTL source: {error}"
                    ) from error
                current_span_hash = (
                    "sha256:" + sha256(source_span_text(source, span).encode()).hexdigest()
                )
                if current_span_hash != span.source_text_hash:
                    raise TransformAuthorizationError(
                        "M3-authorized source span no longer matches current RTL"
                    )
                context = PriorityMuxContext(
                    relative_path=node.relative_path,
                    source_text=_node_text(source, node),
                    start_line=node.start_line,
                    start_column=node.start_column,
                    end_line=node.end_line,
                    end_column=node.end_column,
                    owner_hierarchy=span.owner_hierarchy,
                    expected_owner_hierarchy=span.owner_hierarchy,
                    clock_domain_ids=(opportunity.target_domain,),
                    protected_neighbor_ids=opportunity.protected_neighbors,
                )
                try:
                    capability.match(context)
                except PriorityMuxError:
                    continue
                return PriorityMuxAuthorization(
                    opportunity=opportunity,
                    source_span=span,
                    evidence_ref=reference,
                    syntax_node=node,
                    context=context,
                    protected_spans=protected_spans,
                )
    raise TransformAuthorizationError(
        "no M3 opportunity authorizes a safe bounded priority mux AST node"
    )


__all__ = [
    "PriorityMuxAuthorization",
    "TransformAuthorizationError",
    "select_priority_mux_authority",
    "source_span_text",
]
