from __future__ import annotations

from nova_rtl.contracts.analysis import FrequencySweepContract
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.evaluation.frequency import FrequencyObservation, evaluate_frequency_sweep


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def _contract() -> FrequencySweepContract:
    payload = {
        "schema_version": 1,
        "frequency_sweep_contract_id": "sweep_contract_control",
        "baseline_analysis_view_hash": _hash("a"),
        "target_master_clock_id": "clk_control",
        "target_domain_id": "control",
        "ordered_trial_periods_ns": (1.5, 2.0, 2.5),
        "fixed_non_target_clock_definitions_hash": _hash("b"),
        "setup_pass_limit_ns": 0.0,
        "hold_pass_limit_ns": 0.0,
        "constraint_overlay_generator_hash": _hash("c"),
        "search_method": "BOUNDED_BINARY",
        "maximum_trials": 3,
        "result_label_policy": "ACHIEVED_BY_SWEEP_ONLY_ON_SETUP_HOLD_PASS",
    }
    return FrequencySweepContract(**payload, contract_hash=canonical_sha256(payload))


def test_sweep_reports_fastest_point_passing_both_setup_and_hold() -> None:
    observations = (
        FrequencyObservation(
            period_ns=2.5,
            setup_slack_ns=0.2,
            hold_slack_ns=0.1,
            setup_stage_result_id="setup_25",
            hold_stage_result_id="hold_25",
        ),
        FrequencyObservation(
            period_ns=1.5,
            setup_slack_ns=-0.1,
            hold_slack_ns=0.1,
            setup_stage_result_id="setup_15",
            hold_stage_result_id="hold_15",
        ),
        FrequencyObservation(
            period_ns=2.0,
            setup_slack_ns=0.0,
            hold_slack_ns=0.05,
            setup_stage_result_id="setup_20",
            hold_stage_result_id="hold_20",
        ),
    )

    result = evaluate_frequency_sweep("candidate_primary", _contract(), observations)

    assert tuple(trial.period_ns for trial in result.trials) == (1.5, 2.0, 2.5)
    assert result.fmax_mhz == 500.0
    assert len(result.passing_trial_ids) == 2
    assert result.trials[0].setup_status == "FAIL"


def test_overlay_identity_changes_only_with_target_period() -> None:
    observations = tuple(
        FrequencyObservation(
            period_ns=period,
            setup_slack_ns=0.1,
            hold_slack_ns=0.1,
            setup_stage_result_id=f"setup_{index}",
            hold_stage_result_id=f"hold_{index}",
        )
        for index, period in enumerate((1.5, 2.0, 2.5))
    )

    first = evaluate_frequency_sweep("candidate_primary", _contract(), observations)
    second = evaluate_frequency_sweep("candidate_primary", _contract(), observations)

    assert first == second
    assert first.contract_hash == _contract().contract_hash
