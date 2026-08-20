from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.contracts.base import canonical_json_bytes
from nova_rtl.contracts.platform import (
    DoctorCheck,
    DoctorReport,
    InstalledComponentReceipt,
    M0SignoffReport,
    OrganizerDecisions,
    PlatformAnalysisViews,
    PlatformSmokeReport,
    SignoffEvidenceFile,
    SmokeTimingView,
    ToolchainReceipt,
)
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import (
    hash_file,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)
from nova_rtl.platform.signoff import (
    M0_REQUIRED_TOOLS,
    SignoffError,
    _signoff_invocation_hash,
    capture_git_checkpoint,
    load_analysis_views,
    load_m0_defaults,
    load_organizer_decisions,
    publish_signoff_packet,
    validate_analysis_views,
    verify_m0_signoff_packet,
)
from nova_rtl.platform.smoke import _execution_environment_identity_hash

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORGANIZER_DECISIONS = PROJECT_ROOT / "config/challenge/organizer_decisions.yaml"
LOCK_PATH = PROJECT_ROOT / "config/platform/platform.lock.yaml"
VIEWS_PATH = PROJECT_ROOT / "config/analysis/views.yaml"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config/policy/default.yaml"
CDC_PATTERNS_PATH = PROJECT_ROOT / "config/policy/cdc_patterns.yaml"
RESET_ASSUMPTIONS_PATH = PROJECT_ROOT / "config/formal/reset_assumptions.yaml"
MANIFEST_PATH = PROJECT_ROOT / "config/platform/toolchain-sources.json"
SELECTION_POLICY_PATH = PROJECT_ROOT / "config/platform/platform-selection-policy.yaml"
runner = CliRunner()


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def evidence(relative_path: str, digit: str = "1") -> SignoffEvidenceFile:
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_ref(digit),
        size_bytes=1,
    )


def file_evidence(path: Path, relative_path: str) -> SignoffEvidenceFile:
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=hash_file(path),
        size_bytes=path.stat().st_size,
    )


def write_contract(path: Path, contract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(contract) + b"\n")


