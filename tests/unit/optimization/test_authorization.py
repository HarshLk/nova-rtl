from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import (
    OpportunitySeverity,
    OptimizationOpportunity,
    RootCause,
)
from nova_rtl.evidence.models import SourceSpanRecord
from nova_rtl.evidence.opportunities import RankedOpportunitySet
from nova_rtl.evidence.source_map import SourceMapSnapshot
from nova_rtl.optimization.authority import (
    TransformAuthorizationError,
    select_priority_mux_authority,
)
from nova_rtl.transforms.priority_mux import RestructurePriorityMux
from nova_rtl.transforms.syntax import ParsedSlangAst, SyntaxNodeRange


def _hash_text(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _span(
    span_id: str,
    *,
    path: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    owner: str,
    text: str,
) -> SourceSpanRecord:
    return SourceSpanRecord(
        source_span_id=span_id,
        rtl_snapshot_hash=_hash("1"),
        relative_path=path,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
        owner_hierarchy=owner,
        source_text_hash=_hash_text(text),
        mapping_confidence=1.0,
        protected=False,
        protection_kinds=(),
    )


def _opportunity(
    opportunity_id: str,
    span: SourceSpanRecord,
    *,
    domain: str,
    snapshot_hash: str,
) -> OptimizationOpportunity:
    evidence = EvidenceRef(
        evidence_id=span.source_span_id,
        kind="SOURCE_SPAN",
        artifact_id="evidence_source_map",
        json_pointer=f"/source_spans/{span.source_span_id}",
        snapshot_hash=snapshot_hash,
    )
    return OptimizationOpportunity(
        opportunity_id=opportunity_id,
        parent_candidate_id="baseline",
        target_domain=domain,
        target_analysis_view_id="asap7_setup",
        affected_analysis_view_ids=("asap7_setup",),
        root_causes=(RootCause(category="DEEP_PRIORITY_CHAIN", confidence=0.9),),
        severity=OpportunitySeverity(
            worst_view_id="asap7_setup",
            worst_slack_ns=-0.1,
            affected_endpoints=1,
            tns_share_percent=1.0,
        ),
        editability="RTL_EDITABLE",
        source_spans=(span.source_span_id,),
        protected_neighbors=(),
        eligible_transform_families=("RESTRUCTURE_PRIORITY_MUX",),
        proof_contracts=("STRICT_SEQ_EQUIV",),
        evidence_refs=(evidence,),
    )


def _inputs(tmp_path: Path) -> tuple[RankedOpportunitySet, SourceMapSnapshot, ParsedSlangAst]:
    source = """module mux;
  logic out;
  always_comb begin
    if (a) out = x;
    else if (b) out = y;
    else out = z;
  end
endmodule
"""
    path = "rtl/mux.sv"
    destination = tmp_path / path
    destination.parent.mkdir(parents=True)
    destination.write_text(source)
    unrelated = _span(
        "source_unrelated",
        path=path,
        start_line=2,
        start_column=3,
        end_line=2,
        end_column=13,
        owner="u_schedule/u_mux/_unrelated_",
        text="logic out;",
    )
    selected_text = "if (a) out = x;\n    else if (b) out = y;\n    else out = z;"
    selected = _span(
        "source_authorized",
        path=path,
        start_line=4,
        start_column=5,
        end_line=6,
        end_column=18,
        owner="u_ingress/u_mux/_selected_",
        text=selected_text,
    )
    evidence_hash = _hash("9")
    first = _opportunity(
        "opportunity_unrelated", unrelated, domain="schedule", snapshot_hash=evidence_hash
    )
    second = _opportunity(
        "opportunity_authorized", selected, domain="ingress", snapshot_hash=evidence_hash
    )
    ranked = RankedOpportunitySet.model_construct(
        evidence_snapshot_hash=evidence_hash,
        policy_hash=_hash("2"),
        opportunities=(first, second),
        priority_scores={first.opportunity_id: 2.0, second.opportunity_id: 1.0},
        ranking_hash=_hash("3"),
    )
    source_map = SourceMapSnapshot.model_construct(
        candidate_id="baseline",
        rtl_snapshot_hash=_hash("1"),
        synthesis_structure_hash=_hash("4"),
        mapped_objects=(),
        connectivity_edges=(),
        source_spans=(unrelated, selected),
        source_map_hash=_hash("5"),
    )
    node = SyntaxNodeRange(
        kind="ConditionalStatement",
        relative_path=path,
        start_line=4,
        start_column=5,
        end_line=6,
        end_column=18,
    )
    parsed = ParsedSlangAst(nodes=(node,), ast_hash=_hash("6"))
    return ranked, source_map, parsed


def test_selection_uses_the_opportunity_that_authorizes_the_exact_ast_node(
    tmp_path: Path,
) -> None:
    ranked, source_map, parsed = _inputs(tmp_path)

    authorization = select_priority_mux_authority(
        project_root=tmp_path,
        editable_path_patterns=("rtl/*.sv",),
        protected_path_patterns=(),
        parsed=parsed,
        capability=RestructurePriorityMux(),
        ranked=ranked,
        source_map=source_map,
    )

    assert authorization.opportunity.opportunity_id == "opportunity_authorized"
    assert authorization.source_span.source_span_id == "source_authorized"
    assert authorization.evidence_ref == authorization.opportunity.evidence_refs[0]
    assert authorization.context.clock_domain_ids == ("ingress",)
    assert authorization.context.owner_hierarchy == "u_ingress/u_mux/_selected_"


def test_selection_fails_closed_when_no_opportunity_authorizes_the_ast_node(
    tmp_path: Path,
) -> None:
    ranked, source_map, parsed = _inputs(tmp_path)
    ranked = ranked.model_copy(
        update={"opportunities": (ranked.opportunities[0],)},
    )

    with pytest.raises(TransformAuthorizationError, match="no M3 opportunity"):
        select_priority_mux_authority(
            project_root=tmp_path,
            editable_path_patterns=("rtl/*.sv",),
            protected_path_patterns=(),
            parsed=parsed,
            capability=RestructurePriorityMux(),
            ranked=ranked,
            source_map=source_map,
        )
