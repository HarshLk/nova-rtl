from __future__ import annotations

from hashlib import sha256

import pytest

from nova_rtl.transforms.priority_mux import (
    PriorityMuxContext,
    PriorityMuxError,
    PriorityMuxParameters,
    RestructurePriorityMux,
)

SOURCE = """if (req[0]) grant = $unsigned(value[0]);
else if (req[1]) grant = $unsigned(value[1]);
else if (req[2]) grant = $unsigned(value[2]);
else grant = $unsigned(fallback);"""


def _hash(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _context(source: str = SOURCE, **overrides: object) -> PriorityMuxContext:
    values: dict[str, object] = {
        "relative_path": "rtl/priority_mux.sv",
        "source_text": source,
        "start_line": 10,
        "start_column": 5,
        "end_line": 13,
        "end_column": 42,
        "owner_hierarchy": "top.u_mux",
        "expected_owner_hierarchy": "top.u_mux",
        "clock_domain_ids": ("clk_core",),
        "protected_neighbor_ids": (),
    }
    values.update(overrides)
    return PriorityMuxContext(**values)


def test_priority_mux_rewrite_is_deterministic_and_preserves_expressions() -> None:
    capability = RestructurePriorityMux()
    parameters = PriorityMuxParameters(max_branches=8)
    match = capability.match(_context())

    first = capability.rewrite(match, parameters)
    second = capability.rewrite(match, parameters)

    assert first == second
    assert first.expected_text_hash == _hash(SOURCE)
    assert "logic [2:0] nova_priority_cond;" in first.replacement
    assert "nova_priority_cond[0] = ((req[0]) === 1'b1);" in first.replacement
    assert "~(|nova_priority_cond[1:0])" in first.replacement
    assert "grant = $unsigned(value[2]);" in first.replacement
    assert "default: grant = $unsigned(fallback);" in first.replacement
    assert capability.fingerprint(match, parameters).startswith("priority_mux:v1:")


def test_balanced_predecode_preserves_four_state_priority_choice() -> None:
    capability = RestructurePriorityMux()
    match = capability.match(_context())

    for conditions in (
        ("0", "0", "0"),
        ("1", "1", "1"),
        ("x", "1", "1"),
        ("z", "x", "1"),
        ("0", "x", "0"),
    ):
        original = next(
            (index for index, condition in enumerate(conditions) if condition == "1"),
            len(conditions),
        )
        decoded = capability.selected_branch(match, conditions)
        assert decoded == original


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "if (a) y = 1; else if (b) y = 2;",
            "complete default assignment",
        ),
        (
            "if (a) y = 1; else if (b) z = 2; else y = 3;",
            "same assignment target",
        ),
        (
            "if (a) y = side_effect(); else if (b) y = 2; else y = 3;",
            "side-effect-free",
        ),
        (
            "if (a === 1'bx) y = 1; else if (b) y = 2; else y = 3;",
            "ambiguous X/Z",
        ),
        (
            "if (a) y++; else if (b) y = 2; else y = 3;",
            "simple blocking assignments",
        ),
    ),
)
def test_priority_mux_rejects_unsafe_source_patterns(source: str, message: str) -> None:
    with pytest.raises(PriorityMuxError, match=message):
        RestructurePriorityMux().match(_context(source))


def test_priority_mux_rejects_scope_protection_and_branch_boundaries() -> None:
    capability = RestructurePriorityMux()

    with pytest.raises(PriorityMuxError, match="proposal owner"):
        capability.match(_context(expected_owner_hierarchy="top.other"))
    with pytest.raises(PriorityMuxError, match="protected neighbor"):
        capability.match(_context(protected_neighbor_ids=("cdc_sync",)))
    with pytest.raises(PriorityMuxError, match="one clock domain"):
        capability.match(_context(clock_domain_ids=("clk_a", "clk_b")))

    match = capability.match(_context())
    with pytest.raises(PriorityMuxError, match="configured bound"):
        capability.rewrite(match, PriorityMuxParameters(max_branches=2))


def test_priority_mux_parameter_model_is_strict() -> None:
    with pytest.raises(ValueError):
        PriorityMuxParameters(max_branches=8, unknown=True)
