"""Snapshot-bound strict EQY composition and raw proof preservation."""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.contracts.verification import FormalModelContract, ProofPartition, ProofResult


class FormalCompositionError(RuntimeError):
    """Formal sources, model, tool identities, or outputs fail closed."""


class FormalSourceIdentity(StrictContract):
    """One exact source file consumed by a strict proof side."""

    relative_path: str
    sha256: HashRef

    @field_validator("relative_path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
            or path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or value != path.as_posix()
        ):
            raise ValueError("formal source path must be normalized, relative, and shell-free")
        return value


class StrictEquivalencePlan(StrictContract):
    """Deterministic whole-design EQY recipe bound to exact source and model hashes."""

    schema_version: Literal[1] = 1
    top: NonEmptyString
    gold_sources: tuple[FormalSourceIdentity, ...] = Field(min_length=1)
    gate_sources: tuple[FormalSourceIdentity, ...] = Field(min_length=1)
    gold_snapshot_hash: HashRef
    gate_snapshot_hash: HashRef
    formal_model_contract_id: EntityId
    reset_assumption_hash: HashRef
    environment_assumption_hash: HashRef
    recipe: NonEmptyString
    recipe_hash: HashRef
    plan_hash: HashRef

    @model_validator(mode="after")
    def identities_are_canonical_and_self_hashed(self) -> Self:
        for label, identities in (
            ("gold", self.gold_sources),
            ("gate", self.gate_sources),
        ):
            paths = tuple(item.relative_path for item in identities)
            if paths != tuple(sorted(set(paths))):
                raise ValueError(f"{label} formal sources must be unique and ordered")
        if tuple(item.relative_path for item in self.gold_sources) != tuple(
            item.relative_path for item in self.gate_sources
        ):
            raise ValueError("gold and gate formal source path sets must match")
        if self.recipe_hash != _hash_bytes(self.recipe.encode("utf-8")):
            raise ValueError("formal recipe hash does not match recipe bytes")
        if self.plan_hash != canonical_sha256(self, exclude=frozenset({"plan_hash"})):
            raise ValueError("strict equivalence plan hash is not canonical")
        return self


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _source_identities(root: Path, source_paths: Sequence[str]) -> tuple[FormalSourceIdentity, ...]:
    resolved_root = root.resolve()
    identities: list[FormalSourceIdentity] = []
    if not source_paths or len(source_paths) != len(set(source_paths)):
        raise FormalCompositionError("formal source path set must be nonempty and unique")
    for relative_path in sorted(source_paths):
        try:
            FormalSourceIdentity(relative_path=relative_path, sha256="sha256:" + "0" * 64)
        except ValueError as error:
            raise FormalCompositionError(str(error)) from error
        path = (resolved_root / relative_path).resolve()
        if resolved_root not in path.parents or path.is_symlink() or not path.is_file():
            raise FormalCompositionError(f"formal source is missing or unsafe: {relative_path}")
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise FormalCompositionError(
                f"formal source is not readable UTF-8: {relative_path}"
            ) from error
        identities.append(
            FormalSourceIdentity(relative_path=relative_path, sha256=_hash_bytes(data))
        )
    return tuple(identities)


def source_snapshot_hash(root: Path, source_paths: Sequence[str]) -> str:
    """Return the canonical identity of an ordered formal RTL source set."""

    identities = _source_identities(root, source_paths)
    return canonical_sha256(
        {"files": tuple(item.model_dump(mode="json") for item in identities)}
    )


def _render_recipe(source_paths: Sequence[str], top: str) -> str:
    lines = ["[gold]"]
    lines.extend(f"read -sv gold/{path}" for path in source_paths)
    lines.extend((f"prep -top {top}", "[gate]"))
    lines.extend(f"read -sv gate/{path}" for path in source_paths)
    lines.extend(
        (
            f"prep -top {top}",
            "[strategy strict_sat]",
            "use sat",
            "depth 8",
            "",
        )
    )
    return "\n".join(lines)


