from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import nova_rtl.platform.smoke as smoke_module
from nova_rtl.contracts.platform import SignoffEvidenceFile, SmokeTimingView
from nova_rtl.platform.activation import VerifiedToolchain
from nova_rtl.platform.lock import (
    PlatformLockVerification,
    load_platform_lock,
    load_platform_selection_policy,
    selection_policy_content_identity_hash,
)
from nova_rtl.platform.smoke import (
    SmokeError,
    _execution_environment_identity_hash,
    _orfs_config,
    _require_output_outside_roots,
    _run_checked,
    _smoke_environment,
    _snapshot_input,
    _sta_environment,
    _sta_script,
    _validated_smoke_lock,
    parse_timing_evidence,
    require_orfs_safe_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCK_PATH = PROJECT_ROOT / "config/platform/platform.lock.yaml"
POLICY_PATH = PROJECT_ROOT / "config/platform/platform-selection-policy.yaml"


def evidence(relative_path: str) -> SignoffEvidenceFile:
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256="sha256:" + "1" * 64,
        size_bytes=1,
    )


def test_smoke_timing_view_rejects_hold_report_labeled_as_max_path() -> None:
    with pytest.raises(ValidationError, match="HOLD smoke view requires min path type"):
        SmokeTimingView(
            check="HOLD",
            corner_id="asap7_bc",
            path_type="max",
            path_group="smoke_clock",
            register_count=2,
            path_count=1,
            slack_ps=42.57,
            report=evidence("hold.rpt"),
        )


def test_orfs_smoke_rejects_whitespace_path_with_portability_diagnostic() -> None:
    with pytest.raises(SmokeError, match="whitespace or Make metacharacters"):
        require_orfs_safe_path(Path("/tmp/project with spaces/smoke.sv"), "smoke RTL")


@pytest.mark.parametrize("metacharacter", ("|", "&", "`", "<", ">", ":", "="))
def test_orfs_smoke_rejects_every_unquoted_make_recipe_metacharacter(
    metacharacter: str,
) -> None:
    with pytest.raises(SmokeError, match="portable path allowlist"):
        require_orfs_safe_path(
            Path(f"/tmp/project{metacharacter}injected/smoke.sv"),
            "smoke RTL",
        )


def test_smoke_input_snapshot_is_immutable_after_source_edit(tmp_path: Path) -> None:
    source = tmp_path / "source.sv"
    source.write_text("module before; endmodule\n", encoding="utf-8")

    snapshot = _snapshot_input(source, tmp_path / "scratch/input.sv", "smoke RTL")
    source.write_text("module after; endmodule\n", encoding="utf-8")

    assert snapshot.read_text(encoding="utf-8") == "module before; endmodule\n"


def test_smoke_input_snapshot_rejects_source_mutation_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sdc"
    source.write_text("create_clock -period 1 clk\n", encoding="utf-8")
    real_copyfile = smoke_module.shutil.copyfile

    def copy_then_mutate(source_path: Path, destination_path: Path) -> Path:
        copied = real_copyfile(source_path, destination_path)
        Path(source_path).write_text("create_clock -period 2 clk\n", encoding="utf-8")
        return copied

    monkeypatch.setattr(smoke_module.shutil, "copyfile", copy_then_mutate)

    with pytest.raises(SmokeError, match="changed while snapshotting"):
        _snapshot_input(source, tmp_path / "scratch/input.sdc", "smoke constraints")


def test_smoke_output_may_not_modify_a_verified_tool_root(tmp_path: Path) -> None:
    tool_root = tmp_path / ".nova-tools"
    tool_root.mkdir()

    with pytest.raises(SmokeError, match="inside verified tool root"):
        _require_output_outside_roots(tool_root / "components/orfs/evidence", (tool_root,))


def test_smoke_output_confinement_resolves_symlinked_parent(tmp_path: Path) -> None:
    tool_root = tmp_path / ".nova-tools"
    tool_root.mkdir()
    alias = tmp_path / "tool-alias"
    alias.symlink_to(tool_root, target_is_directory=True)

    with pytest.raises(SmokeError, match="inside verified tool root"):
        _require_output_outside_roots(alias / "evidence", (tool_root,))


