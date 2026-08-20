from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.contracts.platform import (
    M0SignoffReport,
    OrganizerDecisions,
    PlatformAnalysisViews,
    SignoffEvidenceFile,
)
from nova_rtl.platform.lock import load_platform_lock
from nova_rtl.platform.signoff import (
    SignoffError,
    load_m0_defaults,
    load_organizer_decisions,
    publish_signoff_packet,
    validate_analysis_views,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORGANIZER_DECISIONS = PROJECT_ROOT / "config/challenge/organizer_decisions.yaml"
LOCK_PATH = PROJECT_ROOT / "config/platform/platform.lock.yaml"
VIEWS_PATH = PROJECT_ROOT / "config/analysis/views.yaml"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config/policy/default.yaml"
CDC_PATTERNS_PATH = PROJECT_ROOT / "config/policy/cdc_patterns.yaml"
RESET_ASSUMPTIONS_PATH = PROJECT_ROOT / "config/formal/reset_assumptions.yaml"
runner = CliRunner()


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def evidence(relative_path: str, digit: str = "1") -> SignoffEvidenceFile:
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_ref(digit),
        size_bytes=1,
    )


def valid_report() -> M0SignoffReport:
    return M0SignoffReport(
        status="PASS",
        milestone="M0",
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        exit_code=0,
        doctor_report=evidence("doctor-report.json", "1"),
        toolchain_receipt=evidence("toolchain-receipt.json", "2"),
        platform_lock=evidence("platform.lock.yaml", "3"),
        selection_policy=evidence("platform-selection-policy.yaml", "4"),
        analysis_views=evidence("analysis-views.yaml", "5"),
        organizer_decisions=evidence("organizer-decisions.yaml", "6"),
        default_policy=evidence("default-policy.yaml", "7"),
        cdc_patterns=evidence("cdc-patterns.yaml", "8"),
        reset_assumptions=evidence("reset-assumptions.yaml", "9"),
        smoke_report=evidence("smoke/smoke-report.json", "a"),
    )


def test_organizer_decisions_reject_inconsistent_generated_clock_total() -> None:
    with pytest.raises(ValidationError, match="masters multiplied"):
        OrganizerDecisions.model_validate(
            {
                "schema_version": 1,
                "generated_clock_interpretation": {
                    "effective_value": "21_PER_MASTER",
                    "masters": 5,
                    "total_generated_clocks": 104,
                    "source": "CONSERVATIVE_INTERNAL_DEFAULT",
                },
                "psm_interpretation": {
                    "effective_value": "FSM_STATE_MACHINE_OPTIMIZATION",
                    "source": "CONSERVATIVE_INTERNAL_DEFAULT",
                },
                "organizer_response": {
                    "status": "NOT_RECEIVED",
                    "evidence_artifact_id": None,
                },
                "override_rule": "CONFIG_ONLY_WITH_NEW_RUN_ID",
            }
        )


def test_production_organizer_decisions_are_strict_and_versioned() -> None:
    decisions = load_organizer_decisions(ORGANIZER_DECISIONS)

    assert decisions.generated_clock_interpretation.masters == 5
    assert decisions.generated_clock_interpretation.total_generated_clocks == 105
    assert decisions.organizer_response.status == "NOT_RECEIVED"
    assert decisions.override_rule == "CONFIG_ONLY_WITH_NEW_RUN_ID"


def test_production_m0_defaults_are_strict_and_conservative() -> None:
    defaults = load_m0_defaults(
        default_policy=DEFAULT_POLICY_PATH,
        cdc_patterns=CDC_PATTERNS_PATH,
        reset_assumptions=RESET_ASSUMPTIONS_PATH,
    )

    assert defaults.policy.primary_correctness == "STRICT_SEQ_EQUIV"
    assert defaults.policy.allow_sdc_edits is False
    assert defaults.policy.allow_latency_change is False
    assert defaults.cdc.claim_scope == "STRUCTURAL_CDC_INVARIANT_AUDIT"
    assert {pattern.kind for pattern in defaults.cdc.patterns} == {
        "TWO_FLOP_LEVEL",
        "TOGGLE_PULSE",
        "REQUEST_ACKNOWLEDGE",
        "ASYNC_FIFO_GRAY",
        "GRAY_POINTER",
        "RESET_SYNCHRONIZER",
    }
    assert defaults.reset.master_clock_model == "INDEPENDENT_SHARED_GOLD_GATE_EVENTS"
    assert defaults.reset.generated_clock_model == "DERIVED_FROM_PROTECTED_DIVIDER_STATE"


def test_cli_exposes_m0_signoff_command() -> None:
    result = runner.invoke(app, ["m0", "signoff", "--help"])

    assert result.exit_code == 0, result.output
    assert "--organizer-decisions" in result.output
    assert "--output" in result.output


def test_signoff_rejects_views_whose_liberty_hashes_disagree_with_lock() -> None:
    lock = load_platform_lock(LOCK_PATH)
    views = PlatformAnalysisViews.model_validate(
        yaml.safe_load(VIEWS_PATH.read_text(encoding="utf-8"))
    )
    changed_setup = views.views[0].model_copy(
        update={"liberty_artifact_hashes": ("sha256:" + "0" * 64,)}
    )
    changed = views.model_copy(update={"views": (changed_setup, views.views[1])})

    with pytest.raises(SignoffError, match="setup Liberty hashes"):
        validate_analysis_views(lock, changed)


def test_signoff_report_rejects_duplicate_evidence_paths() -> None:
    report = valid_report()
    payload = report.model_dump(mode="json")
    payload["smoke_report"] = payload["doctor_report"]

    with pytest.raises(ValidationError, match="evidence paths must be unique"):
        M0SignoffReport.model_validate(payload)


def test_publish_signoff_packet_rejects_existing_destination_without_changes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "m0-signoff"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")

    with pytest.raises(SignoffError, match="already exists"):
        publish_signoff_packet(
            destination=destination,
            report=valid_report(),
            source_files={},
        )

    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert tuple(destination.iterdir()) == (sentinel,)


def test_publish_signoff_packet_rejects_missing_report_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "m0-signoff"

    with pytest.raises(SignoffError, match="source set does not match"):
        publish_signoff_packet(
            destination=destination,
            report=valid_report(),
            source_files={},
        )

    assert not destination.exists()
