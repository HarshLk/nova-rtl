"""Build deterministic, claim-linked M9 evidence bundles."""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.release import EvidenceClaim

REPORT_SECTIONS = (
    ("executive_result", "Executive result and claim boundaries"),
    ("reproducibility", "Platform, tools, recipes and seeds"),
    ("benchmark", "Benchmark clock, CDC and cell-count validation"),
    ("baseline", "Baseline timing and physical evidence"),
    ("critical_paths", "Critical paths, clusters and source mapping"),
    ("ai_review", "Planner and council hypotheses and critiques"),
    ("candidate_tournament", "Candidate DAG and gate matrix"),
    ("recovery", "Failure and recovery lineage"),
    ("formal", "Strict proof and composition coverage"),
    ("invariants", "Binding, clock and CDC comparisons"),
    ("metrics", "Per-view timing, frequency, area and power"),
    ("pareto", "Strict Pareto frontier and secondary lanes"),
    ("ablations", "Ablation and resource usage"),
    ("delivered_rtl", "Selected RTL and patch hashes"),
    ("replay", "Replay instructions and artifact index"),
)


class ReportIntegrityError(RuntimeError):
    """A report claim, artifact, or self-identity cannot be verified."""


class ReportArtifact(StrictContract):
    artifact_id: EntityId
    relative_path: NonEmptyString
    sha256: HashRef
    size_bytes: NonNegativeInt

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_confined(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("report artifact path must be normalized and relative")
        return value


class ReportSection(StrictContract):
    section_id: EntityId
    title: NonEmptyString
    claim_ids: tuple[EntityId, ...]


class M9ReportBundle(StrictContract):
    schema_version: Literal[1] = 1
    report_bundle_id: EntityId
    run_id: EntityId
    selected_candidate_id: EntityId
    sections: tuple[ReportSection, ...] = Field(min_length=15, max_length=15)
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1)
    artifacts: tuple[ReportArtifact, ...] = Field(min_length=1)
    bundle_hash: HashRef

    @model_validator(mode="after")
    def bundle_is_closed_and_self_hashed(self) -> Self:
        if tuple(item.section_id for item in self.sections) != tuple(
            item[0] for item in REPORT_SECTIONS
        ):
            raise ValueError("report requires the canonical fifteen sections")
        claim_ids = tuple(item.claim_id for item in self.claims)
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("report claim IDs must be unique")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("report artifact IDs must be unique")
        available = set(artifact_ids)
        for claim in self.claims:
            if not set(claim.artifact_ids).issubset(available):
                raise ValueError("report claim references an unavailable artifact")
        if self.bundle_hash != canonical_sha256(self, exclude=frozenset({"bundle_hash"})):
            raise ValueError("bundle_hash does not match canonical M9 report bundle")
        return self


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _common_root(paths: tuple[Path, ...]) -> Path:
    common = Path(os.path.commonpath(tuple(str(path.resolve()) for path in paths)))
    return common.parent if common.is_file() else common


def build_report_bundle(
    *,
    run_id: str,
    selected_candidate_id: str,
    evidence_paths: dict[str, Path],
    claims: tuple[EvidenceClaim, ...],
    output_directory: Path,
) -> tuple[Path, M9ReportBundle]:
    required = {artifact_id for claim in claims for artifact_id in claim.artifact_ids}
    missing = sorted(required - set(evidence_paths))
    if missing:
        raise ReportIntegrityError(f"missing report artifact: {missing[0]}")
    resolved: dict[str, Path] = {}
    for artifact_id, supplied in sorted(evidence_paths.items()):
        try:
            path = supplied.resolve(strict=True)
        except OSError as error:
            raise ReportIntegrityError(f"missing report artifact: {artifact_id}") from error
        if not path.is_file():
            raise ReportIntegrityError(f"report artifact is not a file: {artifact_id}")
        resolved[artifact_id] = path
    root = _common_root(tuple(resolved.values()))
    artifacts = tuple(
        ReportArtifact(
            artifact_id=artifact_id,
            relative_path=path.relative_to(root).as_posix(),
            sha256=_file_hash(path),
            size_bytes=path.stat().st_size,
        )
        for artifact_id, path in sorted(resolved.items())
    )
    claim_ids = tuple(sorted(claim.claim_id for claim in claims))
    sections = tuple(
        ReportSection(
            section_id=section_id,
            title=title,
            claim_ids=claim_ids if section_id == "executive_result" else (),
        )
        for section_id, title in REPORT_SECTIONS
    )
    identity = canonical_sha256(
        {
            "run_id": run_id,
            "selected_candidate_id": selected_candidate_id,
            "claims": tuple(
                claim.model_dump(mode="json")
                for claim in sorted(claims, key=lambda item: item.claim_id)
            ),
            "artifacts": tuple(item.model_dump(mode="json") for item in artifacts),
        }
    )
    payload = {
        "schema_version": 1,
        "report_bundle_id": "report_bundle_" + identity.removeprefix("sha256:")[:24],
        "run_id": run_id,
        "selected_candidate_id": selected_candidate_id,
        "sections": tuple(item.model_dump(mode="json") for item in sections),
        "claims": tuple(
            item.model_dump(mode="json") for item in sorted(claims, key=lambda item: item.claim_id)
        ),
        "artifacts": tuple(item.model_dump(mode="json") for item in artifacts),
    }
    bundle = M9ReportBundle(**payload, bundle_hash=canonical_sha256(payload))
    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / "report-bundle.json"
    destination.write_bytes(canonical_json_bytes(bundle) + b"\n")
    return destination, bundle


def verify_report_bundle(path: Path, *, evidence_root: Path) -> M9ReportBundle:
    try:
        bundle = M9ReportBundle.model_validate_json(path.resolve(strict=True).read_bytes())
    except (OSError, ValueError) as error:
        raise ReportIntegrityError("report bundle is missing or invalid") from error
    root = evidence_root.resolve(strict=True)
    for artifact in bundle.artifacts:
        candidate = (root / artifact.relative_path).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ReportIntegrityError(f"report artifact is missing: {artifact.artifact_id}")
        if (
            candidate.stat().st_size != artifact.size_bytes
            or _file_hash(candidate) != artifact.sha256
        ):
            raise ReportIntegrityError(f"report artifact is corrupt: {artifact.artifact_id}")
    return bundle


__all__ = [
    "M9ReportBundle",
    "REPORT_SECTIONS",
    "ReportIntegrityError",
    "build_report_bundle",
    "verify_report_bundle",
]
