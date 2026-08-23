from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.benchmark.validate import validate_benchmark
from nova_rtl.contracts.benchmark import BenchmarkConfig

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _config_payload(config: BenchmarkConfig) -> dict[str, object]:
    return config.model_dump(mode="python")


def test_full_profile_has_required_clock_topology_and_deterministic_snapshot(
    tmp_path: Path,
) -> None:
    config = load_benchmark_config(PROFILE_MANIFEST, "full")

    first = generate_benchmark(config, tmp_path / "first")
    second = generate_benchmark(config, tmp_path / "second")

    assert first.expected_master_clocks == 5
    assert config.workload_scale == 4
    assert first.expected_generated_per_master == 21
    assert first.expected_generated_total == 105
    assert first.config_hash == second.config_hash
    assert first.template_hash == second.template_hash
    assert first.source_hash == second.source_hash
    assert first.snapshot_hash == second.snapshot_hash
    assert _tree_bytes(tmp_path / "first") == _tree_bytes(tmp_path / "second")


def test_tiny_profile_changes_configuration_without_changing_template_semantics(
    tmp_path: Path,
) -> None:
    tiny = generate_benchmark(
        load_benchmark_config(PROFILE_MANIFEST, "tiny"),
        tmp_path / "tiny",
    )
    full = generate_benchmark(
        load_benchmark_config(PROFILE_MANIFEST, "full"),
        tmp_path / "full",
    )

    assert tiny.expected_master_clocks == full.expected_master_clocks == 5
    assert tiny.expected_generated_per_master == 2
    assert tiny.expected_generated_total == 10
    assert full.expected_generated_per_master == 21
    assert full.expected_generated_total == 105
    assert tiny.template_hash == full.template_hash
    assert tiny.config_hash != full.config_hash
    assert tiny.source_hash != full.source_hash
    assert (tmp_path / "tiny/rtl/nebula_top.sv").read_bytes() == (
        tmp_path / "full/rtl/nebula_top.sv"
    ).read_bytes()
    assert (tmp_path / "tiny/rtl/benchmark_parameters.svh").read_bytes() != (
        tmp_path / "full/rtl/benchmark_parameters.svh"
    ).read_bytes()


def test_invalid_divider_ratios_are_rejected() -> None:
    payload = _config_payload(BenchmarkConfig.default())
    payload["generated_clocks_per_master"] = 2
    payload["divider_ratios"] = (2, 2)

    with pytest.raises(ValidationError, match="divider ratios must be unique"):
        BenchmarkConfig.model_validate(payload)

    payload["divider_ratios"] = (1, 2)
    with pytest.raises(ValidationError, match="divider ratios must be greater than one"):
        BenchmarkConfig.model_validate(payload)


def test_duplicate_master_or_clock_identities_are_rejected() -> None:
    config = BenchmarkConfig.default()
    payload = _config_payload(config)
    payload["master_domains"] = (
        config.master_domains[0],
        config.master_domains[0],
        *config.master_domains[2:],
    )

    with pytest.raises(ValidationError, match="master domain identities must be unique"):
        BenchmarkConfig.model_validate(payload)

    duplicate_clock = config.master_domains[1].model_copy(
        update={"master_clock_id": config.master_domains[0].master_clock_id}
    )
    payload["master_domains"] = (
        config.master_domains[0],
        duplicate_clock,
        *config.master_domains[2:],
    )
    with pytest.raises(ValidationError, match="master clock identities must be unique"):
        BenchmarkConfig.model_validate(payload)


def test_inconsistent_generated_clock_count_is_rejected() -> None:
    payload = _config_payload(BenchmarkConfig.default())
    payload["generated_clocks_per_master"] = 20

    with pytest.raises(ValidationError, match="must equal the divider ratio count"):
        BenchmarkConfig.model_validate(payload)


def test_generated_clock_expectations_have_active_consumers(tmp_path: Path) -> None:
    snapshot = generate_benchmark(BenchmarkConfig.default(), tmp_path / "full")

    result = validate_benchmark(snapshot, snapshot.expectations)

    assert result.status == "PASS"
    assert result.generated_without_consumers == ()
    assert len(snapshot.expectations.generated_clocks) == 105
    assert all(item.active_consumers for item in snapshot.expectations.generated_clocks)
    assert snapshot.expectations.generated_clocks[0].active_consumers == (
        "u_ingress.u_domain.gen_consumers[0].u_consumer",
    )
    assert snapshot.expectations.generated_clocks[-1].active_consumers == (
        "u_control.u_domain.gen_consumers[20].u_consumer",
    )


def test_generation_rejects_an_existing_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()

    with pytest.raises(FileExistsError, match="output path already exists"):
        generate_benchmark(BenchmarkConfig.default(), output)