def test_execution_environment_identity_is_order_independent_and_tool_bound() -> None:
    first = _execution_environment_identity_hash(
        {"PATH": {"operation": "PREPEND_PATH", "paths": ["/tools/bin"]}, "TZ": "UTC"},
        make_sha256="sha256:" + "1" * 64,
        make_version="GNU Make 4.4",
        python_sha256="sha256:" + "2" * 64,
        python_version="3.11.0",
    )
    reordered = _execution_environment_identity_hash(
        {"TZ": "UTC", "PATH": {"paths": ["/tools/bin"], "operation": "PREPEND_PATH"}},
        make_sha256="sha256:" + "1" * 64,
        make_version="GNU Make 4.4",
        python_sha256="sha256:" + "2" * 64,
        python_version="3.11.0",
    )
    changed_python = _execution_environment_identity_hash(
        {"PATH": {"operation": "PREPEND_PATH", "paths": ["/tools/bin"]}, "TZ": "UTC"},
        make_sha256="sha256:" + "1" * 64,
        make_version="GNU Make 4.4",
        python_sha256="sha256:" + "3" * 64,
        python_version="3.11.0",
    )

    assert first == reordered
    assert first != changed_python


@pytest.mark.parametrize(
    ("check", "path_type", "slack_ps"),
    (("SETUP", "max", 858.82), ("HOLD", "min", 42.57)),
)
def test_timing_report_parser_requires_the_two_flop_smoke_path(
    check: str,
    path_type: str,
    slack_ps: float,
) -> None:
    report = f"""\
NOVA_SMOKE_VIEW asap7_{'wc' if check == 'SETUP' else 'bc'}
NOVA_SMOKE_CHECK {check}
NOVA_SMOKE_REGISTER_COUNT 2
NOVA_SMOKE_PATH_COUNT 1
Startpoint: first_stage$_SDFF_PN0_
Endpoint: data_out$_SDFF_PN0_
Path Group: smoke_clock
Path Type: {path_type}
                         {slack_ps:.2f}   slack (MET)
"""

    parsed = parse_timing_evidence(report, check)

    assert parsed.path_type == path_type
    assert parsed.path_group == "smoke_clock"
    assert parsed.register_count == 2
    assert parsed.path_count == 1
    assert parsed.slack_ps == slack_ps


def test_timing_report_parser_rejects_unconstrained_or_violated_path() -> None:
    with pytest.raises(SmokeError, match="required HOLD timing evidence"):
        parse_timing_evidence("slack (VIOLATED)\n", "HOLD")


def passing_verification(lock) -> PlatformLockVerification:
    return PlatformLockVerification(status="PASS", issues=(), lock_hash=None, lock=lock)