def build_synthetic_signoff_packet(tmp_path: Path) -> Path:
    source = tmp_path / "packet-source"
    smoke_root = source / "smoke"
    smoke_root.mkdir(parents=True)
    committed_inputs = {
        "platform.lock.yaml": LOCK_PATH,
        "platform-selection-policy.yaml": SELECTION_POLICY_PATH,
        "analysis-views.yaml": VIEWS_PATH,
        "organizer-decisions.yaml": ORGANIZER_DECISIONS,
        "default-policy.yaml": DEFAULT_POLICY_PATH,
        "cdc-patterns.yaml": CDC_PATTERNS_PATH,
        "reset-assumptions.yaml": RESET_ASSUMPTIONS_PATH,
    }
    for relative_path, original in committed_inputs.items():
        shutil.copyfile(original, source / relative_path)

    lock = load_platform_lock(source / "platform.lock.yaml")
    loaded_manifest = load_toolchain_source_manifest(MANIFEST_PATH)
    receipt = ToolchainReceipt(
        schema_version=1,
        manifest_hash=lock.source_manifest_hash,
        manifest=loaded_manifest.manifest,
        components=tuple(
            InstalledComponentReceipt(
                component_id=item.component_id,
                source=item,
                inventory=(),
                tree_identity=hash_ref("a"),
            )
            for item in loaded_manifest.manifest.components
        ),
        environment={},
        tool_fingerprints=lock.tool_fingerprints,
    )
    receipt_path = source / "toolchain-receipt.json"
    write_contract(receipt_path, receipt)

    fingerprints = {item.tool_id: item for item in receipt.tool_fingerprints}
    doctor = DoctorReport(
        status="PASS",
        checks=tuple(
            DoctorCheck(
                name=tool_id,
                status="PASS",
                resolved_path=fingerprints[tool_id].executable,
                tool_fingerprint=fingerprints[tool_id],
                artifact_hash=None,
                issues=(),
                message="synthetic passing probe",
            )
            for tool_id in M0_REQUIRED_TOOLS
        )
        + (
            DoctorCheck(
                name="platform_lock",
                status="PASS",
                resolved_path=str(LOCK_PATH.resolve()),
                tool_fingerprint=None,
                artifact_hash=hash_file(source / "platform.lock.yaml"),
                issues=(),
                message="synthetic passing lock check",
            ),
        ),
        platform_lock_hash=hash_file(source / "platform.lock.yaml"),
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        exit_code=0,
    )
    doctor_path = source / "doctor-report.json"
    write_contract(doctor_path, doctor)

    setup_text = """\
NOVA_SMOKE_VIEW asap7_wc
NOVA_SMOKE_CHECK SETUP
NOVA_SMOKE_REGISTER_COUNT 2
NOVA_SMOKE_PATH_COUNT 1
Startpoint: first_stage$_SDFF_PN0_
Endpoint: data_out$_SDFF_PN0_
Path Group: smoke_clock
Path Type: max
                         858.82   slack (MET)
"""
    hold_text = setup_text.replace("asap7_wc", "asap7_bc").replace(
        "SETUP", "HOLD"
    ).replace("max", "min").replace("858.82", "42.57")
    (smoke_root / "setup.rpt").write_text(setup_text, encoding="utf-8")
    (smoke_root / "hold.rpt").write_text(hold_text, encoding="utf-8")
    for relative_path in (
        "two_flop_smoke.sv",
        "two_flop_smoke.sdc",
        "config.mk",
        "openroad-cts.log",
        "mapped-netlist.v",
        "1_synth.odb",
        "4_cts.odb",
    ):
        (smoke_root / relative_path).write_bytes(f"synthetic {relative_path}\n".encode())
    environment_hash = _execution_environment_identity_hash(
        receipt.model_dump(mode="json")["environment"],
        make_sha256=hash_ref("1"),
        make_version="GNU Make 4.4",
        python_sha256=hash_ref("2"),
        python_version="3.11.0",
    )
    smoke = PlatformSmokeReport(
        status="PASS",
        platform_id=lock.platform_id,
        platform_lock_hash=hash_file(source / "platform.lock.yaml"),
        toolchain_receipt_hash=hash_file(receipt_path),
        source_manifest_hash=receipt.manifest_hash,
        selection_policy_hash=selection_policy_content_identity_hash(
            load_platform_selection_policy(source / "platform-selection-policy.yaml")
        ),
        execution_environment_hash=environment_hash,
        make_executable="/usr/bin/make",
        make_executable_sha256=hash_ref("1"),
        make_version="GNU Make 4.4",
        python_executable="/usr/bin/python3",
        python_executable_sha256=hash_ref("2"),
        python_version="3.11.0",
        rtl=file_evidence(smoke_root / "two_flop_smoke.sv", "two_flop_smoke.sv"),
        constraints=file_evidence(smoke_root / "two_flop_smoke.sdc", "two_flop_smoke.sdc"),
        smoke_config=file_evidence(smoke_root / "config.mk", "config.mk"),
        openroad_log=file_evidence(smoke_root / "openroad-cts.log", "openroad-cts.log"),
        mapped_netlist=file_evidence(smoke_root / "mapped-netlist.v", "mapped-netlist.v"),
        synthesis_database=file_evidence(smoke_root / "1_synth.odb", "1_synth.odb"),
        cts_database=file_evidence(smoke_root / "4_cts.odb", "4_cts.odb"),
        setup_view=SmokeTimingView(
            check="SETUP",
            corner_id="asap7_wc",
            path_type="max",
            path_group="smoke_clock",
            register_count=2,
            path_count=1,
            slack_ps=858.82,
            report=file_evidence(smoke_root / "setup.rpt", "setup.rpt"),
        ),
        hold_view=SmokeTimingView(
            check="HOLD",
            corner_id="asap7_bc",
            path_type="min",
            path_group="smoke_clock",
            register_count=2,
            path_count=1,
            slack_ps=42.57,
            report=file_evidence(smoke_root / "hold.rpt", "hold.rpt"),
        ),
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    smoke_report_path = smoke_root / "smoke-report.json"
    write_contract(smoke_report_path, smoke)
    top_evidence = {
        relative_path: file_evidence(source / relative_path, relative_path)
        for relative_path in (
            "doctor-report.json",
            "toolchain-receipt.json",
            *committed_inputs,
            "smoke/smoke-report.json",
        )
    }
    implementation_commit = "b" * 40
    implementation_tree_hash = "c" * 40
    report = M0SignoffReport(
        milestone="M0",
        status="PASS",
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        exit_code=0,
        implementation_commit=implementation_commit,
        implementation_tree_hash=implementation_tree_hash,
        signoff_invocation_hash=_signoff_invocation_hash(
            implementation_commit=implementation_commit,
            implementation_tree_hash=implementation_tree_hash,
            source_manifest_hash=receipt.manifest_hash,
            selection_policy_hash=smoke.selection_policy_hash,
            platform_lock_hash=top_evidence["platform.lock.yaml"].sha256,
            analysis_views_hash=top_evidence["analysis-views.yaml"].sha256,
            organizer_decisions_hash=top_evidence["organizer-decisions.yaml"].sha256,
            default_policy_hash=top_evidence["default-policy.yaml"].sha256,
            cdc_patterns_hash=top_evidence["cdc-patterns.yaml"].sha256,
            reset_assumptions_hash=top_evidence["reset-assumptions.yaml"].sha256,
            rtl_hash=smoke.rtl.sha256,
            constraints_hash=smoke.constraints.sha256,
        ),
        execution_environment_hash=environment_hash,
        doctor_report=top_evidence["doctor-report.json"],
        toolchain_receipt=top_evidence["toolchain-receipt.json"],
        platform_lock=top_evidence["platform.lock.yaml"],
        selection_policy=top_evidence["platform-selection-policy.yaml"],
        analysis_views=top_evidence["analysis-views.yaml"],
        organizer_decisions=top_evidence["organizer-decisions.yaml"],
        default_policy=top_evidence["default-policy.yaml"],
        cdc_patterns=top_evidence["cdc-patterns.yaml"],
        reset_assumptions=top_evidence["reset-assumptions.yaml"],
        smoke_report=top_evidence["smoke/smoke-report.json"],
    )
    source_files = {
        relative_path: source / relative_path for relative_path in top_evidence
    }
    source_files.update(
        {
            f"smoke/{item.relative_path}": smoke_root / item.relative_path
            for item in (
                smoke.rtl,
                smoke.constraints,
                smoke.smoke_config,
                smoke.openroad_log,
                smoke.mapped_netlist,
                smoke.synthesis_database,
                smoke.cts_database,
                smoke.setup_view.report,
                smoke.hold_view.report,
            )
        }
    )
    destination = tmp_path / "published-packet"
    publish_signoff_packet(destination=destination, report=report, source_files=source_files)
    return destination


def resign_packet_indexes(packet: Path) -> None:
    report_payload = json.loads((packet / "m0-signoff.json").read_text(encoding="utf-8"))
    evidence_fields = {
        "doctor_report": "doctor-report.json",
        "toolchain_receipt": "toolchain-receipt.json",
        "platform_lock": "platform.lock.yaml",
        "selection_policy": "platform-selection-policy.yaml",
        "analysis_views": "analysis-views.yaml",
        "organizer_decisions": "organizer-decisions.yaml",
        "default_policy": "default-policy.yaml",
        "cdc_patterns": "cdc-patterns.yaml",
        "reset_assumptions": "reset-assumptions.yaml",
        "smoke_report": "smoke/smoke-report.json",
    }
    for field, relative_path in evidence_fields.items():
        report_payload[field] = file_evidence(packet / relative_path, relative_path).model_dump(
            mode="json"
        )
    smoke = PlatformSmokeReport.model_validate_json(
        (packet / "smoke/smoke-report.json").read_text(encoding="utf-8")
    )
    receipt = ToolchainReceipt.model_validate_json(
        (packet / "toolchain-receipt.json").read_text(encoding="utf-8")
    )
    report_payload["execution_environment_hash"] = smoke.execution_environment_hash
    report_payload["signoff_invocation_hash"] = _signoff_invocation_hash(
        implementation_commit=report_payload["implementation_commit"],
        implementation_tree_hash=report_payload["implementation_tree_hash"],
        source_manifest_hash=receipt.manifest_hash,
        selection_policy_hash=smoke.selection_policy_hash,
        platform_lock_hash=report_payload["platform_lock"]["sha256"],
        analysis_views_hash=report_payload["analysis_views"]["sha256"],
        organizer_decisions_hash=report_payload["organizer_decisions"]["sha256"],
        default_policy_hash=report_payload["default_policy"]["sha256"],
        cdc_patterns_hash=report_payload["cdc_patterns"]["sha256"],
        reset_assumptions_hash=report_payload["reset_assumptions"]["sha256"],
        rtl_hash=smoke.rtl.sha256,
        constraints_hash=smoke.constraints.sha256,
    )
    write_contract(packet / "m0-signoff.json", M0SignoffReport.model_validate(report_payload))


def valid_report() -> M0SignoffReport:
    return M0SignoffReport(
        status="PASS",
        milestone="M0",
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        exit_code=0,
        implementation_commit="b" * 40,
        implementation_tree_hash="c" * 40,
        signoff_invocation_hash=hash_ref("d"),
        execution_environment_hash=hash_ref("e"),
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


def test_analysis_view_loader_normalizes_malformed_yaml_to_signoff_error(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "views.yaml"
    malformed.write_text("views: [unterminated\n", encoding="utf-8")

    with pytest.raises(SignoffError, match="analysis views are invalid"):
        load_analysis_views(malformed)


def test_cli_exposes_m0_signoff_command() -> None:
    result = runner.invoke(app, ["m0", "signoff", "--help"])

    assert result.exit_code == 0, result.output
    assert "--organizer-decisions" in result.output
    assert "--output" in result.output


def test_git_checkpoint_requires_a_clean_committed_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("sealed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=NOVA Test",
            "-c",
            "user.email=nova@example.test",
            "commit",
            "--quiet",
            "-m",
            "sealed checkpoint",
        ],
        check=True,
    )

    checkpoint = capture_git_checkpoint(tmp_path)
    assert len(checkpoint.commit) == 40
    assert len(checkpoint.tree_hash) == 40

    tracked.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(SignoffError, match="working tree is not clean"):
        capture_git_checkpoint(tmp_path)


def test_signoff_invocation_identity_changes_with_any_consumed_input() -> None:
    inputs = {
        "implementation_commit": "1" * 40,
        "implementation_tree_hash": "2" * 40,
        "source_manifest_hash": hash_ref("3"),
        "selection_policy_hash": hash_ref("4"),
        "platform_lock_hash": hash_ref("5"),
        "analysis_views_hash": hash_ref("6"),
        "organizer_decisions_hash": hash_ref("7"),
        "default_policy_hash": hash_ref("8"),
        "cdc_patterns_hash": hash_ref("9"),
        "reset_assumptions_hash": hash_ref("a"),
        "rtl_hash": hash_ref("b"),
        "constraints_hash": hash_ref("c"),
    }

    original = _signoff_invocation_hash(**inputs)
    changed = _signoff_invocation_hash(
        **{**inputs, "constraints_hash": hash_ref("d")}
    )

    assert original != changed


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


def test_packet_verifier_accepts_a_complete_self_contained_packet(tmp_path: Path) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)

    report = verify_m0_signoff_packet(packet)

    assert report.status == "PASS"


def test_packet_verifier_rejects_symlinked_evidence_even_when_bytes_match(
    tmp_path: Path,
) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)
    evidence_path = packet / "default-policy.yaml"
    outside_copy = tmp_path / "outside-default-policy.yaml"
    shutil.copyfile(evidence_path, outside_copy)
    evidence_path.unlink()
    evidence_path.symlink_to(outside_copy)

    with pytest.raises(SignoffError, match="symlink"):
        verify_m0_signoff_packet(packet)


