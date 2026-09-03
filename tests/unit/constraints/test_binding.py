"""Constraint-binding safety preflight behavior."""

from __future__ import annotations

import pytest

from nova_rtl.constraints.binding import (
    ConstraintCommandResolution,
    SafetyPreflightError,
    audit_constraint_binding,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _audit(
    *,
    candidate_id: str = "baseline",
    command_objects: tuple[str, ...] = ("pin:u_b/D", "port:rst_n", "pin:u_a/D"),
    timed: tuple[str, ...] = ("pin:u_a/D",),
    excepted: tuple[str, ...] = ("pin:u_b/D",),
    view_hash: str | None = None,
    baseline=None,
):
    return audit_constraint_binding(
        binding_manifest_id=f"binding_{candidate_id}",
        candidate_id=candidate_id,
        sdc_hash=hash_ref("1"),
        netlist_snapshot_hash=hash_ref("2") if candidate_id == "baseline" else hash_ref("5"),
        analysis_view_hashes={
            "func_hold_fast": hash_ref("4"),
            "func_setup_slow": view_hash or hash_ref("3"),
        },
        expected_analysis_view_hashes={
            "func_hold_fast": hash_ref("4"),
            "func_setup_slow": hash_ref("3"),
        },
        expected_command_ids=("false_path_reset",),
        command_resolutions=(
            ConstraintCommandResolution(
                command_id="false_path_reset",
                normalized_command="set_false_path -from rst_n -to all_registers",
                selectors=("get_ports:rst_n", "all_registers"),
                resolved_object_ids=command_objects,
            ),
        ),
        sequential_endpoint_ids=("pin:u_b/D", "pin:u_a/D"),
        timed_endpoint_ids=timed,
        reviewed_exception_endpoint_ids=excepted,
        baseline=baseline,
    )


def test_auditor_emits_sorted_canonical_binding_with_complete_coverage() -> None:
    manifest = audit_constraint_binding(
        binding_manifest_id="binding_baseline",
        candidate_id="baseline",
        sdc_hash=hash_ref("1"),
        netlist_snapshot_hash=hash_ref("2"),
        analysis_view_hashes={
            "func_setup_slow": hash_ref("3"),
            "func_hold_fast": hash_ref("4"),
        },
        expected_analysis_view_hashes={
            "func_hold_fast": hash_ref("4"),
            "func_setup_slow": hash_ref("3"),
        },
        expected_command_ids=("false_path_reset",),
        command_resolutions=(
            ConstraintCommandResolution(
                command_id="false_path_reset",
                normalized_command="set_false_path -from rst_n -to all_registers",
                selectors=("get_ports:rst_n", "all_registers"),
                resolved_object_ids=("pin:u_b/D", "port:rst_n", "pin:u_a/D"),
            ),
        ),
        sequential_endpoint_ids=("pin:u_b/D", "pin:u_a/D"),
        timed_endpoint_ids=("pin:u_a/D",),
        reviewed_exception_endpoint_ids=("pin:u_b/D",),
        baseline=None,
    )

    assert manifest.analysis_view_ids == ("func_hold_fast", "func_setup_slow")
    assert manifest.resolved_commands[0].resolved_object_ids == (
        "pin:u_a/D",
        "pin:u_b/D",
        "port:rst_n",
    )
    assert manifest.coverage.sequential_endpoints_total == 2
    assert manifest.coverage.timed_endpoints == 1
    assert manifest.coverage.reviewed_exception_endpoints == 1
    assert manifest.coverage.unresolved_selectors == 0
    assert manifest.comparison_to_baseline == "EQUIVALENT"


def test_empty_sdc_wildcard_is_rejected_before_manifest_creation() -> None:
    with pytest.raises(SafetyPreflightError) as failure:
        _audit(command_objects=())

    assert failure.value.code == "CONSTRAINT_SELECTOR_UNRESOLVED"


def test_changed_selector_binding_is_rejected_against_baseline() -> None:
    baseline = _audit()

    with pytest.raises(SafetyPreflightError) as failure:
        _audit(
            candidate_id="candidate_001",
            command_objects=("pin:u_c/D", "port:rst_n"),
            baseline=baseline,
        )

    assert failure.value.code == "CONSTRAINT_BINDING_FORBIDDEN_DELTA"


def test_incomplete_endpoint_coverage_is_rejected() -> None:
    with pytest.raises(SafetyPreflightError) as failure:
        _audit(timed=(), excepted=("pin:u_b/D",))

    assert failure.value.code == "CONSTRAINT_ENDPOINT_COVERAGE_INCOMPLETE"


def test_incorrect_required_analysis_view_hash_is_rejected() -> None:
    with pytest.raises(SafetyPreflightError) as failure:
        _audit(view_hash=hash_ref("9"))

    assert failure.value.code == "ANALYSIS_VIEW_HASH_MISMATCH"


def test_missing_expected_sdc_command_is_rejected() -> None:
    with pytest.raises(SafetyPreflightError) as failure:
        audit_constraint_binding(
            binding_manifest_id="binding_baseline",
            candidate_id="baseline",
            sdc_hash=hash_ref("1"),
            netlist_snapshot_hash=hash_ref("2"),
            analysis_view_hashes={"func_setup_slow": hash_ref("3")},
            expected_analysis_view_hashes={"func_setup_slow": hash_ref("3")},
            expected_command_ids=("create_clock_ingress",),
            command_resolutions=(),
            sequential_endpoint_ids=("pin:u_a/D",),
            timed_endpoint_ids=("pin:u_a/D",),
            reviewed_exception_endpoint_ids=(),
            baseline=None,
        )

    assert failure.value.code == "CONSTRAINT_COMMAND_INVENTORY_INCOMPLETE"
