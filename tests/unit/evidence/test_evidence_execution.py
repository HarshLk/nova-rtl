from __future__ import annotations

from types import SimpleNamespace

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.analysis import AnalysisViewContract
from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.manifest import ProjectManifest
from nova_rtl.evidence.execution import (
    EvidenceExecutionError,
    _critical_path_report_hash,
    _load_cached_evidence,
    normalize_evidence_stages,
    validate_m3_run_boundary,
)
from nova_rtl.evidence.graph import build_evidence_graph
from tests.unit.contracts.test_identity_contracts import analysis_view_payload
from tests.unit.contracts.test_manifest import valid_manifest_payload
from tests.unit.evidence.test_graph import graph_inputs


def test_evidence_stage_set_is_complete_and_order_independent() -> None:
    assert normalize_evidence_stages((" opportunities ", "EVIDENCE")) == (
        "evidence",
        "opportunities",
    )


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        (("evidence",), "missing: opportunities"),
        (("evidence", "opportunities", "yosys"), "unknown M3 stage aliases: yosys"),
        ((), "missing: evidence, opportunities"),
    ],
)
def test_evidence_stage_set_fails_closed(
    stages: tuple[str, ...], message: str
) -> None:
    with pytest.raises(EvidenceExecutionError, match=message):
        normalize_evidence_stages(stages)


def test_m3_analysis_rejects_project_design_boundary_mismatch() -> None:
    project = ProjectManifest.model_validate(valid_manifest_payload())
    views = (
        AnalysisViewContract.model_validate(analysis_view_payload("SETUP")),
        AnalysisViewContract.model_validate(analysis_view_payload("HOLD")),
    )
    project_hash = "sha256:" + "2" * 64
    contract_hash = "sha256:" + "3" * 64
    index = SimpleNamespace(
        run_id="run_001",
        design_contract_hash=contract_hash,
        project_manifest_artifact=SimpleNamespace(sha256=project_hash),
        platform_lock_hash=project.technology.platform_lock_hash,
        analysis_views=views,
    )
    design = SimpleNamespace(
        contract_hash=contract_hash,
        run_id="run_001",
        project_manifest_hash=project_hash,
        platform_lock_hash=project.technology.platform_lock_hash,
        protection_policy_hash=canonical_sha256(project.protection),
        analysis_view_ids=tuple(sorted(view.analysis_view_id for view in views)),
        analysis_view_set_hash=canonical_sha256(
            {"analysis_views": tuple(canonical_sha256(view) for view in views)}
        ),
    )

    validate_m3_run_boundary(index, project, design)  # type: ignore[arg-type]
    tampered = SimpleNamespace(
        **{
            **vars(design),
            "protection_policy_hash": "sha256:" + "4" * 64,
        }
    )
    with pytest.raises(EvidenceExecutionError, match="protection policy"):
        validate_m3_run_boundary(index, project, tampered)  # type: ignore[arg-type]


def test_critical_path_report_identity_is_order_independent_and_change_sensitive() -> None:
    setup = ("asap7_setup", "sha256:" + "1" * 64, "MAX", "sha256:" + "2" * 64)
    hold = ("asap7_hold", "sha256:" + "3" * 64, "MIN", "sha256:" + "4" * 64)

    first = _critical_path_report_hash((setup, hold))

    assert first == _critical_path_report_hash((hold, setup))
    assert first != _critical_path_report_hash(
        (setup, (*hold[:3], "sha256:" + "5" * 64))
    )


@pytest.mark.parametrize(
    "artifact_name",
    ("graph_artifact", "source_map_artifact", "path_record_artifact"),
)
def test_cached_evidence_fails_closed_on_corrupt_artifact(
    tmp_path, artifact_name: str
) -> None:  # type: ignore[no-untyped-def]
    store = ArtifactStore(tmp_path / "artifacts")
    inputs = graph_inputs()
    snapshot = build_evidence_graph(inputs, artifact_store=store)
    snapshot_ref = store.put_named_bytes(
        canonical_json_bytes(snapshot),
        artifact_id="stage_evidence_graph_snapshot",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id="stage_evidence_graph",
    )
    result = SimpleNamespace(
        raw_artifacts=(
            snapshot_ref,
            snapshot.graph_artifact,
            snapshot.source_map_artifact,
            snapshot.path_record_artifact,
        )
    )
    target = getattr(snapshot, artifact_name)
    blob = store.blob_path(target)
    blob.chmod(0o600)
    blob.write_bytes(b"corrupt")

    with pytest.raises(EvidenceExecutionError):
        _load_cached_evidence(store, result, inputs.input_identity.identity_hash)
