from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.benchmark.calibrate import (
    CalibrationError,
    SelectedCalibrationValidation,
    build_calibration_report,
    calibrate_with_measurement,
    publish_calibration_report,
    run_full_calibration,
    validate_selected_calibration,
)
from nova_rtl.contracts.base import ArtifactRef, canonical_json_bytes
from nova_rtl.contracts.benchmark import (
    BenchmarkConfig,
    CalibrationReport,
    CalibrationSample,
    MappedCellTarget,
)
from nova_rtl.contracts.platform import ToolFingerprint


def _tool() -> ToolFingerprint:
    return ToolFingerprint(
        tool_id="yosys",
        executable=str(Path("/usr/bin/true").resolve()),
        version="Yosys 0.50",
        version_args=("-V",),
        executable_sha256=f"sha256:{'e' * 64}",
        build_hash=f"sha256:{'f' * 64}",
        adapter_version="yosys-adapter-v1",
    )


def _stage_result_artifact(digest: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"stage_result_{digest}",
        uri=f"artifact://sha256/{digest * 64}",
        sha256=f"sha256:{digest * 64}",
        media_type="application/json",
        size_bytes=1,
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        producer_stage_result_id=f"stage_calibration_{digest}",
        classification="INTERNAL",
    )


def _sample(scale: int, cells: int, digest: str) -> CalibrationSample:
    return CalibrationSample(
        workload_scale=scale,
        mapped_cell_count=cells,
        config_hash=f"sha256:{digest * 64}",
        source_hash=f"sha256:{digest * 64}",
        snapshot_hash=f"sha256:{digest * 64}",
        recipe_hash=f"sha256:{'c' * 64}",
        tool_fingerprint=_tool(),
        stage_result_artifact=_stage_result_artifact(digest),
    )


def test_calibration_selects_first_in_range_sample_and_binds_report_hash() -> None:
    report = build_calibration_report(
        MappedCellTarget(minimum=45_000, maximum=55_000),
        (
            _sample(32, 31_000, "a"),
            _sample(64, 49_500, "b"),
            _sample(96, 70_000, "c"),
        ),
    )

    assert report.status == "PASS"
    assert report.selected_workload_scale == 64
    assert report.report_hash.startswith("sha256:")


def test_calibration_contract_rejects_more_than_twelve_samples() -> None:
    samples = tuple(_sample(index + 1, 1_000 + index, "d") for index in range(13))
    payload = {
        "target": MappedCellTarget(minimum=45_000, maximum=55_000),
        "samples": samples,
        "selected_workload_scale": None,
        "status": "BUDGET_EXHAUSTED",
        "report_hash": f"sha256:{'0' * 64}",
    }

    with pytest.raises(ValidationError, match="twelve-sample budget"):
        CalibrationReport.model_validate(payload)


def test_calibration_search_changes_only_workload_scale_and_stops_in_range() -> None:
    base = BenchmarkConfig.default().model_copy(update={"workload_scale": 64})
    seen: list[BenchmarkConfig] = []

    def measure(config: BenchmarkConfig) -> CalibrationSample:
        seen.append(config)
        return _sample(config.workload_scale, config.workload_scale * 1_000, "a")

    report = calibrate_with_measurement(base, measure)

    assert report.status == "PASS"
    assert report.selected_workload_scale == 48
    assert tuple(config.workload_scale for config in seen) == (64, 32, 48)
    base_without_scale = base.model_dump(mode="json", exclude={"workload_scale"})
    assert all(
        config.model_dump(mode="json", exclude={"workload_scale"}) == base_without_scale
        for config in seen
    )


def test_calibration_search_fails_closed_after_twelve_unique_samples() -> None:
    base = BenchmarkConfig.default().model_copy(update={"workload_scale": 64})
    seen: list[int] = []

    def measure(config: BenchmarkConfig) -> CalibrationSample:
        seen.append(config.workload_scale)
        return _sample(config.workload_scale, 1_000, "b")

    with pytest.raises(CalibrationError, match="twelve-sample budget") as error:
        calibrate_with_measurement(base, measure)

    assert len(seen) == 12
    assert len(set(seen)) == 12
    assert error.value.report.status == "BUDGET_EXHAUSTED"


def test_calibration_rejects_measurement_with_wrong_scale_identity() -> None:
    base = BenchmarkConfig.default()

    with pytest.raises(CalibrationError, match="wrong workload scale"):
        calibrate_with_measurement(base, lambda _: _sample(1, 50_000, "d"))


def test_calibration_curve_is_published_as_resolvable_immutable_artifact(
    tmp_path: Path,
) -> None:
    report = build_calibration_report(
        MappedCellTarget(minimum=45_000, maximum=55_000),
        (_sample(48, 49_000, "a"),),
    )
    store = ArtifactStore(tmp_path / "artifacts")

    reference = publish_calibration_report(store, report)

    assert store.open_verified(reference).read() == canonical_json_bytes(report)
    assert store.blob_path(reference).stat().st_mode & 0o777 == 0o444


def test_full_calibration_validates_the_search_selected_scale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = BenchmarkConfig.default()
    report = build_calibration_report(
        base.mapped_cell_target,
        (
            _sample(4, 64_158, "a"),
            _sample(2, 44_508, "b"),
            _sample(3, 54_335, "c"),
        ),
    )
    evidence = _stage_result_artifact("e")
    validation = SelectedCalibrationValidation(
        status="PASS",
        timing_violation_family_ids=("family_0", "family_1"),
        evidence_artifact=evidence,
        evidence_hash=f"sha256:{'e' * 64}",
    )
    validated_configs: list[BenchmarkConfig] = []

    monkeypatch.setattr(
        "nova_rtl.benchmark.calibrate._verified_runtime",
        lambda _: (object(), object(), tmp_path, {}),
    )
    monkeypatch.setattr(
        "nova_rtl.benchmark.calibrate._locked_liberty_inputs",
        lambda *_: (),
    )
    monkeypatch.setattr(
        "nova_rtl.benchmark.calibrate.calibrate_with_measurement",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        "nova_rtl.benchmark.calibrate._publish_run_files",
        lambda *_: evidence,
    )

    def capture_validation(
        config: BenchmarkConfig,
        _output: Path,
    ) -> SelectedCalibrationValidation:
        validated_configs.append(config)
        return validation

    monkeypatch.setattr(
        "nova_rtl.benchmark.calibrate.validate_selected_calibration",
        capture_validation,
    )

    result = run_full_calibration(base, tmp_path / "calibration")

    assert result.validation == validation
    assert len(validated_configs) == 1
    selected = validated_configs[0]
    assert selected.workload_scale == 3
    assert selected.model_dump(exclude={"workload_scale"}) == base.model_dump(
        exclude={"workload_scale"}
    )


def test_selected_calibration_validation_rejects_wrong_scale_before_artifact_use(
    tmp_path: Path,
) -> None:
    base = BenchmarkConfig.default()
    report = build_calibration_report(
        base.mapped_cell_target,
        (_sample(3, 54_335, "c"),),
    )
    (tmp_path / "calibration-report.json").write_bytes(canonical_json_bytes(report))

    with pytest.raises(
        CalibrationError,
        match="configuration scale 4 does not match selected scale 3",
    ):
        validate_selected_calibration(base, tmp_path)
