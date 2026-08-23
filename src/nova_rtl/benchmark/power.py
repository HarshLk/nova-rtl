"""Deterministic simulation workload and canonical VCD activity identities."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import yaml

from nova_rtl.contracts.analysis import PowerActivityContract
from nova_rtl.contracts.base import ArtifactRef, canonical_sha256
from nova_rtl.contracts.benchmark import BenchmarkPowerWorkload, PowerWorkloadPhase

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_WORKLOAD = _PROJECT_ROOT / "benchmark/sim/power_workload.yaml"


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def render_power_workload(
    path: Path = _DEFAULT_WORKLOAD,
) -> tuple[bytes, BenchmarkPowerWorkload]:
    """Render the checked-in workload recipe with a canonical self identity."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    phases = tuple(PowerWorkloadPhase.model_validate(item) for item in raw["phases"])
    payload = {
        "schema_version": raw["schema_version"],
        "workload_id": raw["workload_id"],
        "seed": raw["seed"],
        "phases": phases,
        "vcd_file": raw["vcd_file"],
        "vcd_scope": raw["vcd_scope"],
        "measurement_window_ns": tuple(raw["measurement_window_ns"]),
    }
    hash_payload = {
        **payload,
        "phases": tuple(item.model_dump(mode="json") for item in phases),
    }
    workload = BenchmarkPowerWorkload(
        **payload,
        workload_hash=canonical_sha256(hash_payload),
    )
    content = yaml.safe_dump(
        workload.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    return content, workload


def load_power_workload(path: Path) -> BenchmarkPowerWorkload:
    """Load and validate one generated power workload."""

    return BenchmarkPowerWorkload.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _canonicalize_vcd(content: bytes) -> bytes:
    """Remove simulator wall-clock metadata while preserving waveform semantics."""

    lines = content.decode("utf-8").splitlines()
    canonical: list[str] = []
    skipping_date = False
    for line in lines:
        stripped = line.strip()
        if stripped == "$date":
            canonical.extend(("$date", "  NOVA_DETERMINISTIC", "$end"))
            skipping_date = True
            continue
        if skipping_date:
            if stripped == "$end":
                skipping_date = False
            continue
        canonical.append(line.rstrip())
    return ("\n".join(canonical) + "\n").encode("utf-8")


def materialize_power_activity_contract(
    workload: BenchmarkPowerWorkload,
    vcd_path: Path,
) -> PowerActivityContract:
    """Bind a real canonicalized VCD to the fixed comparable activity contract."""

    canonical = _canonicalize_vcd(vcd_path.read_bytes())
    if vcd_path.read_bytes() != canonical:
        vcd_path.write_bytes(canonical)
    scope_tokens = workload.vcd_scope.split(".")
    scope_declarations = [f"$scope module {token} $end" for token in scope_tokens]
    vcd_text = canonical.decode("utf-8")
    if not all(declaration in vcd_text for declaration in scope_declarations):
        raise ValueError(f"VCD does not contain required scope {workload.vcd_scope}")
    artifact = ArtifactRef(
        artifact_id="benchmark_power_vcd",
        uri=f"artifact://benchmark/sim/{workload.vcd_file}",
        sha256=_hash_bytes(canonical),
        media_type="application/vnd.vcd",
        size_bytes=len(canonical),
        created_at=datetime(1970, 1, 1, tzinfo=UTC),
        producer_stage_result_id=None,
        classification="PUBLIC",
    )
    payload = {
        "schema_version": 1,
        "power_activity_contract_id": "nebula_comparable_vcd",
        "format": "VCD",
        "source_artifact": artifact,
        "scope": workload.vcd_scope,
        "time_window_ns": workload.measurement_window_ns,
        "propagation_policy": "ANNOTATED",
        "comparability": "OFFICIAL_COMPARABLE",
    }
    hash_payload = {
        **payload,
        "source_artifact": artifact.model_dump(mode="json"),
    }
    return PowerActivityContract(
        **payload,
        contract_hash=canonical_sha256(hash_payload),
    )


__all__ = [
    "load_power_workload",
    "materialize_power_activity_contract",
    "render_power_workload",
]
