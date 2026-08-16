from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.platform import (
    ArchiveMetadata,
    DoctorCheck,
    DoctorIssue,
    DoctorReport,
    HostPlatform,
    PlatformArtifact,
    PlatformLock,
    RuntimeEnvironmentEntry,
    TimingCorner,
    ToolchainSourceManifest,
    ToolExecutableSource,
    ToolFingerprint,
    ToolSource,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def archive_metadata() -> ArchiveMetadata:
    return ArchiveMetadata(
        byte_size=1,
        archive_format="TAR_GZ",
        strip_components=1,
        max_decompressed_bytes=1024,
        max_regular_file_bytes=1024,
        max_entries=8,
    )


def artifact(
    artifact_id: str,
    kind: str,
    logical_path: str,
    resolved_path: Path,
    digit: str,
) -> PlatformArtifact:
    return PlatformArtifact(
        artifact_id=artifact_id,
        kind=kind,
        logical_path=logical_path,
        resolved_path=resolved_path,
        sha256=hash_ref(digit),
        size_bytes=1,
    )


def fingerprint(tool_id: str, executable: Path, digit: str) -> ToolFingerprint:
    return ToolFingerprint(
        tool_id=tool_id,
        executable=str(executable),
        version=f"{tool_id} 1.0",
        version_args=("-V",),
        executable_sha256=hash_ref(digit),
        build_hash=hash_ref(digit),
        adapter_version="bootstrap-doctor-v1",
        container_digest=None,
    )


def valid_lock(tmp_path: Path) -> PlatformLock:
    setup_lib = artifact(
        "asap7_setup_lib",
        "LIBERTY",
        "asap7/lib/setup.lib",
        tmp_path / "setup.lib",
        "1",
    )
    hold_lib = artifact(
        "asap7_hold_lib",
        "LIBERTY",
        "asap7/lib/hold.lib",
        tmp_path / "hold.lib",
        "2",
    )
    return PlatformLock(
        schema_version=1,
        platform_id="asap7",
        source_manifest_hash=hash_ref("3"),
        orfs_commit="a" * 40,
        host=HostPlatform(os="linux", architecture="x86_64"),
        tool_fingerprints=(fingerprint("yosys", tmp_path / "yosys", "4"),),
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
        tech_lef=artifact(
            "asap7_tech_lef",
            "TECH_LEF",
            "asap7/lef/tech.lef",
            tmp_path / "tech.lef",
            "5",
        ),
        cell_lefs=(
            artifact(
                "asap7_cell_lef",
                "CELL_LEF",
                "asap7/lef/cells.lef",
                tmp_path / "cells.lef",
                "6",
            ),
        ),
        rc_rules=artifact(
            "asap7_rc_rules",
            "RC_RULES",
            "asap7/rcx.rules",
            tmp_path / "rcx.rules",
            "7",
        ),
        flow_config=artifact(
            "asap7_flow_config",
            "FLOW_CONFIG",
            "asap7/config.mk",
            tmp_path / "config.mk",
            "8",
        ),
        license_artifacts=(
            artifact(
                "asap7_license",
                "LICENSE",
                "asap7/LICENSE",
                tmp_path / "LICENSE",
                "9",
            ),
        ),
        content_identity_hash=hash_ref("a"),
        generated_at=datetime(2026, 8, 15, tzinfo=UTC),
    )


def test_archive_source_rejects_moving_latest_url() -> None:
    with pytest.raises(ValidationError, match="moving latest URL"):
        ToolSource(
            component_id="oss_cad_suite",
            source_kind="ARCHIVE",
            version="2026-07-29",
            source_url="https://example.test/releases/latest/toolchain.tgz",
            archive_sha256=hash_ref("1"),
            git_commit=None,
            license="ISC",
            executables=(
                ToolExecutableSource(
                    tool_id="yosys",
                    relative_path="bin/yosys",
                    version_args=("-V",),
                ),
            ),
            archive=archive_metadata(),
            allowed_redirect_hosts=(),
        )


def test_git_source_requires_full_commit_and_no_archive_hash() -> None:
    with pytest.raises(ValidationError, match="full 40-character Git commit"):
        ToolSource(
            component_id="orfs",
            source_kind="GIT",
            version="orfs-pinned",
            source_url="https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git",
            archive_sha256=None,
            git_commit="main",
            license="BSD-3-Clause",
            executables=(),
            archive=None,
            allowed_redirect_hosts=(),
        )


def test_manifest_rejects_duplicate_executable_ownership() -> None:
    executable = ToolExecutableSource(
        tool_id="yosys",
        relative_path="bin/yosys",
        version_args=("-V",),
    )
    first = ToolSource(
        component_id="first_suite",
        source_kind="ARCHIVE",
        version="1",
        source_url="https://example.test/releases/1/first.tgz",
        archive_sha256=hash_ref("1"),
        git_commit=None,
        license="ISC",
        executables=(executable,),
        archive=archive_metadata(),
        allowed_redirect_hosts=(),
    )
    second = ToolSource(
        component_id="second_suite",
        source_kind="ARCHIVE",
        version="1",
        source_url="https://example.test/releases/1/second.tgz",
        archive_sha256=hash_ref("2"),
        git_commit=None,
        license="ISC",
        executables=(executable,),
        archive=archive_metadata(),
        allowed_redirect_hosts=(),
    )

    with pytest.raises(ValidationError, match="owned by more than one component"):
        ToolchainSourceManifest(
            schema_version=1,
            host=HostPlatform(os="linux", architecture="x86_64"),
            tool_root_name=".nova-tools",
            components=(first, second),
        )


def test_runtime_environment_requires_only_nonempty_relative_paths() -> None:
    with pytest.raises(ValidationError, match="relative_paths"):
        RuntimeEnvironmentEntry.model_validate(
            {"name": "PATH", "operation": "PREPEND_PATH", "relative_path": "bin"}
        )

    with pytest.raises(ValidationError, match="at least 1 item"):
        RuntimeEnvironmentEntry(name="PATH", operation="PREPEND_PATH", relative_paths=())

    with pytest.raises(ValidationError, match="exactly one path"):
        RuntimeEnvironmentEntry(name="TOOL_ROOT", operation="SET", relative_paths=("share", "lib"))


def test_manifest_rejects_cross_component_runtime_operation_conflict() -> None:
    first = ToolSource(
        component_id="first_suite",
        source_kind="ARCHIVE",
        version="1",
        source_url="https://example.test/releases/1/first.tgz",
        archive_sha256=hash_ref("1"),
        git_commit=None,
        license="ISC",
        executables=(),
        archive=archive_metadata(),
        runtime_environment=(
            RuntimeEnvironmentEntry(
                name="LD_LIBRARY_PATH", operation="PREPEND_PATH", relative_paths=("lib",)
            ),
        ),
        allowed_redirect_hosts=(),
    )
    second = ToolSource(
        component_id="second_suite",
        source_kind="ARCHIVE",
        version="1",
        source_url="https://example.test/releases/1/second.tgz",
        archive_sha256=hash_ref("2"),
        git_commit=None,
        license="ISC",
        executables=(),
        archive=archive_metadata(),
        runtime_environment=(
            RuntimeEnvironmentEntry(
                name="LD_LIBRARY_PATH", operation="SET", relative_paths=("lib",)
            ),
        ),
        allowed_redirect_hosts=(),
    )

    with pytest.raises(ValidationError, match="mixes SET and PREPEND_PATH"):
        ToolchainSourceManifest(
            host=HostPlatform(os="linux", architecture="x86_64"),
            tool_root_name=".nova-tools",
            components=(first, second),
        )


def test_manifest_rejects_multiple_set_contributions_for_one_variable() -> None:
    def component(component_id: str, digit: str) -> ToolSource:
        return ToolSource(
            component_id=component_id,
            source_kind="ARCHIVE",
            version="1",
            source_url=f"https://example.test/releases/1/{component_id}.tgz",
            archive_sha256=hash_ref(digit),
            git_commit=None,
            license="ISC",
            executables=(),
            archive=archive_metadata(),
            runtime_environment=(
                RuntimeEnvironmentEntry(
                    name="TOOL_ROOT", operation="SET", relative_paths=("share",)
                ),
            ),
            allowed_redirect_hosts=(),
        )

    with pytest.raises(ValidationError, match="more than one SET contribution"):
        ToolchainSourceManifest(
            host=HostPlatform(os="linux", architecture="x86_64"),
            tool_root_name=".nova-tools",
            components=(component("first_suite", "1"), component("second_suite", "2")),
        )


def test_platform_artifact_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="relative path"):
        artifact(
            "unsafe_artifact",
            "LIBERTY",
            "../outside.lib",
            tmp_path / "outside.lib",
            "1",
        )


