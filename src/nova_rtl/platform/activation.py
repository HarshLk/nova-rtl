"""Offline-verifiable activation receipts for hydrated NOVA toolchains."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from nova_rtl.contracts.base import canonical_json_bytes
from nova_rtl.contracts.platform import (
    CanonicalEnvironmentEntry,
    ComponentInventoryEntry,
    InstalledComponentReceipt,
    ToolchainReceipt,
    ToolchainSourceManifest,
    ToolFingerprint,
)
from nova_rtl.platform.hydration import (
    component_receipt_bytes,
    manifest_content_identity_hash,
    tool_root_lock,
)
from nova_rtl.platform.probe import ToolProbeError, probe_executable

RECEIPT_NAME = "toolchain-receipt.json"
RECEIPT_SCHEMA_VERSION = 1


class ToolchainVerificationError(RuntimeError):
    """Raised when installed toolchain state does not match its reviewed manifest."""


@dataclass(frozen=True)
class VerifiedToolchain:
    """The only tool paths and environment a later consumer may use."""

    root: Path
    receipt_path: Path
    tool_paths: Mapping[str, Path]
    canonical_environment: Mapping[str, tuple[str, ...]]
    environment_operations: Mapping[str, str]
    literal_environment: Mapping[str, str]
    tool_fingerprints: Mapping[str, ToolFingerprint]

    def execution_environment(self) -> dict[str, str]:
        """Return a process environment rooted in this verified install."""

        return _execution_environment(
            self.canonical_environment, self.environment_operations, self.literal_environment
        )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ToolchainVerificationError(
            f"component file cannot be read: {path}: {error}"
        ) from error
    return f"sha256:{digest.hexdigest()}"


def _no_symlink_ancestors(path: Path) -> Path:
    absolute = path.absolute()
    for ancestor in (absolute, *absolute.parents):
        try:
            if ancestor.is_symlink():
                raise ToolchainVerificationError(f"toolchain path traverses symlink: {ancestor}")
        except OSError as error:
            raise ToolchainVerificationError(
                f"toolchain path cannot be inspected: {ancestor}: {error}"
            ) from error
    return absolute


def _tool_root(manifest: ToolchainSourceManifest, root: Path) -> Path:
    if root.name != manifest.tool_root_name:
        raise ToolchainVerificationError("tool root basename does not match manifest")
    checked = _no_symlink_ancestors(root)
    if not checked.is_dir():
        raise ToolchainVerificationError("tool root is missing or is not a directory")
    return checked


def _confined(root: Path, path: Path, label: str) -> Path:
    checked = _no_symlink_ancestors(path)
    try:
        checked.relative_to(root)
    except ValueError as error:
        raise ToolchainVerificationError(f"{label} escapes the tool root") from error
    return checked


def _safe_link_target(relative: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    if not target or "\\" in target or target_path.is_absolute():
        raise ToolchainVerificationError(f"unsafe symlink target: {target!r}")
    resolved = list(relative.parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ToolchainVerificationError(f"unsafe symlink target: {target!r}")
            resolved.pop()
        else:
            resolved.append(part)


def component_inventory(component: Path) -> tuple[ComponentInventoryEntry, ...]:
    """Return a complete, stable inventory of a confined component worktree."""

    if component.is_symlink() or not component.is_dir():
        raise ToolchainVerificationError(f"component is missing or unsafe: {component}")
    entries: list[ComponentInventoryEntry] = []

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ToolchainVerificationError(
                f"component directory cannot be read: {directory}: {error}"
            ) from error
        for child in children:
            relative = prefix / child.name
            if relative.parts[0] == ".git" or relative == PurePosixPath(
                ".nova-hydration-receipt.json"
            ):
                continue
            try:
                observed = child.lstat()
            except OSError as error:
                raise ToolchainVerificationError(
                    f"component path cannot be inspected: {child}: {error}"
                ) from error
            mode = stat.S_IMODE(observed.st_mode)
            if stat.S_ISDIR(observed.st_mode):
                entries.append(
                    ComponentInventoryEntry(path=relative.as_posix(), type="directory", mode=mode)
                )
                visit(child, relative)
            elif stat.S_ISREG(observed.st_mode):
                entries.append(
                    ComponentInventoryEntry(
                        path=relative.as_posix(),
                        type="file",
                        mode=mode,
                        sha256=_sha256_file(child),
                    )
                )
            elif stat.S_ISLNK(observed.st_mode):
                try:
                    target = os.readlink(child)
                except OSError as error:
                    raise ToolchainVerificationError(
                        f"symlink cannot be read: {child}: {error}"
                    ) from error
                _safe_link_target(relative, target)
                entries.append(
                    ComponentInventoryEntry(
                        path=relative.as_posix(), type="symlink", mode=mode, target=target
                    )
                )
            else:
                raise ToolchainVerificationError(f"unsupported component file type: {child}")

    visit(component, PurePosixPath("."))
    return tuple(entries)


def component_tree_identity(inventory: tuple[ComponentInventoryEntry, ...]) -> str:
    """Hash all relevant worktree paths, types, modes, and regular-file bytes."""

    return _sha256_bytes(_canonical_bytes([entry.model_dump(mode="json") for entry in inventory]))


def _runtime_environment(
    manifest: ToolchainSourceManifest, root: Path
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]:
    values: dict[str, list[str]] = {}
    operations: dict[str, str] = {}
    literals: dict[str, str] = {}
    for source in manifest.components:
        component = _confined(root, root / "components" / source.component_id, "component")
        for entry in source.runtime_environment:
            operation = operations.setdefault(entry.name, entry.operation)
            if operation != entry.operation:
                raise ToolchainVerificationError(
                    f"runtime variable {entry.name} mixes runtime environment operations"
                )
            if entry.operation == "SET_LITERAL":
                if entry.name in values or entry.name in literals or entry.literal_value is None:
                    raise ToolchainVerificationError(
                        f"runtime variable {entry.name} has an invalid literal contribution"
                    )
                literals[entry.name] = entry.literal_value
                continue
            if entry.name in literals:
                raise ToolchainVerificationError(
                    f"runtime variable {entry.name} mixes literal and path contributions"
                )
            values.setdefault(entry.name, [])
            for relative in entry.relative_paths:
                candidate = _confined(
                    component, component.joinpath(*PurePosixPath(relative).parts), "runtime path"
                )
                if not candidate.is_dir():
                    raise ToolchainVerificationError(f"runtime path is missing: {candidate}")
                values[entry.name].append(str(candidate))
    return ({name: tuple(paths) for name, paths in values.items()}, operations, literals)


def _execution_environment(
    canonical_environment: Mapping[str, tuple[str, ...]],
    operations: Mapping[str, str],
    literal_environment: Mapping[str, str],
) -> dict[str, str]:
    if set(canonical_environment) & set(literal_environment) or set(operations) != (
        set(canonical_environment) | set(literal_environment)
    ):
        raise ToolchainVerificationError("canonical environment values and operations do not match")
    environment = dict(os.environ)
    for name, operation in operations.items():
        if operation == "SET_LITERAL":
            environment[name] = literal_environment[name]
            continue
        paths = canonical_environment[name]
        joined = ":".join(paths)
        if operation == "SET":
            environment[name] = joined
        else:
            environment[name] = f"{joined}:{environment[name]}" if environment.get(name) else joined
    return environment


def render_shell_environment(
    canonical_environment: Mapping[str, tuple[str, ...]],
    operations: Mapping[str, str],
    literal_environment: Mapping[str, str] | None = None,
) -> str:
    """Render sourceable POSIX shell exports from reviewed rooted paths only."""

    literal_environment = literal_environment or {}
    if set(canonical_environment) & set(literal_environment) or set(operations) != (
        set(canonical_environment) | set(literal_environment)
    ):
        raise ToolchainVerificationError("environment values and operations do not match")
    lines: list[str] = []
    for name, operation in operations.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
            raise ToolchainVerificationError(f"invalid environment variable name: {name!r}")
        try:
            entry = CanonicalEnvironmentEntry(
                operation=operation,
                paths=canonical_environment.get(name, ()),
                literal_value=literal_environment.get(name),
            )
        except ValidationError as error:
            raise ToolchainVerificationError(
                f"invalid canonical environment value for {name}: {error}"
            ) from error
        values = (entry.literal_value,) if entry.operation == "SET_LITERAL" else entry.paths
        quoted = ":".join("'" + value.replace("'", "'\\\"'\\\"'") + "'" for value in values)
        if entry.operation in {"SET", "SET_LITERAL"}:
            lines.append(f"export {name}={quoted}")
        else:
            lines.append(f"export {name}={quoted}${{{name}:+:${name}}}")
    return "\n".join(lines) + ("\n" if lines else "")


def _component_state(
    manifest: ToolchainSourceManifest, root: Path
) -> tuple[tuple[InstalledComponentReceipt, ...], dict[str, Path]]:
    manifest_hash = manifest_content_identity_hash(manifest)
    components: list[InstalledComponentReceipt] = []
    tool_paths: dict[str, Path] = {}
    for source in manifest.components:
        component = _confined(root, root / "components" / source.component_id, "component")
        expected_hydration_receipt = component_receipt_bytes(manifest_hash, source)
        internal = _confined(
            component, component / ".nova-hydration-receipt.json", "internal receipt"
        )
        external = _confined(
            root, root / "receipts" / f"{source.component_id}.json", "component receipt"
        )
        try:
            if (
                internal.read_bytes() != expected_hydration_receipt
                or external.read_bytes() != expected_hydration_receipt
            ):
                raise ToolchainVerificationError(
                    f"component hydration receipt mismatch: {source.component_id}"
                )
        except OSError as error:
            raise ToolchainVerificationError(
                f"component hydration receipt missing: {source.component_id}"
            ) from error
        inventory = component_inventory(component)
        for executable in source.executables:
            path = _confined(
                component,
                component.joinpath(*PurePosixPath(executable.relative_path).parts),
                "executable",
            )
            if path.is_symlink() or not path.is_file():
                raise ToolchainVerificationError(
                    f"declared executable is missing or unsafe: {path}"
                )
            tool_paths[executable.tool_id] = path
        components.append(
            InstalledComponentReceipt(
                component_id=source.component_id,
                source=source,
                inventory=inventory,
                tree_identity=component_tree_identity(inventory),
            )
        )
    return tuple(components), tool_paths


def _verify_git_state(manifest: ToolchainSourceManifest, root: Path) -> None:
    for source in manifest.components:
        if source.source_kind != "GIT":
            continue
        component = _confined(root, root / "components" / source.component_id, "component")
        git_metadata = component / ".git"
        try:
            metadata_stat = git_metadata.lstat()
        except OSError as error:
            raise ToolchainVerificationError(
                f"Git metadata is missing or unreadable: {source.component_id}"
            ) from error
        if git_metadata.is_symlink() or not stat.S_ISDIR(metadata_stat.st_mode):
            raise ToolchainVerificationError(
                f"Git metadata must be a real directory: {source.component_id}"
            )
        _confined(component, git_metadata, "Git metadata")
        try:
            head = subprocess.run(
                ("git", "-C", str(component), "rev-parse", "HEAD"),
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            dirty = subprocess.run(
                ("git", "-C", str(component), "status", "--porcelain", "--untracked-files=no"),
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolchainVerificationError(
                f"Git state cannot be verified: {source.component_id}: {error}"
            ) from error
        if head.returncode != 0 or head.stdout.strip().lower() != source.git_commit:
            raise ToolchainVerificationError(f"Git HEAD mismatch: {source.component_id}")
        if dirty.returncode != 0 or dirty.stdout:
            raise ToolchainVerificationError(
                f"Git worktree has tracked changes: {source.component_id}"
            )


def _receipt_payload(
    manifest: ToolchainSourceManifest,
    components: tuple[InstalledComponentReceipt, ...],
    canonical_environment: Mapping[str, tuple[str, ...]],
    operations: Mapping[str, str],
    literal_environment: Mapping[str, str],
    fingerprints: Mapping[str, ToolFingerprint],
) -> ToolchainReceipt:
    return ToolchainReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        manifest_hash=manifest_content_identity_hash(manifest),
        manifest=manifest,
        components=components,
        environment={
            name: CanonicalEnvironmentEntry(
                operation=operation,
                paths=canonical_environment.get(name, ()),
                literal_value=literal_environment.get(name),
            )
            for name, operation in operations.items()
        },
        tool_fingerprints=tuple(fingerprints.values()),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_toolchain_receipt(manifest: ToolchainSourceManifest, root: Path) -> Path:
    """Probe all hydrated executables and atomically publish the global receipt."""

    checked_root = _tool_root(manifest, root)
    with tool_root_lock(checked_root):
        components, tool_paths = _component_state(manifest, checked_root)
        canonical_environment, operations, literal_environment = _runtime_environment(
            manifest, checked_root
        )
        execution_environment = _execution_environment(
            canonical_environment, operations, literal_environment
        )
        fingerprints: dict[str, ToolFingerprint] = {}
        for source in manifest.components:
            for executable in source.executables:
                try:
                    fingerprints[executable.tool_id] = probe_executable(
                        executable.tool_id,
                        tool_paths[executable.tool_id],
                        version_args=executable.version_args,
                        environment=execution_environment,
                    )
                except ToolProbeError as error:
                    raise ToolchainVerificationError(
                        f"executable probe failed for {executable.tool_id}: {error}"
                    ) from error
        _verify_git_state(manifest, checked_root)
        receipt = _confined(checked_root, checked_root / RECEIPT_NAME, "global receipt")
        _atomic_write(
            receipt,
            canonical_json_bytes(
                _receipt_payload(
                    manifest,
                    components,
                    canonical_environment,
                    operations,
                    literal_environment,
                    fingerprints,
                )
            )
            + b"\n",
        )
    return receipt


def verify_toolchain(manifest: ToolchainSourceManifest, root: Path) -> VerifiedToolchain:
    """Verify a completed toolchain receipt without network access."""

    checked_root = _tool_root(manifest, root)
    with tool_root_lock(checked_root):
        receipt_path = _confined(checked_root, checked_root / RECEIPT_NAME, "global receipt")
        try:
            receipt = ToolchainReceipt.model_validate_json(receipt_path.read_bytes())
        except (OSError, ValidationError) as error:
            raise ToolchainVerificationError(
                f"global toolchain receipt is invalid: {error}"
            ) from error
        if (
            receipt.manifest_hash != manifest_content_identity_hash(manifest)
            or receipt.manifest != manifest
        ):
            raise ToolchainVerificationError("global receipt manifest mismatch")
        components, tool_paths = _component_state(manifest, checked_root)
        if receipt.components != components:
            raise ToolchainVerificationError("component tree identity or inventory mismatch")
        canonical_environment, operations, literal_environment = _runtime_environment(
            manifest, checked_root
        )
        expected_environment = {
            name: CanonicalEnvironmentEntry(
                operation=operation,
                paths=canonical_environment.get(name, ()),
                literal_value=literal_environment.get(name),
            )
            for name, operation in operations.items()
        }
        if receipt.environment != expected_environment:
            raise ToolchainVerificationError("canonical environment mismatch")
        expected_ids = [
            item.tool_id for source in manifest.components for item in source.executables
        ]
        version_args_by_tool = {
            item.tool_id: item.version_args
            for source in manifest.components
            for item in source.executables
        }
        fingerprints: dict[str, ToolFingerprint] = {}
        execution_environment = _execution_environment(
            canonical_environment, operations, literal_environment
        )
        for recorded, tool_id in zip(receipt.tool_fingerprints, expected_ids, strict=True):
            expected_path = tool_paths[tool_id]
            if recorded.executable != str(expected_path):
                raise ToolchainVerificationError(
                    f"receipt executable escapes expected tool path: {tool_id}"
                )
            try:
                observed = probe_executable(
                    tool_id,
                    expected_path,
                    version_args=version_args_by_tool[tool_id],
                    environment=execution_environment,
                )
            except ToolProbeError as error:
                raise ToolchainVerificationError(
                    f"executable verification failed for {tool_id}: {error}"
                ) from error
            if recorded != observed:
                raise ToolchainVerificationError(f"executable fingerprint mismatch: {tool_id}")
            fingerprints[tool_id] = recorded
        _verify_git_state(manifest, checked_root)
    return VerifiedToolchain(
        checked_root,
        receipt_path,
        tool_paths,
        canonical_environment,
        operations,
        literal_environment,
        fingerprints,
    )


__all__ = [
    "RECEIPT_NAME",
    "ToolchainVerificationError",
    "VerifiedToolchain",
    "component_inventory",
    "component_tree_identity",
    "create_toolchain_receipt",
    "render_shell_environment",
    "verify_toolchain",
]