def test_smoke_preflight_rejects_lock_from_another_manifest() -> None:
    lock = load_platform_lock(LOCK_PATH)
    changed = lock.model_copy(update={"source_manifest_hash": "sha256:" + "0" * 64})

    with pytest.raises(SmokeError, match="source manifest"):
        _validated_smoke_lock(
            passing_verification(changed),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=lock.selection_policy_hash,
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_preflight_rejects_lock_from_another_selection_policy() -> None:
    lock = load_platform_lock(LOCK_PATH)
    changed = lock.model_copy(update={"selection_policy_hash": "sha256:" + "0" * 64})

    with pytest.raises(SmokeError, match="selection policy"):
        _validated_smoke_lock(
            passing_verification(changed),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=lock.selection_policy_hash,
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_preflight_requires_yosys_opensta_and_openroad_fingerprints() -> None:
    lock = load_platform_lock(LOCK_PATH)
    missing_openroad = lock.model_copy(
        update={
            "tool_fingerprints": tuple(
                item for item in lock.tool_fingerprints if item.tool_id != "openroad"
            )
        }
    )
    policy = load_platform_selection_policy(POLICY_PATH)

    with pytest.raises(SmokeError, match="required tool fingerprint: openroad"):
        _validated_smoke_lock(
            passing_verification(missing_openroad),
            expected_manifest_hash=lock.source_manifest_hash,
            expected_policy_hash=selection_policy_content_identity_hash(policy),
            verified_fingerprints={item.tool_id: item for item in lock.tool_fingerprints},
        )


def test_smoke_environment_does_not_inherit_make_or_orfs_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAKEFLAGS", "-e")
    monkeypatch.setenv("CORNER", "WC")
    monkeypatch.setenv("LIB_FILES", "/unreviewed.lib")
    monkeypatch.setenv("TMPDIR", "/unreviewed/tmp")
    verified = VerifiedToolchain(
        root=tmp_path,
        receipt_path=tmp_path / "receipt.json",
        component_tree_identities={},
        tool_paths={},
        canonical_environment={
            "PATH": ("/verified/tools/bin",),
            "LD_LIBRARY_PATH": ("/verified/tools/lib",),
        },
        environment_operations={
            "PATH": "PREPEND_PATH",
            "LD_LIBRARY_PATH": "PREPEND_PATH",
            "PYTHONDONTWRITEBYTECODE": "SET_LITERAL",
        },
        literal_environment={"PYTHONDONTWRITEBYTECODE": "1"},
        tool_fingerprints={},
    )

    environment = _smoke_environment(verified, tmp_path)

    assert environment["PATH"] == "/verified/tools/bin:/usr/local/bin:/usr/bin:/bin"
    assert environment["LD_LIBRARY_PATH"] == "/verified/tools/lib"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "MAKEFLAGS" not in environment
    assert "CORNER" not in environment
    assert "LIB_FILES" not in environment
    assert "TMPDIR" not in environment


def test_run_checked_persists_partial_log_and_kills_process_group_on_timeout(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "print('partial smoke output', flush=True); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )
    log_path = tmp_path / "timeout.log"

    with pytest.raises(SmokeError, match="timed out"):
        _run_checked(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            environment=dict(os.environ),
            log_path=log_path,
            timeout_seconds=0.5,
        )

    assert "partial smoke output" in log_path.read_text(encoding="utf-8")
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()


def test_sta_script_rebinds_libraries_to_verified_orfs_root(tmp_path: Path) -> None:
    lock = load_platform_lock(LOCK_PATH)
    old_root = Path("/old/unverified/orfs")
    moved_setup = lock.setup_corner.model_copy(
        update={
            "liberty_files": tuple(
                artifact.model_copy(update={"resolved_path": old_root / artifact.logical_path})
                for artifact in lock.setup_corner.liberty_files
            )
        }
    )
    moved_lock = lock.model_copy(update={"setup_corner": moved_setup})
    verified_root = tmp_path / "verified-orfs"

    sta_environment = _sta_environment(
        moved_lock,
        "SETUP",
        artifact_root=verified_root,
        netlist=tmp_path / "netlist.v",
        constraints=tmp_path / "smoke.sdc",
    )

    assert all(str(old_root) not in value for value in sta_environment.values())
    assert sta_environment["NOVA_SMOKE_LIB_01"] == str(
        verified_root / lock.setup_corner.liberty_files[0].logical_path
    )


def test_sta_paths_are_passed_as_tcl_environment_values(tmp_path: Path) -> None:
    lock = load_platform_lock(LOCK_PATH)
    tricky_root = tmp_path / "root with $dollar {brace} #hash"
    netlist = tmp_path / "mapped $netlist.v"
    constraints = tmp_path / "smoke {constraints}.sdc"

    sta_environment = _sta_environment(
        lock,
        "HOLD",
        artifact_root=tricky_root,
        netlist=netlist,
        constraints=constraints,
    )
    script = _sta_script(lock, "HOLD")

    assert str(tricky_root) not in script
    assert str(netlist) not in script
    assert str(constraints) not in script
    assert sta_environment["NOVA_SMOKE_NETLIST"] == str(netlist)
    assert sta_environment["NOVA_SMOKE_SDC"] == str(constraints)
    assert "read_liberty $::env(NOVA_SMOKE_LIB_01)" in script


def test_orfs_config_does_not_interpolate_runtime_paths() -> None:
    config = _orfs_config()

    assert str(PROJECT_ROOT) not in config
    assert "$(value NOVA_SMOKE_RTL)" in config
    assert "$(value NOVA_SMOKE_SDC)" in config
    assert "$(value NOVA_SMOKE_WORK_HOME)" in config
