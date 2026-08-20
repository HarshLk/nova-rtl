from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import yaml
from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.platform.activation import VerifiedToolchain
from nova_rtl.platform.lock import load_platform_lock, verify_platform_lock


def hash_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def write_platform(root: Path) -> None:
    platform = root / "flow/platforms/asap7"
    for relative, condition, voltage, temperature in (
        ("lib/setup.lib.gz", "PVT_0P63V_100C", 0.63, 100),
        ("lib/hold.lib.gz", "PVT_0P77V_0C", 0.77, 0),
    ):
        path = platform / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(
                f'''library (fixture) {{
  time_unit : "1ps";
  nom_voltage : {voltage};
  nom_temperature : {temperature};
  default_operating_conditions : {condition};
  operating_conditions ({condition}) {{ voltage : {voltage}; }}
}}
'''
            )
    for relative in ("lef/tech.lef", "lef/cells.lef", "setRC.tcl", "config.mk"):
        path = platform / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {relative}\n", encoding="utf-8")
    (root / "LICENSE_BUILD_RUN_SCRIPTS").write_text("BSD-3-Clause\n", encoding="utf-8")


def write_policy(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "platform_id": "asap7",
        "orfs_component_id": "orfs",
        "orfs_commit": "4c06bcb2466996a90d31101d85d705ad015950bc",
        "platform_root": "flow/platforms/asap7",
        "library_model": "NLDM",
        "setup_corner": {
            "corner_id": "asap7_wc",
            "role": "SETUP",
            "liberty_files": ["flow/platforms/asap7/lib/setup.lib.gz"],
        },
        "hold_corner": {
            "corner_id": "asap7_bc",
            "role": "HOLD",
            "liberty_files": ["flow/platforms/asap7/lib/hold.lib.gz"],
        },
        "reference_corner": None,
        "tech_lef": "flow/platforms/asap7/lef/tech.lef",
        "cell_lefs": ["flow/platforms/asap7/lef/cells.lef"],
        "rc_config": "flow/platforms/asap7/setRC.tcl",
        "flow_config": "flow/platforms/asap7/config.mk",
        "license_artifacts": ["LICENSE_BUILD_RUN_SCRIPTS"],
        "redistribution_status": "REVIEW_REQUIRED",
        "license_notes": ["ASAP7 collateral redistribution requires separate review."],
        "deterministic_seed": 0,
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_platform_lock_cli_uses_only_verified_orfs_and_writes_both_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    tool_root = project_root / ".nova-tools"
    orfs_root = tool_root / "components/orfs"
    write_platform(orfs_root)
    executable = tmp_path / "yosys"
    executable.write_bytes(b"yosys executable")
    fingerprint = ToolFingerprint(
        tool_id="yosys",
        executable=str(executable.resolve()),
        version="Yosys 1.0",
        version_args=("-V",),
        executable_sha256=hash_bytes(executable.read_bytes()),
        build_hash=hash_bytes(b"build"),
        adapter_version="bootstrap-doctor-v1",
        container_digest=None,
    )
    verified = VerifiedToolchain(
        root=tool_root.resolve(),
        receipt_path=(tool_root / "toolchain-receipt.json").resolve(),
        tool_paths={"yosys": executable.resolve()},
        canonical_environment={},
        environment_operations={},
        literal_environment={},
        tool_fingerprints={"yosys": fingerprint},
    )
    monkeypatch.setattr("nova_rtl.cli.verify_toolchain", lambda manifest, root: verified)
    policy = tmp_path / "policy.yaml"
    write_policy(policy)
    lock_path = tmp_path / "platform.lock.yaml"
    views_path = tmp_path / "views.yaml"
    manifest = Path(__file__).resolve().parents[3] / "config/platform/toolchain-sources.json"

    result = CliRunner().invoke(
        app,
        [
            "platform",
            "lock",
            "--root",
            str(orfs_root),
            "--policy",
            str(policy),
            "--toolchain-manifest",
            str(manifest),
            "--project-root",
            str(project_root),
            "--output",
            str(lock_path),
            "--views-output",
            str(views_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["platform"] == "asap7"
    assert verify_platform_lock(lock_path).status == "PASS"
    assert load_platform_lock(lock_path).orfs_commit == (
        "4c06bcb2466996a90d31101d85d705ad015950bc"
    )
    assert yaml.safe_load(views_path.read_text(encoding="utf-8"))["views"][1]["check"] == "HOLD"


def test_platform_lock_cli_rejects_unverified_orfs_root(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    verified_root = project_root / ".nova-tools"
    (verified_root / "components/orfs").mkdir(parents=True)
    unverified_orfs = tmp_path / "unverified-orfs"
    write_platform(unverified_orfs)
    policy = tmp_path / "policy.yaml"
    write_policy(policy)
    verified = VerifiedToolchain(
        root=verified_root.resolve(),
        receipt_path=(verified_root / "toolchain-receipt.json").resolve(),
        tool_paths={},
        canonical_environment={},
        environment_operations={},
        literal_environment={},
        tool_fingerprints={},
    )
    monkeypatch.setattr("nova_rtl.cli.verify_toolchain", lambda manifest, root: verified)
    manifest = Path(__file__).resolve().parents[3] / "config/platform/toolchain-sources.json"

    result = CliRunner().invoke(
        app,
        [
            "platform",
            "lock",
            "--root",
            str(unverified_orfs),
            "--policy",
            str(policy),
            "--toolchain-manifest",
            str(manifest),
            "--project-root",
            str(project_root),
            "--output",
            str(tmp_path / "lock.yaml"),
            "--views-output",
            str(tmp_path / "views.yaml"),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert __import__("json").loads(result.stdout) == {
        "error": "--root must be the verified hydrated ORFS component",
        "status": "FAIL",
    }