def compose_strict_equivalence(
    *,
    gold_root: Path,
    gate_root: Path,
    source_paths: Sequence[str],
    top: str,
    formal_model: FormalModelContract,
) -> StrictEquivalencePlan:
    """Compose a deterministic EQY recipe only when formal identity has no delta."""

    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_$]*", top):
        raise FormalCompositionError("formal top is not a safe SystemVerilog identifier")
    if formal_model.behavioral_elaboration_delta != "NONE":
        raise FormalCompositionError("strict proof requires no formal elaboration delta")
    if formal_model.gate_snapshot_hash is None:
        raise FormalCompositionError("strict candidate proof requires a gate snapshot")
    gold_sources = _source_identities(gold_root, source_paths)
    gate_sources = _source_identities(gate_root, source_paths)
    gold_hash = canonical_sha256(
        {"files": tuple(item.model_dump(mode="json") for item in gold_sources)}
    )
    gate_hash = canonical_sha256(
        {"files": tuple(item.model_dump(mode="json") for item in gate_sources)}
    )
    if formal_model.functional_rtl_hash != gold_hash:
        raise FormalCompositionError("formal model functional RTL hash does not match gold source")
    if formal_model.gold_snapshot_hash != gold_hash:
        raise FormalCompositionError("formal model gold snapshot hash does not match gold source")
    if formal_model.gate_snapshot_hash != gate_hash:
        raise FormalCompositionError("formal model gate snapshot hash does not match gate source")
    ordered_paths = tuple(item.relative_path for item in gold_sources)
    recipe = _render_recipe(ordered_paths, top)
    payload = {
        "schema_version": 1,
        "top": top,
        "gold_sources": tuple(item.model_dump(mode="json") for item in gold_sources),
        "gate_sources": tuple(item.model_dump(mode="json") for item in gate_sources),
        "gold_snapshot_hash": gold_hash,
        "gate_snapshot_hash": gate_hash,
        "formal_model_contract_id": formal_model.formal_model_contract_id,
        "reset_assumption_hash": formal_model.reset_assumption_hash,
        "environment_assumption_hash": formal_model.environment_assumption_hash,
        "recipe": recipe,
        "recipe_hash": _hash_bytes(recipe.encode("utf-8")),
    }
    return StrictEquivalencePlan(**payload, plan_hash=canonical_sha256(payload))


def _verify_tool(fingerprint: ToolFingerprint, expected_id: str) -> Path:
    if fingerprint.tool_id != expected_id:
        raise FormalCompositionError(f"expected {expected_id} tool fingerprint")
    executable = Path(fingerprint.executable)
    try:
        actual = _hash_bytes(executable.read_bytes())
    except OSError as error:
        raise FormalCompositionError(f"cannot read {expected_id} executable: {error}") from error
    if actual != fingerprint.executable_sha256:
        raise FormalCompositionError(f"{expected_id} executable hash does not match fingerprint")
    return executable


