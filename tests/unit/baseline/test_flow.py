from __future__ import annotations

from pathlib import Path

import yaml

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_run
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.execution import _openroad_recipe, _resumable_stage_ids, _sby_recipe
from nova_rtl.baseline.flow import initialize_run, load_run_index
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.contracts.platform import PlatformLock

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"


def _locked_platform() -> tuple[PlatformLock, Path]:
    lock = PlatformLock.model_validate(
        yaml.safe_load((PROJECT_ROOT / "config/platform/platform.lock.yaml").read_text())
    )
    return lock, PROJECT_ROOT / ".nova-tools/components/orfs"


def test_resume_manifest_excludes_historical_stages_without_current_hashes() -> None:
    assert _resumable_stage_ids(
        {"stage_a": object(), "stage_future": object()},
        {"stage_a": object()},
    ) == ("stage_a",)


def test_cdc_property_recipe_is_bounded_multiclock_and_uses_staged_sources() -> None:
    recipe = _sby_recipe("input_rtl_bundle", "input_formal_property_source")

    assert "mode bmc" in recipe
    assert "depth 16" in recipe
    assert "multiclock on" in recipe
    assert "prep -top nova_cdc_protocol_harness" in recipe
    assert "rtl_bundle.sv inputs/input_rtl_bundle" in recipe
    assert "cdc_protocol_properties.sv inputs/input_formal_property_source" in recipe


def test_hold_physical_analysis_reuses_the_setup_placed_checkpoint() -> None:
    lock, orfs_root = _locked_platform()

    setup = _openroad_recipe(
        lock,
        orfs_root,
        corner_name="SETUP",
        netlist_id="mapped_netlist",
        constraint_id="setup_sdc",
        register_count=42,
        deterministic_seed=20260808,
        checkpoint_id=None,
    )
    hold = _openroad_recipe(
        lock,
        orfs_root,
        corner_name="HOLD",
        netlist_id="mapped_netlist",
        constraint_id="hold_sdc",
        register_count=42,
        deterministic_seed=20260808,
        checkpoint_id="setup_placed_odb",
    )

    assert "global_placement" in setup
    assert "read_db inputs/setup_placed_odb" in hold
    assert "global_placement" not in hold
    assert "read_verilog inputs/mapped_netlist" not in hold
    assert 'puts "NOVA_DETERMINISTIC_SEED 20260808"' in setup
    assert 'puts "NOVA_DETERMINISTIC_SEED 20260808"' in hold


def test_init_snapshots_a_deterministic_project_into_m1_authorities(tmp_path: Path) -> None:
    project_root = tmp_path / "tiny"
    snapshot = generate_benchmark(
        load_benchmark_config(PROFILE_MANIFEST, "tiny"),
        project_root,
    )

    first = initialize_run(project_root / "project.yaml", runs_root=tmp_path / "runs")
    second = initialize_run(project_root / "project.yaml", runs_root=tmp_path / "runs")

    assert first.run_directory == second.run_directory
    assert (tmp_path / "runs/latest").resolve() == first.run_directory
    index = load_run_index(first.run_directory)
    assert index.profile == "tiny"
    assert index.benchmark_snapshot_hash == snapshot.snapshot_hash
    assert index.expected_master_clocks == 5
    assert index.expected_generated_clocks == 10
    store = ArtifactStore.open_existing(first.run_directory / "artifacts")
    store.open_verified(index.project_manifest_artifact).close()
    store.open_verified(index.design_contract_artifact).close()
    assert index.formal_property_source_artifact is not None
    assert (
        store.open_verified(index.formal_property_source_artifact).read()
        == (project_root / "formal/cdc_protocol_properties.sv").read_bytes()
    )
    ledger = ExperimentLedger.open_existing(
        first.run_directory / "experiment-ledger.sqlite3",
        artifact_store=store,
    )
    assert len(replay_run(ledger, index.run_id)) == 1
