"""Seal and verify self-contained offline replay evidence."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.release import ReplayManifest
from nova_rtl.reports.bundle import (
    M9ReportBundle,
    ReportIntegrityError,
    verify_report_bundle,
)


class OfflineReplayError(RuntimeError):
    """The sealed replay is incomplete, corrupt, or internally inconsistent."""


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def seal_offline_replay(
    report_bundle_path: Path,
    *,
    evidence_root: Path,
    output_directory: Path,
    ledger_hash: str,
    event_artifact_ids: tuple[str, ...] = (),
) -> tuple[Path, ReplayManifest]:
    try:
        report = verify_report_bundle(report_bundle_path, evidence_root=evidence_root)
    except ReportIntegrityError as error:
        raise OfflineReplayError("cannot seal an invalid report bundle") from error
    destination = output_directory.resolve()
    artifacts_directory = destination / "artifacts"
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    root = evidence_root.resolve(strict=True)
    for artifact in report.artifacts:
        source = (root / artifact.relative_path).resolve(strict=True)
        (artifacts_directory / artifact.artifact_id).write_bytes(source.read_bytes())
    (destination / "report-bundle.json").write_bytes(
        canonical_json_bytes(report) + b"\n"
    )
    identity = canonical_sha256(
        {
            "report_bundle_hash": report.bundle_hash,
            "ledger_hash": ledger_hash,
            "event_artifact_ids": event_artifact_ids,
        }
    )
    payload = {
        "schema_version": 1,
        "replay_id": "replay_" + identity.removeprefix("sha256:")[:24],
        "run_id": report.run_id,
        "event_artifact_ids": tuple(sorted(event_artifact_ids)),
        "required_artifact_ids": tuple(
            item.artifact_id for item in report.artifacts
        ),
        "ledger_hash": ledger_hash,
        "terminal_state_hash": report.bundle_hash,
        "external_calls_allowed": False,
    }
    manifest = ReplayManifest(**payload, manifest_hash=canonical_sha256(payload))
    manifest_path = destination / "replay-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    return manifest_path, manifest


def verify_offline_replay(directory: Path) -> ReplayManifest:
    root = directory.resolve(strict=True)
    try:
        manifest = ReplayManifest.model_validate_json(
            (root / "replay-manifest.json").read_bytes()
        )
        report = M9ReportBundle.model_validate_json(
            (root / "report-bundle.json").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise OfflineReplayError("offline replay manifest or report is invalid") from error
    if manifest.run_id != report.run_id or manifest.terminal_state_hash != report.bundle_hash:
        raise OfflineReplayError("offline replay terminal identity differs from report")
    expected_ids = tuple(item.artifact_id for item in report.artifacts)
    if manifest.required_artifact_ids != expected_ids:
        raise OfflineReplayError("offline replay artifact inventory differs from report")
    by_id = {item.artifact_id: item for item in report.artifacts}
    for artifact_id in manifest.required_artifact_ids:
        path = root / "artifacts" / artifact_id
        try:
            content = path.read_bytes()
        except OSError as error:
            raise OfflineReplayError(f"missing replay artifact: {artifact_id}") from error
        artifact = by_id[artifact_id]
        if len(content) != artifact.size_bytes or _hash_bytes(content) != artifact.sha256:
            raise OfflineReplayError(f"corrupt replay artifact: {artifact_id}")
    return manifest


__all__ = ["OfflineReplayError", "seal_offline_replay", "verify_offline_replay"]
