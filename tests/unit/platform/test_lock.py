from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nova_rtl.contracts.platform import (
    HostPlatform,
    PlatformArtifact,
    PlatformLock,
    TimingCorner,
    ToolFingerprint,
)
from nova_rtl.platform.doctor import run_doctor
from nova_rtl.platform.lock import (
    dump_platform_lock,
    load_platform_lock,
    platform_content_identity_hash,
    verify_platform_lock,
)
from nova_rtl.platform.probe import ToolProbeError, probe_executable


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def tool_build_hash(executable_bytes: bytes, stdout: bytes, stderr: bytes = b"") -> str:
    return sha256_bytes(executable_bytes + stdout + stderr)


def write_artifact(
    root: Path,
    artifact_id: str,
    kind: str,
    logical_path: str,
    data: bytes,
) -> PlatformArtifact:
    path = root / logical_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return PlatformArtifact(
        artifact_id=artifact_id,
        kind=kind,
        logical_path=logical_path,
        resolved_path=path.resolve(),
        sha256=sha256_bytes(data),
        size_bytes=len(data),
    )


def materialized_lock(tmp_path: Path) -> PlatformLock:
    executable = tmp_path / "tools" / "yosys"
    executable.parent.mkdir(parents=True)
    executable_bytes = b"#!/bin/sh\nprintf 'Yosys 1.0\\n'\n"
    executable.write_bytes(executable_bytes)
    executable.chmod(0o755)

    setup_lib = write_artifact(
        tmp_path,
        "asap7_setup_lib",
        "LIBERTY",
        "platform/lib/setup.lib",
        b"setup liberty",
    )
    hold_lib = write_artifact(
        tmp_path,
        "asap7_hold_lib",
        "LIBERTY",
        "platform/lib/hold.lib",
        b"hold liberty",
    )
    lock = PlatformLock(
        schema_version=1,
        platform_id="asap7",
        source_manifest_hash=sha256_bytes(b"manifest"),
        orfs_commit="a" * 40,
        host=HostPlatform(os="linux", architecture="x86_64"),
        tool_fingerprints=(
            ToolFingerprint(
                tool_id="yosys",
                executable=str(executable.resolve()),
                version="Yosys 1.0",
                version_args=("-V",),
                executable_sha256=sha256_bytes(executable_bytes),
                build_hash=tool_build_hash(executable_bytes, b"Yosys 1.0\n"),
                adapter_version="bootstrap-doctor-v1",
                container_digest=None,
            ),
        ),
        setup_corner=TimingCorner(
            corner_id="asap7_wc",
            role="SETUP",
            library_model="NLDM",
            liberty_files=(setup_lib,),
            voltage_v=0.63,
            temperature_c=100.0,
            native_time_unit="PS",
        ),
        hold_corner=TimingCorner(
            corner_id="asap7_bc",
            role="HOLD",
            library_model="NLDM",
            liberty_files=(hold_lib,),
            voltage_v=0.77,
            temperature_c=25.0,
            native_time_unit="PS",
        ),
        reference_corner=None,
        tech_lef=write_artifact(
            tmp_path,
            "asap7_tech_lef",
            "TECH_LEF",
            "platform/lef/tech.lef",
            b"tech lef",
        ),
        cell_lefs=(
            write_artifact(
                tmp_path,
                "asap7_cell_lef",
                "CELL_LEF",
                "platform/lef/cells.lef",
                b"cell lef",
            ),
        ),
        rc_rules=write_artifact(
            tmp_path,
            "asap7_rc_rules",
            "RC_RULES",
            "platform/rcx.rules",
            b"rc rules",
        ),
        flow_config=write_artifact(
            tmp_path,
            "asap7_flow_config",
            "FLOW_CONFIG",
            "platform/config.mk",
            b"flow config",
        ),
        license_artifacts=(
            write_artifact(
                tmp_path,
                "asap7_license",
                "LICENSE",
                "platform/LICENSE",
                b"BSD-3-Clause",
            ),
        ),
        content_identity_hash=sha256_bytes(b"content identity"),
        generated_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    return lock.model_copy(
        update={"content_identity_hash": platform_content_identity_hash(lock)}
    )


def test_platform_lock_round_trip_is_byte_deterministic(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    first_path = tmp_path / "first.lock.json"
    second_path = tmp_path / "second.lock.json"

    dump_platform_lock(lock, first_path)
    loaded = load_platform_lock(first_path)
    dump_platform_lock(loaded, second_path)

    assert loaded == lock
    assert first_path.read_bytes() == second_path.read_bytes()


def test_platform_lock_verifies_exact_artifact_and_tool_bytes(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)

    result = verify_platform_lock(lock)

    assert result.status == "PASS"
    assert result.issues == ()


def test_platform_lock_verification_hashes_the_parsed_byte_snapshot(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    lock_path = tmp_path / "platform.lock.json"
    dump_platform_lock(lock, lock_path)
    expected_hash = sha256_bytes(lock_path.read_bytes())

    result = verify_platform_lock(lock_path)

    assert result.status == "PASS"
    assert result.lock_hash == expected_hash


def test_platform_lock_detects_tampered_platform_artifact(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    lock.setup_corner.liberty_files[0].resolved_path.write_bytes(b"tampered")

    result = verify_platform_lock(lock)

    assert result.status == "FAIL"
    assert [(issue.code, issue.subject) for issue in result.issues] == [
        ("ARTIFACT_SIZE_MISMATCH", "asap7_setup_lib"),
        ("ARTIFACT_HASH_MISMATCH", "asap7_setup_lib"),
    ]


def test_platform_lock_detects_tampered_tool_executable(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    Path(lock.tool_fingerprints[0].executable).write_bytes(b"changed executable")

    result = verify_platform_lock(lock)

    assert [(issue.code, issue.subject) for issue in result.issues] == [
        ("TOOL_EXECUTABLE_HASH_MISMATCH", "yosys")
    ]


def test_tampered_tool_is_not_executed_before_hash_rejection(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    executable = Path(lock.tool_fingerprints[0].executable)
    marker = tmp_path / "executed.marker"
    executable.write_text(
        f"#!/bin/sh\nprintf tampered > {marker}\nprintf 'Yosys 1.0\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = verify_platform_lock(lock)

    assert result.status == "FAIL"
    assert not marker.exists()
    assert result.issues[0].code == "TOOL_EXECUTABLE_HASH_MISMATCH"


def test_platform_lock_verification_never_executes_locked_path(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    executable = Path(lock.tool_fingerprints[0].executable)
    marker = tmp_path / "executed.marker"
    executable_bytes = (
        f"#!/bin/sh\nprintf executed > {marker}\nprintf 'Yosys 1.0\\n'\n".encode()
    )
    executable.write_bytes(executable_bytes)
    executable.chmod(0o755)
    fingerprint = lock.tool_fingerprints[0].model_copy(
        update={
            "executable_sha256": sha256_bytes(executable_bytes),
            "build_hash": tool_build_hash(executable_bytes, b"Yosys 1.0\n"),
        }
    )
    changed = lock.model_copy(update={"tool_fingerprints": (fingerprint,)})
    changed = changed.model_copy(
        update={"content_identity_hash": platform_content_identity_hash(changed)}
    )

    result = verify_platform_lock(changed)

    assert result.status == "PASS"
    assert not marker.exists()


def test_probe_preserves_installed_path_for_relative_wrapper(tmp_path: Path) -> None:
    executable = tmp_path / "yosys"
    version_file = tmp_path / "version.txt"
    version_file.write_text("Yosys 1.0\n", encoding="utf-8")
    executable.write_text(
        "#!/bin/sh\ncat \"$(dirname \"$0\")/version.txt\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    fingerprint = probe_executable("yosys", executable)

    assert fingerprint.version == "Yosys 1.0"


def test_probe_rejects_executable_changed_during_version_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "yosys"
    executable.write_text("#!/bin/sh\nprintf 'Yosys 1.0\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    probe_module = importlib.import_module("nova_rtl.platform.probe")
    real_run = probe_module.subprocess.run

    def run_then_mutate(*args: object, **kwargs: object):
        result = real_run(*args, **kwargs)
        executable.write_text("#!/bin/sh\nprintf 'changed\\n'\n", encoding="utf-8")
        executable.chmod(0o755)
        return result

    monkeypatch.setattr(probe_module.subprocess, "run", run_then_mutate)

    with pytest.raises(ToolProbeError, match="changed during version probe"):
        probe_executable("yosys", executable)


def test_doctor_correlates_live_probe_with_locked_fingerprint(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    lock_path = tmp_path / "platform.lock.json"
    dump_platform_lock(lock, lock_path)
    executable = lock.tool_fingerprints[0].executable

    report = run_doctor(
        required_tools=("yosys",),
        platform_lock=lock_path,
        which=lambda _: executable,
    )

    assert report.status == "PASS"


def test_doctor_rejects_live_probe_that_disagrees_with_lock(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    false_fingerprint = lock.tool_fingerprints[0].model_copy(
        update={"version": "Yosys 2.0", "build_hash": sha256_bytes(b"false build")}
    )
    changed = lock.model_copy(update={"tool_fingerprints": (false_fingerprint,)})
    changed = changed.model_copy(
        update={"content_identity_hash": platform_content_identity_hash(changed)}
    )
    lock_path = tmp_path / "platform.lock.json"
    dump_platform_lock(changed, lock_path)

    report = run_doctor(
        required_tools=("yosys",),
        platform_lock=lock_path,
        which=lambda _: false_fingerprint.executable,
    )

    assert report.status == "FAIL"
    assert report.checks[0].issues[0].code == "TOOL_LOCK_MISMATCH"


def test_doctor_rejects_required_tool_missing_from_lock(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    lock_path = tmp_path / "platform.lock.json"
    dump_platform_lock(lock, lock_path)
    executable = tmp_path / "extra_tool"
    executable.write_text("#!/bin/sh\nprintf 'extra 1.0\\n'\n", encoding="utf-8")
    executable.chmod(0o755)

    report = run_doctor(
        required_tools=("extra_tool",),
        platform_lock=lock_path,
        which=lambda _: str(executable),
    )

    assert report.status == "FAIL"
    assert report.checks[0].issues[0].code == "TOOL_LOCK_MISMATCH"
    assert "absent from the platform lock" in report.checks[0].message


def test_doctor_fails_when_referenced_platform_bytes_are_tampered(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    lock_path = tmp_path / "platform.lock.json"
    dump_platform_lock(lock, lock_path)
    lock.setup_corner.liberty_files[0].resolved_path.write_bytes(b"tampered")

    report = run_doctor(required_tools=(), platform_lock=lock_path)

    assert report.status == "FAIL"
    assert report.exit_code == 2
    assert report.checks[0].name == "platform_lock"
    assert "ARTIFACT_HASH_MISMATCH:asap7_setup_lib" in report.checks[0].message
    assert [issue.code for issue in report.checks[0].issues] == [
        "ARTIFACT_SIZE_MISMATCH",
        "ARTIFACT_HASH_MISMATCH",
    ]


def test_platform_lock_rejects_stale_content_identity(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)
    changed = lock.model_copy(update={"orfs_commit": "b" * 40})

    result = verify_platform_lock(changed)

    assert result.status == "FAIL"
    assert result.issues[0].code == "CONTENT_IDENTITY_MISMATCH"


def test_non_utf8_version_output_returns_stable_failure(tmp_path: Path) -> None:
    executable = tmp_path / "yosys"
    executable_bytes = b"#!/bin/sh\nprintf '\\377\\376\\n'\n"
    executable.write_bytes(executable_bytes)
    executable.chmod(0o755)

    with pytest.raises(ToolProbeError, match="not valid UTF-8"):
        probe_executable("yosys", executable)


def test_dump_platform_lock_rejects_unknown_extension(tmp_path: Path) -> None:
    lock = materialized_lock(tmp_path)

    with pytest.raises(ValueError, match="unsupported platform-lock format"):
        dump_platform_lock(lock, tmp_path / "platform.lock.txt")