def test_platform_artifact_rejects_symlink_loop(tmp_path: Path) -> None:
    loop = tmp_path / "loop.lib"
    loop.symlink_to(loop.name)

    with pytest.raises(ValidationError, match="cannot be resolved"):
        artifact("loop_artifact", "LIBERTY", "platform/loop.lib", loop, "1")


@pytest.mark.parametrize(
    "path",
    ("bin/./yosys", "bin//yosys", "bin/yosys/", "bin\\yosys", "bin/\x00yosys"),
)
def test_tool_path_rejects_noncanonical_spelling(path: str) -> None:
    with pytest.raises(ValidationError, match="normalized relative path"):
        ToolExecutableSource(tool_id="yosys", relative_path=path, version_args=("-V",))


def test_platform_lock_requires_setup_and_hold_roles(tmp_path: Path) -> None:
    lock = valid_lock(tmp_path)

    with pytest.raises(ValidationError, match="setup_corner must have role SETUP"):
        PlatformLock.model_validate(
            {
                **lock.model_dump(),
                "setup_corner": lock.setup_corner.model_copy(update={"role": "HOLD"}),
            }
        )


def test_platform_lock_rejects_same_liberty_bytes_for_both_corners(tmp_path: Path) -> None:
    lock = valid_lock(tmp_path)
    reused_hold = lock.hold_corner.model_copy(
        update={"liberty_files": lock.setup_corner.liberty_files}
    )

    with pytest.raises(ValidationError, match="distinct Liberty content"):
        PlatformLock.model_validate({**lock.model_dump(), "hold_corner": reused_hold})