def _copy_sources(
    source_root: Path,
    destination_root: Path,
    identities: Sequence[FormalSourceIdentity],
) -> None:
    for identity in identities:
        source = source_root.resolve() / identity.relative_path
        data = source.read_bytes()
        if _hash_bytes(data) != identity.sha256:
            raise FormalCompositionError(
                f"formal source changed after composition: {identity.relative_path}"
            )
        destination = destination_root / identity.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def _canonical_work_archive(workspace: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(workspace.rglob("*")):
            if path.is_symlink():
                raise FormalCompositionError("EQY work product contains an unsafe symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(workspace).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o444
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _proof_outcome(
    stdout: str, stderr: str, returncode: int, *, has_counterexample: bool
) -> str:
    combined = f"{stdout}\n{stderr}"
    if (
        ("[status] PASS" in combined or "DONE (PASS, rc=0)" in combined)
        and "Successfully proved designs equivalent" in combined
    ):
        return "PASS"
    if (
        ("[status] FAIL" in combined or "DONE (FAIL," in combined)
        and has_counterexample
    ):
        return "FAIL"
    if "[status] UNKNOWN" in combined or "inconclusive" in combined.lower():
        return "INCONCLUSIVE"
    return "INFRASTRUCTURE_ERROR"


def run_strict_equivalence(
    *,
    plan: StrictEquivalencePlan,
    gold_root: Path,
    gate_root: Path,
    workspace_root: Path,
    artifact_store: ArtifactStore,
    eqy_fingerprint: ToolFingerprint,
    yosys_fingerprint: ToolFingerprint,
    run_id: str,
    candidate_id: str,
    timeout_seconds: int = 120,
) -> ProofResult:
    """Execute the exact plan without a shell and preserve all raw EQY products."""

    eqy = _verify_tool(eqy_fingerprint, "eqy")
    yosys = _verify_tool(yosys_fingerprint, "yosys")
    if source_snapshot_hash(gold_root, tuple(x.relative_path for x in plan.gold_sources)) != (
        plan.gold_snapshot_hash
    ):
        raise FormalCompositionError("gold source changed after strict proof composition")
    if source_snapshot_hash(gate_root, tuple(x.relative_path for x in plan.gate_sources)) != (
        plan.gate_snapshot_hash
    ):
        raise FormalCompositionError("gate source changed after strict proof composition")
    workspace_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic_ns()
    with tempfile.TemporaryDirectory(prefix="eqy-", dir=workspace_root) as temporary:
        workspace = Path(temporary)
        _copy_sources(gold_root, workspace / "gold", plan.gold_sources)
        _copy_sources(gate_root, workspace / "gate", plan.gate_sources)
        recipe_path = workspace / "strict.eqy"
        recipe_path.write_text(plan.recipe, encoding="utf-8")
        environment = {
            "HOME": str(workspace / "home"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.pathsep.join(
                (str(eqy.parent), str(yosys.parent), os.environ.get("PATH", "/usr/bin:/bin"))
            ),
            "TZ": "UTC",
        }
        Path(environment["HOME"]).mkdir()
        argv = (str(eqy), "--yosys", str(yosys), "-f", recipe_path.name)
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as error:
            stdout = (
                error.stdout.decode()
                if isinstance(error.stdout, bytes)
                else (error.stdout or "")
            )
            stderr = (
                error.stderr.decode()
                if isinstance(error.stderr, bytes)
                else (error.stderr or "")
            )
            stderr += "\nNOVA strict equivalence timeout"
            returncode = 124
        counterexample_paths = tuple(sorted(workspace.rglob("trace.vcd")))
        counterexample_data = (
            counterexample_paths[0].read_bytes() if counterexample_paths else None
        )
        archive = _canonical_work_archive(workspace)
    runtime_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    outcome = _proof_outcome(
        stdout,
        stderr,
        returncode,
        has_counterexample=counterexample_data is not None,
    )
    prefix = "proof_" + plan.plan_hash.removeprefix("sha256:")[:16]
    stdout_ref = artifact_store.put_named_bytes(
        stdout.encode("utf-8"),
        artifact_id=f"{prefix}_stdout",
        media_type="text/plain",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    stderr_ref = artifact_store.put_named_bytes(
        stderr.encode("utf-8"),
        artifact_id=f"{prefix}_stderr",
        media_type="text/plain",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    recipe_ref = artifact_store.put_named_bytes(
        plan.recipe.encode("utf-8"),
        artifact_id=f"{prefix}_recipe",
        media_type="text/plain",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    work_ref = artifact_store.put_named_bytes(
        archive,
        artifact_id=f"{prefix}_work_products",
        media_type="application/x-tar",
        classification="RESTRICTED_RTL",
        producer_stage_result_id=None,
    )
    counterexample_ref = (
        artifact_store.put_named_bytes(
            counterexample_data,
            artifact_id=f"{prefix}_counterexample",
            media_type="application/vnd.nova-rtl.vcd",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=None,
        )
        if outcome == "FAIL" and counterexample_data is not None
        else None
    )
    raw_artifacts = (
        (recipe_ref, stderr_ref, stdout_ref, work_ref, counterexample_ref)
        if counterexample_ref is not None
        else (recipe_ref, stderr_ref, stdout_ref, work_ref)
    )
    partition = ProofPartition(
        partition_id=f"partition_{candidate_id}",
        status=outcome,
        runtime_ms=runtime_ms,
        strategy="EQY_STRICT_SAT",
        artifact_refs=(work_ref, counterexample_ref)
        if counterexample_ref is not None
        else (work_ref,),
    )
    return ProofResult(
        proof_result_id=f"proof_{candidate_id}",
        run_id=run_id,
        candidate_id=candidate_id,
        contract="STRICT_SEQ_EQUIV",
        outcome=outcome,
        formal_model_contract_id=plan.formal_model_contract_id,
        gold_hash=plan.gold_snapshot_hash,
        gate_hash=plan.gate_snapshot_hash,
        proof_scope="WHOLE_DESIGN",
        partitions=(partition,),
        composition_manifest_artifact_id=None,
        assumption_hashes=tuple(
            sorted({plan.reset_assumption_hash, plan.environment_assumption_hash})
        ),
        counterexample_artifact_id=(
            counterexample_ref.artifact_id if counterexample_ref is not None else None
        ),
        raw_artifacts=raw_artifacts,
        runtime_ms=runtime_ms,
    )


__all__ = [
    "FormalCompositionError",
    "FormalSourceIdentity",
    "StrictEquivalencePlan",
    "compose_strict_equivalence",
    "run_strict_equivalence",
    "source_snapshot_hash",
]
