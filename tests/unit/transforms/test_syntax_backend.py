from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.evidence.models import SourceSpanRecord
from nova_rtl.transforms.syntax import (
    SourceEdit,
    SyntaxBackendError,
    apply_authorized_edit,
    parse_slang_ast,
)


def _hash(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _span(source: str, *, protected: bool = False) -> SourceSpanRecord:
    selected = source.splitlines()[2:6]
    selected[0] = selected[0][2:]
    selected[-1] = selected[-1][:18]
    return SourceSpanRecord(
        source_span_id="span_priority_mux",
        rtl_snapshot_hash="sha256:" + "1" * 64,
        relative_path="rtl/priority_mux.sv",
        start_line=3,
        start_column=3,
        end_line=6,
        end_column=18,
        owner_hierarchy="priority_mux",
        source_text_hash=_hash("\n".join(selected)),
        mapping_confidence=1.0,
        protected=protected,
        protection_kinds=("CLOCK",) if protected else (),
    )


def _ast(path: str = "rtl/priority_mux.sv") -> dict[str, object]:
    return {
        "design": {
            "kind": "Root",
            "addr": 123456,
            "members": [
                {
                    "kind": "ConditionalStatement",
                    "source_file_start": path,
                    "source_file_end": path,
                    "source_line_start": 3,
                    "source_line_end": 6,
                    "source_column_start": 3,
                    "source_column_end": 18,
                    "addr": 987654,
                    "symbol": "987654 grant",
                    "loopVariable": "987654 clock_index",
                }
            ],
        }
    }


def test_slang_ast_normalization_is_address_independent_and_source_located() -> None:
    first = parse_slang_ast(_ast())
    changed_addresses = _ast()
    changed_addresses["design"]["addr"] = 9  # type: ignore[index]
    changed_addresses["design"]["members"][0]["addr"] = 10  # type: ignore[index]
    changed_addresses["design"]["members"][0]["symbol"] = "10 grant"  # type: ignore[index]
    changed_addresses["design"]["members"][0]["loopVariable"] = (  # type: ignore[index]
        "10 clock_index"
    )
    second = parse_slang_ast(changed_addresses)

    assert first.ast_hash == second.ast_hash
    assert first.nodes[0].kind == "ConditionalStatement"
    assert first.nodes[0].relative_path == "rtl/priority_mux.sv"
    assert first.nodes[0].start_line == 3
    assert first.nodes[0].end_column == 18


def test_slang_conditional_kind_is_normalized_to_registry_vocabulary() -> None:
    payload = _ast()
    payload["design"]["members"][0]["kind"] = "Conditional"  # type: ignore[index]

    parsed = parse_slang_ast(payload)

    assert parsed.nodes[0].kind == "ConditionalStatement"


def test_authorized_edit_changes_only_exact_slang_node(tmp_path: Path) -> None:
    source = """module priority_mux;
  always_comb begin
  if (req[0]) grant = 0;
  else if (req[1]) grant = 1;
  else if (req[2]) grant = 2;
  else grant = 3;
  end
endmodule
"""
    source_path = tmp_path / "rtl" / "priority_mux.sv"
    source_path.parent.mkdir()
    source_path.write_text(source)
    parsed = parse_slang_ast(_ast())
    replacement = """if (req[0]) grant = 0;
  else if (req[1]) grant = 1;
  else if (req[2]) grant = 2;
  else grant = 3;"""
    edit = SourceEdit(
        relative_path="rtl/priority_mux.sv",
        start_line=3,
        start_column=3,
        end_line=6,
        end_column=18,
        expected_text_hash=_hash(replacement),
        replacement=replacement,
        ast_node_kind="ConditionalStatement",
    )

    result = apply_authorized_edit(
        repository_root=tmp_path,
        parsed_ast=parsed,
        authorized_span=_span(source),
        protected_spans=(),
        edit=edit,
        allowed_ast_node_kinds=("ConditionalStatement",),
    )

    assert result.relative_path == "rtl/priority_mux.sv"
    assert result.changed_span_ids == ("span_priority_mux",)
    assert result.before_hash == _hash(source)
    assert result.after_hash == _hash(source_path.read_text())
    assert result.before_hash == result.after_hash  # identical replacement is deterministic


@pytest.mark.parametrize(
    ("start_line", "end_line", "message"),
    ((2, 6, "outside authorized span"), (3, 7, "outside authorized span")),
)
def test_authorization_rejects_changes_outside_owned_span(
    tmp_path: Path, start_line: int, end_line: int, message: str
) -> None:
    source = """module priority_mux;
  always_comb begin
  if (req[0]) grant = 0;
  else if (req[1]) grant = 1;
  else if (req[2]) grant = 2;
  else grant = 3;
  end
endmodule
"""
    path = tmp_path / "rtl" / "priority_mux.sv"
    path.parent.mkdir()
    path.write_text(source)
    edit = SourceEdit(
        relative_path="rtl/priority_mux.sv",
        start_line=start_line,
        start_column=3,
        end_line=end_line,
        end_column=18,
        expected_text_hash=_hash("irrelevant"),
        replacement="grant = 0;",
        ast_node_kind="ConditionalStatement",
    )

    with pytest.raises(SyntaxBackendError, match=message):
        apply_authorized_edit(
            repository_root=tmp_path,
            parsed_ast=parse_slang_ast(_ast()),
            authorized_span=_span(source),
            protected_spans=(),
            edit=edit,
            allowed_ast_node_kinds=("ConditionalStatement",),
        )


def test_authorization_rejects_protected_or_unregistered_ast_context(
    tmp_path: Path,
) -> None:
    source = """module priority_mux;
  always_comb begin
  if (req[0]) grant = 0;
  else if (req[1]) grant = 1;
  else if (req[2]) grant = 2;
  else grant = 3;
  end
endmodule
"""
    path = tmp_path / "rtl" / "priority_mux.sv"
    path.parent.mkdir()
    path.write_text(source)
    edit = SourceEdit(
        relative_path="rtl/priority_mux.sv",
        start_line=3,
        start_column=3,
        end_line=6,
        end_column=18,
        expected_text_hash=_hash("\n".join(source.splitlines()[2:6])),
        replacement="grant = 0;",
        ast_node_kind="ConditionalStatement",
    )

    with pytest.raises(SyntaxBackendError, match="protected"):
        apply_authorized_edit(
            repository_root=tmp_path,
            parsed_ast=parse_slang_ast(_ast()),
            authorized_span=_span(source),
            protected_spans=(_span(source, protected=True),),
            edit=edit,
            allowed_ast_node_kinds=("ConditionalStatement",),
        )

    with pytest.raises(SyntaxBackendError, match="not registered"):
        apply_authorized_edit(
            repository_root=tmp_path,
            parsed_ast=parse_slang_ast(_ast()),
            authorized_span=_span(source),
            protected_spans=(),
            edit=edit,
            allowed_ast_node_kinds=("CaseStatement",),
        )


def test_ast_parser_rejects_cross_file_or_malformed_ranges() -> None:
    cross_file = _ast()
    node = cross_file["design"]["members"][0]  # type: ignore[index]
    node["source_file_end"] = "rtl/other.sv"  # type: ignore[index]
    with pytest.raises(SyntaxBackendError, match="cross-file"):
        parse_slang_ast(cross_file)

    with pytest.raises(SyntaxBackendError, match="JSON object"):
        parse_slang_ast(json.loads("[]"))
