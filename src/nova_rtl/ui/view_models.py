"""Pure projections from canonical report contracts into seven evaluator views."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.release import AuthorityClass, EvidenceClaim
from nova_rtl.reports.bundle import M9ReportBundle
from nova_rtl.reports.replay import verify_offline_replay

ViewId = Literal[
    "design_health",
    "critical_path_explorer",
    "ai_design_review",
    "candidate_tournament",
    "recovery_intelligence",
    "formal_proof",
    "results",
]
VIEW_ORDER: tuple[ViewId, ...] = (
    "design_health",
    "critical_path_explorer",
    "ai_design_review",
    "candidate_tournament",
    "recovery_intelligence",
    "formal_proof",
    "results",
)
AUTHORITY_COLORS: dict[AuthorityClass, str] = {
    "ADVISORY_AI": "purple",
    "DETERMINISTIC_POLICY": "green",
    "TRUTH_GATE": "red",
    "MEASURED_EDA": "blue",
    "HISTORICAL_EVIDENCE": "gray",
    "RECOVERY_DECISION": "amber",
}


class ViewCard(StrictContract):
    claim_id: EntityId
    title: NonEmptyString
    value: NonEmptyString
    authority: AuthorityClass
    color: Literal["purple", "green", "red", "blue", "gray", "amber"]
    evidence_ids: tuple[EntityId, ...]


class EvaluatorView(StrictContract):
    view_id: ViewId
    cards: tuple[ViewCard, ...]


class DashboardModel(StrictContract):
    schema_version: Literal[1] = 1
    run_id: EntityId
    selected_candidate_id: EntityId
    sequence: NonNegativeInt | None
    source_bundle_hash: HashRef
    views: tuple[EvaluatorView, ...]
    model_hash: HashRef

    @model_validator(mode="after")
    def view_set_and_hash_are_canonical(self) -> Self:
        if tuple(view.view_id for view in self.views) != VIEW_ORDER:
            raise ValueError("dashboard requires the canonical seven evaluator views")
        if self.model_hash != canonical_sha256(self, exclude=frozenset({"model_hash"})):
            raise ValueError("model_hash does not match canonical dashboard")
        return self


def _card(claim: EvidenceClaim) -> ViewCard:
    return ViewCard(
        claim_id=claim.claim_id,
        title=claim.label,
        value=claim.value,
        authority=claim.authority,
        color=AUTHORITY_COLORS[claim.authority],
        evidence_ids=claim.evidence_ids,
    )


def _visible(view_id: ViewId, claim: EvidenceClaim) -> bool:
    label = f"{claim.claim_id} {claim.label}".lower()
    if view_id in {"design_health", "results"}:
        return True
    if view_id == "critical_path_explorer":
        return any(token in label for token in ("path", "timing", "slack"))
    if view_id == "ai_design_review":
        return claim.authority == "ADVISORY_AI"
    if view_id == "candidate_tournament":
        return claim.authority in {"DETERMINISTIC_POLICY", "MEASURED_EDA"}
    if view_id == "recovery_intelligence":
        return claim.authority == "RECOVERY_DECISION"
    return claim.authority == "TRUTH_GATE"


def build_view_model(
    bundle: M9ReportBundle, *, sequence: int | None = None
) -> DashboardModel:
    ordered_claims = tuple(sorted(bundle.claims, key=lambda item: item.claim_id))
    views = tuple(
        EvaluatorView(
            view_id=view_id,
            cards=tuple(_card(claim) for claim in ordered_claims if _visible(view_id, claim)),
        )
        for view_id in VIEW_ORDER
    )
    payload = {
        "schema_version": 1,
        "run_id": bundle.run_id,
        "selected_candidate_id": bundle.selected_candidate_id,
        "sequence": sequence,
        "source_bundle_hash": bundle.bundle_hash,
        "views": tuple(item.model_dump(mode="json") for item in views),
    }
    return DashboardModel(**payload, model_hash=canonical_sha256(payload))


def build_view_model_from_replay(
    replay_directory: Path, *, sequence: int | None = None
) -> DashboardModel:
    root = replay_directory.resolve(strict=True)
    verify_offline_replay(root)
    bundle = M9ReportBundle.model_validate_json((root / "report-bundle.json").read_bytes())
    return build_view_model(bundle, sequence=sequence)


__all__ = [
    "AUTHORITY_COLORS",
    "DashboardModel",
    "VIEW_ORDER",
    "build_view_model",
    "build_view_model_from_replay",
]