def test_packet_verifier_rejects_an_unindexed_extra_file(tmp_path: Path) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)
    (packet / "unindexed.txt").write_text("not evidence\n", encoding="utf-8")

    with pytest.raises(SignoffError, match="unexpected packet entries"):
        verify_m0_signoff_packet(packet)


def test_packet_verifier_requires_exact_m0_doctor_check_set(tmp_path: Path) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)
    doctor_path = packet / "doctor-report.json"
    payload = json.loads(doctor_path.read_text(encoding="utf-8"))
    payload["checks"] = [item for item in payload["checks"] if item["name"] != "slang"]
    write_contract(doctor_path, DoctorReport.model_validate(payload))
    resign_packet_indexes(packet)

    with pytest.raises(SignoffError, match="exact M0 check set"):
        verify_m0_signoff_packet(packet)


def test_packet_verifier_rejects_resigned_manifest_provenance_mismatch(
    tmp_path: Path,
) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)
    receipt_path = packet / "toolchain-receipt.json"
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_payload["manifest_hash"] = hash_ref("f")
    write_contract(receipt_path, ToolchainReceipt.model_validate(receipt_payload))
    smoke_path = packet / "smoke/smoke-report.json"
    smoke_payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    smoke_payload["toolchain_receipt_hash"] = hash_file(receipt_path)
    smoke_payload["source_manifest_hash"] = hash_ref("f")
    write_contract(smoke_path, PlatformSmokeReport.model_validate(smoke_payload))
    resign_packet_indexes(packet)

    with pytest.raises(SignoffError, match="source manifest"):
        verify_m0_signoff_packet(packet)


def test_packet_verifier_reparses_resigned_raw_timing_report(tmp_path: Path) -> None:
    packet = build_synthetic_signoff_packet(tmp_path)
    setup_path = packet / "smoke/setup.rpt"
    setup_path.write_text(
        setup_path.read_text(encoding="utf-8").replace("858.82", "999.99"),
        encoding="utf-8",
    )
    smoke_path = packet / "smoke/smoke-report.json"
    smoke_payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    smoke_payload["setup_view"]["report"] = file_evidence(
        setup_path, "setup.rpt"
    ).model_dump(mode="json")
    write_contract(smoke_path, PlatformSmokeReport.model_validate(smoke_payload))
    resign_packet_indexes(packet)

    with pytest.raises(SignoffError, match="timing summary disagrees"):
        verify_m0_signoff_packet(packet)
