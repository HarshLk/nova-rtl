"""Deterministic setup-and-hold frequency sweep evaluation."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field

from nova_rtl.contracts.analysis import FrequencySweepContract
from nova_rtl.contracts.base import EntityId, FiniteFloat, StrictContract, canonical_sha256
from nova_rtl.contracts.release import FrequencySweepResult, FrequencySweepTrial


class FrequencyObservation(StrictContract):
    """Parsed STA evidence for one contract period, with raw result identities."""

    period_ns: FiniteFloat = Field(gt=0)
    setup_slack_ns: FiniteFloat
    hold_slack_ns: FiniteFloat
    setup_stage_result_id: EntityId
    hold_stage_result_id: EntityId


def load_frequency_sweep_contract(path: Path) -> FrequencySweepContract:
    """Load a human-authored sweep policy and bind its canonical identity."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("frequency sweep contract must be a mapping")
    payload = dict(raw)
    payload.pop("contract_hash", None)
    return FrequencySweepContract(**payload, contract_hash=canonical_sha256(payload))


def evaluate_frequency_sweep(
    candidate_id: str,
    contract: FrequencySweepContract,
    observations: tuple[FrequencyObservation, ...],
) -> FrequencySweepResult:
    """Evaluate only declared periods; a point passes only setup and hold policy."""

    by_period = {item.period_ns: item for item in observations}
    expected = tuple(contract.ordered_trial_periods_ns)
    if len(by_period) != len(observations) or set(by_period) != set(expected):
        raise ValueError("frequency observations must cover each contract period exactly once")
    trials: list[FrequencySweepTrial] = []
    passing: list[str] = []
    for period in sorted(expected):
        observation = by_period[period]
        overlay_hash = canonical_sha256(
            {
                "target_master_clock_id": contract.target_master_clock_id,
                "target_domain_id": contract.target_domain_id,
                "period_ns": period,
                "fixed_non_target_clock_definitions_hash": (
                    contract.fixed_non_target_clock_definitions_hash
                ),
                "constraint_overlay_generator_hash": (
                    contract.constraint_overlay_generator_hash
                ),
            }
        )
        trial_id = "frequency_trial_" + overlay_hash.removeprefix("sha256:")[:24]
        setup_status = (
            "PASS"
            if observation.setup_slack_ns >= contract.setup_pass_limit_ns
            else "FAIL"
        )
        hold_status = (
            "PASS"
            if observation.hold_slack_ns >= contract.hold_pass_limit_ns
            else "FAIL"
        )
        trial = FrequencySweepTrial(
            trial_id=trial_id,
            period_ns=period,
            frequency_mhz=1000.0 / period,
            setup_status=setup_status,
            hold_status=hold_status,
            setup_stage_result_id=observation.setup_stage_result_id,
            hold_stage_result_id=observation.hold_stage_result_id,
            overlay_hash=overlay_hash,
        )
        trials.append(trial)
        if setup_status == "PASS" and hold_status == "PASS":
            passing.append(trial_id)
    payload = {
        "schema_version": 1,
        "sweep_id": "frequency_sweep_"
        + canonical_sha256(
            {
                "candidate_id": candidate_id,
                "contract_hash": contract.contract_hash,
                "trials": tuple(item.model_dump(mode="json") for item in trials),
            }
        ).removeprefix("sha256:")[:24],
        "candidate_id": candidate_id,
        "contract_hash": contract.contract_hash,
        "trials": tuple(item.model_dump(mode="json") for item in trials),
        "passing_trial_ids": tuple(passing),
        "fmax_mhz": max(
            (item.frequency_mhz for item in trials if item.trial_id in passing),
            default=None,
        ),
    }
    return FrequencySweepResult(**payload, result_hash=canonical_sha256(payload))


__all__ = [
    "FrequencyObservation",
    "evaluate_frequency_sweep",
    "load_frequency_sweep_contract",
]
