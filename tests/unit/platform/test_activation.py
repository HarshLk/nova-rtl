from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nova_rtl.cli import app
from nova_rtl.contracts.platform import (
    ArchiveMetadata,
    HostPlatform,
    RuntimeEnvironmentEntry,
    ToolchainSourceManifest,
    ToolExecutableSource,
    ToolSource,
)
from nova_rtl.platform.activation import (
    ToolchainVerificationError,
    create_toolchain_receipt,
    render_shell_environment,
    verify_toolchain,
)
from nova_rtl.platform.hydration import component_receipt_bytes, manifest_content_identity_hash


def hash_ref(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def source() -> ToolSource:
    return ToolSource(
        component_id="suite",
        source_kind="ARCHIVE",
        version="1.0",
        source_url="https://downloads.example.test/suite.tar.gz",
        archive_sha256=hash_ref(b"archive"),
        git_commit=None,
        license="ISC",
        executables=(
            ToolExecutableSource(tool_id="yosys", relative_path="bin/yosys", version_args=("-V",)),
        ),
        archive=ArchiveMetadata(
            byte_size=7,
            archive_format="TAR_GZ",
            strip_components=1,
            max_decompressed_bytes=1024,
            max_regular_file_bytes=1024,
            max_entries=8,
        ),
        runtime_environment=(
            RuntimeEnvironmentEntry(
                name="PATH", operation="PREPEND_PATH", relative_paths=("bin", "py3bin")
            ),
            RuntimeEnvironmentEntry(name="SUITE_ROOT", operation="SET", relative_paths=("share",)),
        ),
        allowed_redirect_hosts=(),
    )


def manifest() -> ToolchainSourceManifest:
    return ToolchainSourceManifest(
        host=HostPlatform(os="linux", architecture="x86_64"),
        tool_root_name=".nova-tools",
        components=(source(),),
    )


def hydrated_root(tmp_path: Path) -> tuple[ToolchainSourceManifest, Path]:
    source_manifest = manifest()
    root = tmp_path / ".nova-tools"
    component = root / "components" / "suite"
    (component / "bin").mkdir(parents=True)
    (component / "py3bin").mkdir()
    (component / "share").mkdir()
    executable = component / "bin" / "yosys"
    executable.write_text("#!/bin/sh\nprintf 'yosys 1.0\\n'\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    receipt = component_receipt_bytes(manifest_content_identity_hash(source_manifest), source())
    (component / ".nova-hydration-receipt.json").write_bytes(receipt)
    (root / "receipts").mkdir()
    (root / "receipts" / "suite.json").write_bytes(receipt)
    return source_manifest, root


def test_receipt_verification_is_offline_and_detects_component_mutation(tmp_path: Path) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    create_toolchain_receipt(source_manifest, root)

    verified = verify_toolchain(source_manifest, root)
    assert verified.tool_paths["yosys"] == root / "components" / "suite" / "bin" / "yosys"
    assert verified.canonical_environment["PATH"] == (
        str(root / "components" / "suite" / "bin"),
        str(root / "components" / "suite" / "py3bin"),
    )

    (root / "components" / "suite" / "share" / "changed").write_text("changed")
    with pytest.raises(ToolchainVerificationError, match="tree identity"):
        verify_toolchain(source_manifest, root)


def test_receipt_verification_rejects_receipt_paths_outside_the_root(tmp_path: Path) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    receipt_path = create_toolchain_receipt(source_manifest, root)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["tool_fingerprints"][0]["executable"] = str(Path("/bin/sh").resolve())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ToolchainVerificationError, match="receipt executable"):
        verify_toolchain(source_manifest, root)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda receipt: receipt.update({"unexpected": True}),
        lambda receipt: receipt["components"][0].update({"unexpected": True}),
        lambda receipt: receipt["components"][0]["inventory"][0].update({"unexpected": True}),
        lambda receipt: receipt["environment"]["PATH"].update({"unexpected": True}),
    ),
)
def test_receipt_verification_rejects_unknown_fields(tmp_path: Path, mutate) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    receipt_path = create_toolchain_receipt(source_manifest, root)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutate(receipt)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ToolchainVerificationError, match="global toolchain receipt is invalid"):
        verify_toolchain(source_manifest, root)


