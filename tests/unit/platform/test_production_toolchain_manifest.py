from __future__ import annotations

from pathlib import Path

from nova_rtl.platform.hydration import (
    load_toolchain_source_manifest,
    manifest_content_identity_hash,
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "platform" / "toolchain-sources.json"
)
EXPECTED_MANIFEST_HASH = "sha256:fcd2317ef809d1d10c6010844ab4c57d7a2bee029906c98011d87322adeb1cc9"


def test_production_toolchain_manifest_is_strict_and_canonically_stable() -> None:
    first = load_toolchain_source_manifest(MANIFEST_PATH)
    second = load_toolchain_source_manifest(MANIFEST_PATH)
    components = {component.component_id: component for component in first.manifest.components}

    assert first == second
    assert first.content_identity_hash == EXPECTED_MANIFEST_HASH
    assert first.content_identity_hash == manifest_content_identity_hash(first.manifest)
    assert tuple(components) == (
        "oss_cad_suite",
        "openroad",
        "libpython3_12t64",
        "tcl_tclreadline",
        "libqt5charts5",
        "libyaml_cpp0_8",
        "orfs",
    )
    assert {
        executable.tool_id: (executable.relative_path, executable.version_args)
        for component in first.manifest.components
        for executable in component.executables
    } == {
        "yosys": ("bin/yosys", ("-V",)),
        "eqy": ("bin/eqy", ("--version",)),
        "sby": ("bin/sby", ("--version",)),
        "slang": ("bin/slang", ("--version",)),
        "iverilog": ("bin/iverilog", ("-V",)),
        "verilator": ("bin/verilator", ("--version",)),
        "openroad": ("usr/bin/openroad", ("-version",)),
        "opensta": ("usr/bin/sta", ("-version",)),
    }
    assert components["oss_cad_suite"].archive_sha256 == (
        "sha256:89ea1152ea84bc600f18cc685f721d534d1f018e09831662787865a3d79ce4aa"
    )
    assert components["oss_cad_suite"].archive.byte_size == 737344018
    assert components["oss_cad_suite"].archive.max_entries == 25000
    assert components["orfs"].git_commit == "4c06bcb2466996a90d31101d85d705ad015950bc"
