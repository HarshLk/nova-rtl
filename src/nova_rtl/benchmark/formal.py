"""Hashed formal CDC property and harness manifest generation."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import yaml

from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.benchmark import BenchmarkFormalManifest, FormalHarnessEntry

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_HARNESS_MANIFEST = _PROJECT_ROOT / "benchmark/formal/harnesses.yaml"


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def render_formal_assets(
    *,
    project_root: Path = _PROJECT_ROOT,
    harness_manifest: Path = _DEFAULT_HARNESS_MANIFEST,
) -> tuple[dict[str, bytes], BenchmarkFormalManifest]:
    """Load checked-in property sources and produce a self-hashed harness manifest."""

    raw = yaml.safe_load(harness_manifest.read_text(encoding="utf-8"))
    source_files = tuple(str(item) for item in raw["source_files"])
    source_assets = {
        path: (project_root / "benchmark" / path).read_bytes()
        for path in source_files
    }
    harnesses = tuple(FormalHarnessEntry.model_validate(item) for item in raw["harnesses"])
    property_ids = tuple(sorted({item for harness in harnesses for item in harness.property_ids}))
    source_identities = {
        path: _hash_bytes(content) for path, content in sorted(source_assets.items())
    }
    payload = {
        "schema_version": 1,
        "formal_model_id": raw["formal_model_id"],
        "source_files": source_files,
        "source_hash": canonical_sha256(source_identities),
        "property_ids": property_ids,
        "harnesses": harnesses,
    }
    hash_payload = {
        **payload,
        "harnesses": tuple(item.model_dump(mode="json") for item in harnesses),
    }
    manifest = BenchmarkFormalManifest(
        **payload,
        manifest_hash=canonical_sha256(hash_payload),
    )
    return {
        **source_assets,
        "formal/harnesses.json": canonical_json_bytes(manifest) + b"\n",
    }, manifest


def load_formal_manifest(path: Path) -> BenchmarkFormalManifest:
    """Load and validate one generated formal harness manifest."""

    return BenchmarkFormalManifest.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = ["load_formal_manifest", "render_formal_assets"]