def test_shell_environment_quotes_rooted_values_and_keeps_ambient_path_out_of_receipt(
    tmp_path: Path,
) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    create_toolchain_receipt(source_manifest, root)
    verified = verify_toolchain(source_manifest, root)

    shell = render_shell_environment(
        verified.canonical_environment, verified.environment_operations
    )
    assert "export PATH=" in shell
    assert "${PATH:+:$PATH}" in shell
    assert os.environ.get("PATH", "") not in shell
    assert "export SUITE_ROOT=" in shell


def test_shell_environment_rejects_unreviewed_variable_names() -> None:
    with pytest.raises(ToolchainVerificationError, match="environment variable"):
        render_shell_environment(
            {"PATH\nUNSAFE": ("/tmp/tools",)}, {"PATH\nUNSAFE": "PREPEND_PATH"}
        )


def test_toolchain_env_cli_verifies_before_emitting_exports(tmp_path: Path) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    create_toolchain_receipt(source_manifest, root)
    manifest_path = tmp_path / "sources.json"
    manifest_path.write_text(json.dumps(source_manifest.model_dump(mode="json")), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["toolchain", "env", "--manifest", str(manifest_path), "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "export PATH=" in result.stdout
    (root / "components" / "suite" / "share" / "changed").write_text("changed")
    failed = CliRunner().invoke(
        app,
        ["toolchain", "env", "--manifest", str(manifest_path), "--project-root", str(tmp_path)],
    )
    assert failed.exit_code == 2
    assert not failed.stdout


def test_toolchain_hydrate_and_verify_cli_emit_machine_readable_results(tmp_path: Path) -> None:
    source_manifest, root = hydrated_root(tmp_path)
    manifest_path = tmp_path / "sources.json"
    manifest_path.write_text(json.dumps(source_manifest.model_dump(mode="json")), encoding="utf-8")

    hydrated = CliRunner().invoke(
        app,
        [
            "toolchain",
            "hydrate",
            "--manifest",
            str(manifest_path),
            "--project-root",
            str(tmp_path),
            "--json",
        ],
    )
    assert hydrated.exit_code == 0
    assert json.loads(hydrated.stdout) == {
        "receipt": str(root / "toolchain-receipt.json"),
        "status": "PASS",
    }

    verified = CliRunner().invoke(
        app,
        [
            "toolchain",
            "verify",
            "--manifest",
            str(manifest_path),
            "--project-root",
            str(tmp_path),
            "--json",
        ],
    )
    assert verified.exit_code == 0
    assert json.loads(verified.stdout) == {
        "receipt": str(root / "toolchain-receipt.json"),
        "status": "PASS",
    }

    (root / "components" / "suite" / "share" / "changed").write_text("changed")
    failed = CliRunner().invoke(
        app,
        [
            "toolchain",
            "verify",
            "--manifest",
            str(manifest_path),
            "--project-root",
            str(tmp_path),
            "--json",
        ],
    )
    assert failed.exit_code == 2
    assert json.loads(failed.stdout)["status"] == "FAIL"


def test_doctor_cli_consumes_verified_toolchain_mapping(tmp_path: Path) -> None:
    source_manifest, _ = hydrated_root(tmp_path)
    create_toolchain_receipt(source_manifest, tmp_path / ".nova-tools")
    manifest_path = tmp_path / "sources.json"
    manifest_path.write_text(json.dumps(source_manifest.model_dump(mode="json")), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--tool",
            "yosys",
            "--toolchain-manifest",
            str(manifest_path),
            "--project-root",
            str(tmp_path),
            "--json",
        ],
        env={"PATH": ""},
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "PASS"
