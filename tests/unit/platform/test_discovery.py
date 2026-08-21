from __future__ import annotations

import gzip
import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from nova_rtl.contracts.platform import (
    HostPlatform,
    PlatformLockRequest,
    PlatformSelectionPolicy,
    ToolFingerprint,
)
from nova_rtl.platform.lock import (
    PlatformLockError,
    create_platform_lock,
    dump_analysis_views,
    load_platform_selection_policy,
    platform_content_identity_hash,
    publish_platform_outputs,
    verify_platform_lock,
)


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def write_liberty(
    path: Path,
    *,
    operating_condition: str,
    voltage: float,
    temperature: float,
) -> None:
    text = f'''library (fixture) {{
  time_unit : "1ps";
  nom_voltage : {voltage};
  nom_temperature : {temperature};
  default_operating_conditions : {operating_condition};
  operating_conditions ({operating_condition}) {{
    voltage : {voltage};
    temperature : {temperature};
  }}
}}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def fixture_orfs_root(tmp_path: Path) -> Path:
    root = tmp_path / "orfs"
    platform = root / "flow" / "platforms" / "asap7"
    write_liberty(
        platform / "lib" / "setup.lib.gz",
        operating_condition="PVT_0P63V_100C",
        voltage=0.63,
        temperature=100.0,
    )
    write_liberty(
        platform / "lib" / "hold.lib",
        operating_condition="FF_0.77_0",
        voltage=0.77,
        temperature=0.0,
    )
    write_liberty(
        platform / "lib" / "hold_legacy.lib",
        operating_condition="PVT_0P77V_0C",
        voltage=0.77,
        temperature=0.0,
    )
    for relative, content in {
        "flow/platforms/asap7/lef/tech.lef": "VERSION 5.8 ;\n",
        "flow/platforms/asap7/lef/cells.lef": "MACRO BUF\nEND BUF\n",
        "flow/platforms/asap7/setRC.tcl": "set_wire_rc -signal -resistance 1\n",
        "flow/platforms/asap7/config.mk": "export PLATFORM = asap7\n",
        "LICENSE_BUILD_RUN_SCRIPTS": "BSD 3-Clause License\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def policy_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform_id": "asap7",
        "orfs_component_id": "orfs",
        "orfs_commit": "a" * 40,
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
            "liberty_files": [
                "flow/platforms/asap7/lib/hold.lib",
                "flow/platforms/asap7/lib/hold_legacy.lib",
            ],
        },
        "reference_corner": None,
        "tech_lef": "flow/platforms/asap7/lef/tech.lef",
        "cell_lefs": ["flow/platforms/asap7/lef/cells.lef"],
        "rc_config": "flow/platforms/asap7/setRC.tcl",
        "flow_config": "flow/platforms/asap7/config.mk",
        "license_artifacts": ["LICENSE_BUILD_RUN_SCRIPTS"],
        "redistribution_status": "REVIEW_REQUIRED",
        "license_notes": [
            "The selected license file covers ORFS scripts, not the ASAP7 collateral."
        ],
        "deterministic_seed": 0,
    }


def selection_policy() -> PlatformSelectionPolicy:
    return PlatformSelectionPolicy.model_validate(policy_payload())


def fingerprint(tmp_path: Path) -> ToolFingerprint:
    executable = tmp_path / "yosys"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"yosys executable")
    return ToolFingerprint(
        tool_id="yosys",
        executable=str(executable.resolve()),
        version="Yosys 1.0",
        version_args=("-V",),
        executable_sha256=sha256_bytes(executable.read_bytes()),
        build_hash=sha256_bytes(b"build"),
        adapter_version="bootstrap-doctor-v1",
        container_digest=None,
    )


def request(tmp_path: Path, root: Path) -> PlatformLockRequest:
    from nova_rtl.platform.hydration import component_inventory, component_tree_identity

    return PlatformLockRequest(
        orfs_root=root.resolve(),
        policy=selection_policy(),
        source_manifest_hash=sha256_bytes(b"toolchain manifest"),
        verified_orfs_tree_identity=component_tree_identity(
            component_inventory(root, exclude_git_metadata=True)
        ),
        host=HostPlatform(os="linux", architecture="x86_64"),
        tool_fingerprints=(fingerprint(tmp_path),),
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_create_platform_lock_discovers_liberty_conditions_and_hashes_real_files(
    tmp_path: Path,
) -> None:
    root = fixture_orfs_root(tmp_path)

    lock = create_platform_lock(request(tmp_path, root))

    assert lock.setup_corner.operating_condition_mode == "PER_LIBRARY_NOMINAL"
    assert lock.setup_corner.operating_conditions == ("PVT_0P63V_100C",)
    assert lock.setup_corner.voltage_v == 0.63
    assert lock.setup_corner.temperature_c == 100.0
    assert lock.hold_corner.operating_condition_mode == "PER_LIBRARY_NOMINAL"
    assert lock.hold_corner.operating_conditions == ("FF_0.77_0", "PVT_0P77V_0C")
    assert lock.hold_corner.voltage_v == 0.77
    assert lock.hold_corner.temperature_c == 0.0
    assert lock.setup_corner.native_time_unit == "PS"
    assert lock.rc_rules.logical_path == "flow/platforms/asap7/setRC.tcl"
    assert lock.selection_policy_hash == (
        "sha256:d4d1cbf8c885388e79e4cdeabf5fe49ddfd77c0078b7ded32acdee2e5f569ec8"
    )
    assert lock.content_identity_hash == platform_content_identity_hash(lock)


def test_create_platform_lock_rejects_missing_hold_liberty(tmp_path: Path) -> None:
    root = fixture_orfs_root(tmp_path)
    (root / "flow/platforms/asap7/lib/hold.lib").unlink()

    with pytest.raises(PlatformLockError, match="required HOLD Liberty"):
        create_platform_lock(request(tmp_path, root))


def test_create_platform_lock_rejects_tree_changed_after_toolchain_verification(
    tmp_path: Path,
) -> None:
    root = fixture_orfs_root(tmp_path)
    lock_request = request(tmp_path, root).model_copy(
        update={"verified_orfs_tree_identity": sha256_bytes(b"prior verified tree")}
    )

    with pytest.raises(PlatformLockError, match="changed after toolchain verification"):
        create_platform_lock(lock_request)


def test_create_platform_lock_selects_declared_default_liberty_condition(
    tmp_path: Path,
) -> None:
    root = fixture_orfs_root(tmp_path)
    hold = root / "flow/platforms/asap7/lib/hold.lib"
    hold.write_text(
        '''library (fixture) {
  time_unit : "1ps";
  nom_voltage : 0.77;
  nom_temperature : 0;
  operating_conditions (FIRST) { voltage : 0.77; temperature : 0; }
  default_operating_conditions : ACTUAL_DEFAULT;
  operating_conditions (ACTUAL_DEFAULT) { voltage : 0.77; temperature : 0; }
}
''',
        encoding="utf-8",
    )

    lock = create_platform_lock(request(tmp_path, root))

    assert lock.hold_corner.operating_conditions[0] == "ACTUAL_DEFAULT"


def test_create_platform_lock_rejects_artifact_symlink_outside_orfs_root(
    tmp_path: Path,
) -> None:
    root = fixture_orfs_root(tmp_path)
    lock_request = request(tmp_path, root)
    outside = tmp_path / "outside.lef"
    outside.write_text("VERSION 5.8 ;\n", encoding="utf-8")
    tech_lef = root / "flow/platforms/asap7/lef/tech.lef"
    tech_lef.unlink()
    tech_lef.symlink_to(outside)

    with pytest.raises(PlatformLockError, match="unsafe archive link|escapes the ORFS root"):
        create_platform_lock(lock_request)


def test_load_platform_selection_policy_rejects_unknown_or_unsafe_paths(
    tmp_path: Path,
) -> None:
    payload = policy_payload()
    payload["tech_lef"] = "../outside.lef"
    payload["unexpected"] = True
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(PlatformLockError, match="invalid platform selection policy"):
        load_platform_selection_policy(path)


def test_platform_selection_policy_rejects_reused_corner_id() -> None:
    payload = policy_payload()
    assert isinstance(payload["hold_corner"], dict)
    payload["hold_corner"]["corner_id"] = "asap7_wc"

    with pytest.raises(ValueError, match="corner IDs must be distinct"):
        PlatformSelectionPolicy.model_validate(payload)


def test_create_platform_lock_rejects_two_paths_aliasing_one_liberty(tmp_path: Path) -> None:
    root = fixture_orfs_root(tmp_path)
    alias = root / "flow/platforms/asap7/lib/hold_alias.lib"
    alias.symlink_to("hold.lib")
    payload = policy_payload()
    assert isinstance(payload["hold_corner"], dict)
    payload["hold_corner"]["liberty_files"] = [
        "flow/platforms/asap7/lib/hold.lib",
        "flow/platforms/asap7/lib/hold_alias.lib",
    ]
    selected = PlatformSelectionPolicy.model_validate(payload)
    lock_request = request(tmp_path, root).model_copy(update={"policy": selected})

    with pytest.raises(PlatformLockError, match="resolve to distinct files"):
        create_platform_lock(lock_request)


def test_dump_analysis_views_binds_both_views_to_exact_lock_hashes(tmp_path: Path) -> None:
    root = fixture_orfs_root(tmp_path)
    lock = create_platform_lock(request(tmp_path, root))
    lock_path = tmp_path / "platform.lock.yaml"
    from nova_rtl.platform.lock import dump_platform_lock

    dump_platform_lock(lock, lock_path)
    views_path = tmp_path / "views.yaml"

    dump_analysis_views(lock, lock_path, views_path)
    payload = yaml.safe_load(views_path.read_text(encoding="utf-8"))

    assert payload["platform_lock_hash"] == sha256_bytes(lock_path.read_bytes())
    assert [view["check"] for view in payload["views"]] == ["SETUP", "HOLD"]
    assert payload["views"][0]["liberty_corner_id"] == "asap7_wc"
    assert payload["views"][1]["liberty_corner_id"] == "asap7_bc"
    assert payload["views"][0]["rc_artifact_hash"] == lock.rc_rules.sha256
    assert payload["views"][1]["rc_artifact_hash"] == lock.rc_rules.sha256


def test_publish_platform_outputs_rejects_one_destination_for_both_files(
    tmp_path: Path,
) -> None:
    root = fixture_orfs_root(tmp_path)
    lock = create_platform_lock(request(tmp_path, root))
    destination = tmp_path / "same.yaml"

    with pytest.raises(PlatformLockError, match="distinct destinations"):
        publish_platform_outputs(lock, destination, destination)

    assert not destination.exists()


def test_publish_platform_outputs_restores_prior_pair_when_second_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = fixture_orfs_root(tmp_path)
    lock = create_platform_lock(request(tmp_path, root))
    lock_path = tmp_path / "platform.lock.yaml"
    views_path = tmp_path / "views.yaml"
    lock_path.write_bytes(b"old lock\n")
    views_path.write_bytes(b"old views\n")
    real_replace = os.replace

    def fail_views_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == views_path:
            raise OSError("injected second publish failure")
        real_replace(source, destination)

    monkeypatch.setattr("nova_rtl.platform.lock.os.replace", fail_views_replace)

    with pytest.raises(PlatformLockError, match="could not be published atomically"):
        publish_platform_outputs(lock, lock_path, views_path)

    assert lock_path.read_bytes() == b"old lock\n"
    assert views_path.read_bytes() == b"old views\n"


def test_platform_content_identity_is_independent_of_local_realization_paths(
    tmp_path: Path,
) -> None:
    first_root = fixture_orfs_root(tmp_path / "first")
    second_root = tmp_path / "second/orfs"
    second_root.parent.mkdir(parents=True)
    shutil.copytree(first_root, second_root, symlinks=True)

    first = create_platform_lock(request(tmp_path / "first_tools", first_root))
    second = create_platform_lock(request(tmp_path / "second_tools", second_root))

    assert first.content_identity_hash == second.content_identity_hash


def test_platform_lock_rebinds_to_identical_verified_realization(tmp_path: Path) -> None:
    first_root = fixture_orfs_root(tmp_path / "first")
    second_root = tmp_path / "second/orfs"
    second_root.parent.mkdir(parents=True)
    shutil.copytree(first_root, second_root, symlinks=True)
    first_tools = tmp_path / "first_tools"
    second_tools = tmp_path / "second_tools"
    lock = create_platform_lock(request(first_tools, first_root))
    second_fingerprint = fingerprint(second_tools)

    result = verify_platform_lock(
        lock,
        artifact_root=second_root.resolve(),
        tool_paths={"yosys": Path(second_fingerprint.executable)},
    )

    assert result.status == "PASS"


def test_platform_lock_detects_change_anywhere_in_orfs_component_tree(tmp_path: Path) -> None:
    root = fixture_orfs_root(tmp_path)
    transitive_input = root / "flow/platforms/asap7/openRoad/tapcell.tcl"
    transitive_input.parent.mkdir(parents=True)
    transitive_input.write_text("tapcell v1\n", encoding="utf-8")
    lock = create_platform_lock(request(tmp_path, root))
    transitive_input.write_text("tapcell v2\n", encoding="utf-8")

    result = verify_platform_lock(lock)

    assert result.status == "FAIL"
    assert result.issues[0].code == "ORFS_TREE_IDENTITY_MISMATCH"
