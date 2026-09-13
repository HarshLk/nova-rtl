from __future__ import annotations

from pathlib import Path

from nova_rtl.platform.hydration import (
    load_toolchain_source_manifest,
    manifest_content_identity_hash,
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "platform" / "toolchain-sources.json"
)
EXPECTED_MANIFEST_HASH = "sha256:d9f06b235a7180a5c8bdf99e4e74187c4c4e405d356b9e60df850b9c0a14b423"


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
        "libdouble_conversion3",
        "libmd4c0",
        "libpcre2_16_0",
        "libicu74",
        "libqt5core5t64",
        "libqt5gui5t64",
        "libqt5widgets5t64",
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
    assert {
        entry.name: (entry.operation, entry.relative_paths, entry.literal_value)
        for entry in components["tcl_tclreadline"].runtime_environment
    } == {
        "LD_LIBRARY_PATH": ("PREPEND_PATH", ("usr/lib/x86_64-linux-gnu",), None),
        "TCLLIBPATH": ("SET", ("usr/lib/tcltk/x86_64-linux-gnu",), None),
    }
    assert {
        entry.name: (entry.operation, entry.relative_paths, entry.literal_value)
        for entry in components["oss_cad_suite"].runtime_environment
    }["PYTHONDONTWRITEBYTECODE"] == ("SET_LITERAL", (), "1")


def test_production_openroad_runtime_pins_missing_ubuntu_libraries() -> None:
    loaded = load_toolchain_source_manifest(MANIFEST_PATH)
    components = {component.component_id: component for component in loaded.manifest.components}
    expected = {
        "libqt5core5t64": (
            "5.15.13+dfsg-1ubuntu1",
            "https://archive.ubuntu.com/ubuntu/pool/universe/q/qtbase-opensource-src/libqt5core5t64_5.15.13+dfsg-1ubuntu1_amd64.deb",
            "sha256:8fb5c6a51ae436fefc41e0c9ad7a363ba8ab6b35db727eff5a469de2ee9f52bc",
            2010540,
            "LGPL-3.0-only",
        ),
        "libqt5gui5t64": (
            "5.15.13+dfsg-1ubuntu1",
            "https://archive.ubuntu.com/ubuntu/pool/universe/q/qtbase-opensource-src/libqt5gui5t64_5.15.13+dfsg-1ubuntu1_amd64.deb",
            "sha256:e2c8a969c3566bdad2b692c611ec0e6bbeaeed88db55e71bbd4f61a414b89252",
            3747576,
            "LGPL-3.0-only",
        ),
        "libqt5widgets5t64": (
            "5.15.13+dfsg-1ubuntu1",
            "https://archive.ubuntu.com/ubuntu/pool/universe/q/qtbase-opensource-src/libqt5widgets5t64_5.15.13+dfsg-1ubuntu1_amd64.deb",
            "sha256:bdcb4395194d5062fcbda9ba70ae6d4deef75671d70140543d58ef34728dff53",
            2560968,
            "LGPL-3.0-only",
        ),
        "libdouble_conversion3": (
            "3.3.0-1build1",
            "https://archive.ubuntu.com/ubuntu/pool/universe/d/double-conversion/libdouble-conversion3_3.3.0-1build1_amd64.deb",
            "sha256:856f534738da20fa9d8c271e17781fba3dc180bbbab116c22a0b569f6f506e25",
            40294,
            "BSD-3-Clause",
        ),
        "libmd4c0": (
            "0.4.8-1build1",
            "https://archive.ubuntu.com/ubuntu/pool/universe/m/md4c/libmd4c0_0.4.8-1build1_amd64.deb",
            "sha256:34fd2e7a7aa62ada2597cffd4529f086cb5058a6d7ac0048695254bd9fd882d7",
            42274,
            "MIT",
        ),
        "libpcre2_16_0": (
            "10.42-4ubuntu2.1",
            "https://archive.ubuntu.com/ubuntu/pool/main/p/pcre2/libpcre2-16-0_10.42-4ubuntu2.1_amd64.deb",
            "sha256:06bba768fd16e6ea6f744114a0c500d9f5d98ee82630edf0d8a54f2175d3921b",
            210092,
            "BSD-3-Clause",
        ),
        "libicu74": (
            "74.2-1ubuntu3",
            "https://archive.ubuntu.com/ubuntu/pool/main/i/icu/libicu74_74.2-1ubuntu3_amd64.deb",
            "sha256:d29c97a21a3e3254731cfac186e4d4e611e5e67d2c9a0430f6acfbd9acaefa2e",
            10860410,
            "MIT",
        ),
    }

    assert expected.keys() <= components.keys()
    for component_id, (
        version, source_url, archive_sha256, byte_size, license_id
    ) in expected.items():
        component = components[component_id]
        assert component.version == version
        assert component.source_url == source_url
        assert component.archive_sha256 == archive_sha256
        assert component.license == license_id
        assert component.archive is not None
        assert component.archive.archive_format == "DEB"
        assert component.archive.byte_size == byte_size
        assert {
            entry.name: (entry.operation, entry.relative_paths)
            for entry in component.runtime_environment
        } == {
            "LD_LIBRARY_PATH": ("PREPEND_PATH", ("usr/lib/x86_64-linux-gnu",))
        }


def test_production_openroad_runtime_exports_registered_executables() -> None:
    loaded = load_toolchain_source_manifest(MANIFEST_PATH)
    components = {component.component_id: component for component in loaded.manifest.components}

    assert {
        entry.name: (entry.operation, entry.relative_paths)
        for entry in components["openroad"].runtime_environment
    }["PATH"] == ("PREPEND_PATH", ("usr/bin",))
