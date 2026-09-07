from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.optimization import OptimizationProposal
from nova_rtl.evidence.models import SourceSpanRecord
from nova_rtl.search.dag import CandidateDag, CandidateDagError, replace_candidate
from nova_rtl.transforms.executor import (
    MaterializationRequest,
    TransformExecutor,
    UnauthorizedDiffError,
)
from nova_rtl.transforms.priority_mux import PriorityMuxContext, RestructurePriorityMux
from nova_rtl.transforms.registry import TransformRegistry
from nova_rtl.transforms.syntax import SourceEdit, parse_slang_ast

SOURCE = (
    "if (req[0]) grant = value[0]; "
    "else if (req[1]) grant = value[1]; else grant = fallback;"
)
FILE_TEXT = f"""module priority_mux;
  always_comb begin
    {SOURCE}
  end
endmodule
"""
START_COLUMN = 5
END_COLUMN = START_COLUMN + len(SOURCE)


def _hash(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _proposal() -> OptimizationProposal:
    evidence = (
        {
            "evidence_id": "source_priority_mux",
            "kind": "SOURCE_SPAN",
            "artifact_id": "artifact_evidence",
            "json_pointer": "/source_spans/source_priority_mux",
            "snapshot_hash": "sha256:" + "a" * 64,
        },
    )
    return OptimizationProposal.model_validate(
        {
            "proposal_id": "proposal_priority_mux",
            "parent_candidate_id": "baseline",
            "opportunity_id": "opportunity_priority_mux",
            "diagnosis_refs": ("diagnosis_priority_mux",),
            "target": {
                "hierarchy": "priority_mux",
                "source_span_id": "source_priority_mux",
                "cone_fingerprint": "cone:v1:priority_mux",
            },
            "transformation": {
                "operation": "RESTRUCTURE_PRIORITY_MUX",
                "family": "LOGIC_RESTRUCTURING",
                "parameters": {"max_branches": 8},
            },
            "preconditions": (
                "NO_PROTECTED_NODE_IN_EDIT_SET",
                "SINGLE_CLOCK_DOMAIN",
            ),
            "correctness": {
                "contract": "STRICT_SEQ_EQUIV",
                "proof_scope": "priority_mux",
                "reset_model": "NO_RESET_STATE",
            },
            "prediction": {
                "timing_direction": "IMPROVE",
                "area_direction": "SMALL_INCREASE",
                "confidence": 0.8,
            },
            "evidence_refs": evidence,
            "abort_conditions": ("FORMAL_NON_PASS",),
        }
    )


def _span() -> SourceSpanRecord:
    return SourceSpanRecord(
        source_span_id="source_priority_mux",
        rtl_snapshot_hash="sha256:" + "b" * 64,
        relative_path="rtl/priority_mux.sv",
        start_line=3,
        start_column=START_COLUMN,
        end_line=3,
        end_column=END_COLUMN,
        owner_hierarchy="priority_mux",
        source_text_hash=_hash("    " + SOURCE),
        mapping_confidence=1.0,
        protected=False,
        protection_kinds=(),
    )


def _ast() -> object:
    return {
        "design": {
            "kind": "Root",
            "node": {
                "kind": "ConditionalStatement",
                "source_file_start": "rtl/priority_mux.sv",
                "source_file_end": "rtl/priority_mux.sv",
                "source_line_start": 3,
                "source_line_end": 3,
                "source_column_start": START_COLUMN,
                "source_column_end": END_COLUMN,
            },
        }
    }


def _request(parent_root: Path) -> MaterializationRequest:
    return MaterializationRequest(
        run_id="run_m4_tiny",
        parent_candidate_id="baseline",
        parent_root=parent_root,
        source_paths=("rtl/priority_mux.sv",),
        proposal=_proposal(),
        authorized_span=_span(),
        protected_spans=(),
        parsed_ast=parse_slang_ast(_ast()),
        transform_context=PriorityMuxContext(
            relative_path="rtl/priority_mux.sv",
            source_text=SOURCE,
            start_line=3,
            start_column=START_COLUMN,
            end_line=3,
            end_column=END_COLUMN,
            owner_hierarchy="priority_mux",
            expected_owner_hierarchy="priority_mux",
            clock_domain_ids=("clk_core",),
            protected_neighbor_ids=(),
        ),
        lineage_depth=1,
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    path = parent / "rtl" / "priority_mux.sv"
    path.parent.mkdir(parents=True)
    path.write_text(FILE_TEXT)
    return parent


def test_executor_materializes_deterministic_isolated_candidate(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    executor = TransformExecutor(
        registry=TransformRegistry((RestructurePriorityMux(),)),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        candidates_root=tmp_path / "candidates",
    )

    first = executor.materialize(_request(parent))
    second = executor.materialize(_request(parent))

    assert first.candidate == second.candidate
    assert first.workspace == second.workspace
    assert first.workspace != parent
    assert parent.joinpath("rtl/priority_mux.sv").read_text() == FILE_TEXT
    rewritten = first.workspace.joinpath("rtl/priority_mux.sv").read_text()
    assert "nova_priority_match" in rewritten
    assert first.candidate.changed_spans == ("source_priority_mux",)
    assert first.candidate.rtl_snapshot_artifact.sha256 == first.candidate.source_hash
    assert first.candidate.patch_artifact.media_type == "text/x-diff"
    assert first.candidate.classification == "INCONCLUSIVE"
    assert first.candidate.terminal_disposition == "PENDING_EVALUATION"


class _MaliciousPriorityMux(RestructurePriorityMux):
    def rewrite(self, match: object, parameters: object) -> SourceEdit:
        edit = super().rewrite(match, parameters)  # type: ignore[arg-type]
        return SourceEdit(
            **{
                **edit.model_dump(mode="python"),
                "start_line": 2,
                "start_column": 3,
            }
        )


def test_executor_rejects_change_outside_authorized_span(tmp_path: Path) -> None:
    executor = TransformExecutor(
        registry=TransformRegistry((_MaliciousPriorityMux(),)),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        candidates_root=tmp_path / "candidates",
    )

    with pytest.raises(UnauthorizedDiffError, match="outside authorized span"):
        executor.materialize(_request(_parent(tmp_path)))


def test_candidate_dag_is_immutable_canonical_and_parent_checked(tmp_path: Path) -> None:
    executor = TransformExecutor(
        registry=TransformRegistry((RestructurePriorityMux(),)),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        candidates_root=tmp_path / "candidates",
    )
    candidate = executor.materialize(_request(_parent(tmp_path))).candidate
    dag = CandidateDag("baseline")
    dag.add(candidate)

    first = dag.snapshot()
    second = dag.snapshot()
    assert first == second
    assert first.candidate_ids == (candidate.candidate_id,)

    with pytest.raises(CandidateDagError, match="already exists"):
        dag.add(candidate)
    orphan = replace_candidate(candidate, parent_candidate_id="missing_parent")
    with pytest.raises(CandidateDagError, match="unknown parent"):
        CandidateDag("baseline").add(orphan)


def test_candidate_updates_are_revalidated_not_mutated(tmp_path: Path) -> None:
    executor = TransformExecutor(
        registry=TransformRegistry((RestructurePriorityMux(),)),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        candidates_root=tmp_path / "candidates",
    )
    original = executor.materialize(_request(_parent(tmp_path))).candidate
    updated = replace_candidate(original, terminal_disposition="FORMAL_PENDING")

    assert updated.terminal_disposition == "FORMAL_PENDING"
    assert original.terminal_disposition == "PENDING_EVALUATION"
    with pytest.raises(ValueError):
        replace_candidate(original, source_hash="sha256:" + "0" * 64)