def test_platform_lock_rejects_duplicate_tool_ids(tmp_path: Path) -> None:
    lock = valid_lock(tmp_path)

    with pytest.raises(ValidationError, match="duplicate tool_id"):
        PlatformLock.model_validate(
            {
                **lock.model_dump(),
                "tool_fingerprints": (
                    lock.tool_fingerprints[0],
                    lock.tool_fingerprints[0],
                ),
            }
        )


def test_doctor_report_rejects_naive_generated_timestamp() -> None:
    failed_check = DoctorCheck(
        name="yosys",
        status="FAIL",
        resolved_path=None,
        tool_fingerprint=None,
        artifact_hash=None,
        issues=(DoctorIssue(code="TOOL_MISSING", subject="yosys", message="missing"),),
        message="missing",
    )

    with pytest.raises(ValidationError, match="timezone_aware"):
        DoctorReport(
            status="FAIL",
            checks=(failed_check,),
            platform_lock_hash=None,
            generated_at=datetime(2026, 8, 15),
            exit_code=2,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("voltage_v", float("inf")), ("temperature_c", float("nan"))),
)
def test_timing_corner_rejects_non_finite_values(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    lock = valid_lock(tmp_path)

    with pytest.raises(ValidationError, match="finite_number"):
        TimingCorner.model_validate({**lock.setup_corner.model_dump(), field: value})


def test_tool_fingerprint_rejects_unknown_probe_adapter(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ToolFingerprint(
            tool_id="yosys",
            executable=str((tmp_path / "yosys").resolve()),
            version="Yosys 1.0",
            version_args=("-V",),
            executable_sha256=hash_ref("1"),
            build_hash=hash_ref("2"),
            adapter_version="unknown-adapter",
            container_digest=None,
        )


def test_tool_fingerprint_rejects_unregistered_version_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="registered probe recipe"):
        ToolFingerprint(
            tool_id="yosys",
            executable=str((tmp_path / "yosys").resolve()),
            version="Yosys 1.0",
            version_args=("--help",),
            executable_sha256=hash_ref("1"),
            build_hash=hash_ref("2"),
            adapter_version="bootstrap-doctor-v1",
            container_digest=None,
        )
