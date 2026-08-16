"""Secure, deterministic acquisition of pinned portable toolchain components."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml
import zstandard as zstd

from nova_rtl.contracts.base import HashRef, canonical_json_bytes
from nova_rtl.contracts.platform import ArchiveMetadata, ToolchainSourceManifest, ToolSource


class HydrationError(RuntimeError):
    """Raised when a pinned component cannot be acquired safely."""


@dataclass(frozen=True)
class LoadedToolchainSourceManifest:
    """A strictly validated manifest paired with its canonical content identity."""

    manifest: ToolchainSourceManifest
    content_identity_hash: HashRef


@dataclass(frozen=True)
class HydratedComponent:
    """One published component and its deterministic receipt."""

    component_id: str
    path: Path
    receipt_path: Path
    reused: bool


@dataclass(frozen=True)
class HydrationResult:
    """Complete result of hydrating one reviewed source manifest."""

    manifest_hash: HashRef
    components: tuple[HydratedComponent, ...]


def manifest_content_identity_hash(manifest: ToolchainSourceManifest) -> HashRef:
    """Return the stable hash of strict manifest content, independent of YAML spelling."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()}"


def load_toolchain_source_manifest(path: Path) -> LoadedToolchainSourceManifest:
    """Load a JSON or YAML manifest and derive its canonical content identity."""

    suffix = path.suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise HydrationError(f"unsupported source-manifest format: {path.suffix or '<none>'}")
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
        manifest = ToolchainSourceManifest.model_validate(payload)
    except (OSError, UnicodeDecodeError, ValueError, TypeError, yaml.YAMLError) as error:
        raise HydrationError(f"invalid source manifest: {error}") from error
    return LoadedToolchainSourceManifest(manifest, manifest_content_identity_hash(manifest))


def _hash_file(path: Path) -> HashRef:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _validate_final_url(requested_url: str, final_url: str) -> None:
    requested = urlsplit(requested_url)
    final = urlsplit(final_url)
    if (
        final.scheme.lower() != "https"
        or final.hostname is None
        or final.username is not None
        or final.password is not None
        or final.hostname.lower() != (requested.hostname or "").lower()
        or final.port != requested.port
    ):
        raise HydrationError("final URL violates the source HTTPS host policy")


def _verify_archive(path: Path, source: ToolSource) -> None:
    assert source.archive is not None
    try:
        observed_size = path.stat().st_size
        observed_hash = _hash_file(path)
    except OSError as error:
        raise HydrationError(f"archive could not be read: {error}") from error
    if observed_size != source.archive.byte_size:
        raise HydrationError(
            f"archive size mismatch for {source.component_id}: "
            f"expected {source.archive.byte_size}, got {observed_size}"
        )
    if observed_hash != source.archive_sha256:
        raise HydrationError(f"archive SHA-256 mismatch for {source.component_id}")


def download_archive(
    source: ToolSource,
    cache_directory: Path,
    *,
    opener: Callable[[Request], Any] = urlopen,
) -> Path:
    """Download an immutable archive to a content-addressed cache with safe resume."""

    if source.source_kind != "ARCHIVE" or source.archive is None or source.archive_sha256 is None:
        raise HydrationError("download_archive requires an ARCHIVE source")
    cache_directory.mkdir(parents=True, exist_ok=True)
    digest = source.archive_sha256.removeprefix("sha256:")
    cached = cache_directory / f"{digest}.archive"
    partial = cache_directory / f".{digest}.part"
    if cached.exists():
        try:
            _verify_archive(cached, source)
        except HydrationError:
            cached.unlink(missing_ok=True)
        else:
            return cached

    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    request = Request(source.source_url, headers=headers)
    try:
        with opener(request) as response:
            _validate_final_url(source.source_url, response.geturl())
            status = getattr(response, "status", None)
            append = offset > 0 and status == 206
            if offset > 0 and status not in {200, 206}:
                raise HydrationError(f"unexpected HTTP status while resuming archive: {status}")
            with partial.open("ab" if append else "wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
                stream.flush()
                os.fsync(stream.fileno())
    except HydrationError:
        raise
    except OSError as error:
        raise HydrationError(f"archive download failed: {error}") from error
    _verify_archive(partial, source)
    os.replace(partial, cached)
    return cached


def _safe_member_path(name: str, strip_components: int) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise HydrationError(f"unsafe archive path: {name!r}")
    parts = tuple(part for part in path.parts if part != ".")
    if len(parts) <= strip_components:
        return None
    return PurePosixPath(*parts[strip_components:])


def _safe_link_target(link_path: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    if not target or "\\" in target or target_path.is_absolute():
        raise HydrationError(f"unsafe archive link: {target!r}")
    resolved = list(link_path.parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise HydrationError(f"unsafe archive link: {target!r}")
            resolved.pop()
        else:
            resolved.append(part)


def _extract_tar(archive: tarfile.TarFile, destination: Path, metadata: ArchiveMetadata) -> None:
    entries: list[tuple[tarfile.TarInfo, PurePosixPath | None]] = []
    for member in archive.getmembers():
        relative = _safe_member_path(member.name, metadata.strip_components)
        if relative is None:
            continue
        if not (member.isdir() or member.isfile() or member.issym()):
            raise HydrationError(f"unsupported archive entry: {member.name!r}")
        if member.issym():
            _safe_link_target(relative, member.linkname)
        entries.append((member, relative))

    destination.mkdir(parents=True, exist_ok=False)
    try:
        for member, relative in entries:
            assert relative is not None
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise HydrationError(f"archive file could not be read: {member.name!r}")
                with source, target.open("xb") as stream:
                    shutil.copyfileobj(source, stream, length=1024 * 1024)
                target.chmod(member.mode & 0o777)
        for member, relative in entries:
            if member.issym():
                assert relative is not None
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
    except (OSError, tarfile.TarError) as error:
        raise HydrationError(f"archive extraction failed: {error}") from error


def _deb_data_tar(archive_path: Path, temporary_directory: Path) -> Path:
    temporary_directory.mkdir(parents=True, exist_ok=True)
    with archive_path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise HydrationError("invalid Debian archive header")
        while header := stream.read(60):
            if len(header) != 60 or header[58:60] != b"`\n":
                raise HydrationError("invalid Debian archive member header")
            try:
                name = header[:16].decode("ascii").strip().rstrip("/")
                size = int(header[48:58].decode("ascii").strip())
            except (UnicodeDecodeError, ValueError) as error:
                raise HydrationError("invalid Debian archive member metadata") from error
            if size < 0:
                raise HydrationError("invalid Debian archive member size")
            if name.startswith("data.tar"):
                suffix = name.removeprefix("data.tar")
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=temporary_directory, prefix=".data-", suffix=suffix
                )
                temporary = Path(temporary_name)
                with os.fdopen(descriptor, "wb") as output:
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise HydrationError("truncated Debian archive member")
                        output.write(chunk)
                        remaining -= len(chunk)
                return temporary
            stream.seek(size + (size % 2), io.SEEK_CUR)
    raise HydrationError("Debian archive has no data.tar member")


def _decompress_zstd(compressed_path: Path, temporary_directory: Path) -> Path:
    """Stream a Debian zstd payload to a private tar file without invoking system tools."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=temporary_directory, prefix=".data-", suffix=".tar"
    )
    temporary = Path(temporary_name)
    try:
        with (
            os.fdopen(descriptor, "wb") as output,
            compressed_path.open("rb") as compressed,
            zstd.ZstdDecompressor().stream_reader(compressed) as reader,
        ):
            shutil.copyfileobj(reader, output, length=1024 * 1024)
        return temporary
    except (OSError, zstd.ZstdError):
        temporary.unlink(missing_ok=True)
        raise


def safe_extract_archive(
    archive_path: Path,
    destination: Path,
    metadata: ArchiveMetadata | None,
) -> None:
    """Extract only normalized regular files, directories, and confined symlinks."""

    if metadata is None:
        raise HydrationError("archive metadata is required for extraction")
    temporary_paths: list[Path] = []
    try:
        if metadata.archive_format == "DEB":
            data_archive = _deb_data_tar(archive_path, destination.parent)
            temporary_paths.append(data_archive)
            if data_archive.suffix == ".zst":
                tar_input: str | Path = _decompress_zstd(data_archive, destination.parent)
                temporary_paths.append(Path(tar_input))
            else:
                tar_input = data_archive
        else:
            tar_input = archive_path
        with tarfile.open(tar_input, mode="r:*") as archive:
            _extract_tar(archive, destination, metadata)
    except zstd.ZstdError as error:
        raise HydrationError(f"zstd Debian payload could not be decompressed: {error}") from error
    except (OSError, tarfile.TarError) as error:
        raise HydrationError(f"archive extraction failed: {error}") from error
    finally:
        for temporary_path in reversed(temporary_paths):
            temporary_path.unlink(missing_ok=True)


def _run_git(command: tuple[str, ...], run: Callable[..., Any]) -> Any:
    try:
        completed = run(command, capture_output=True, check=False, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise HydrationError(f"Git command failed to start: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise HydrationError(f"Git command failed: {' '.join(command[:2])}: {detail}")
    return completed


def checkout_git_source(
    source: ToolSource,
    destination: Path,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Clone and detach exactly at the manifest's full Git commit."""

    if source.source_kind != "GIT" or source.git_commit is None:
        raise HydrationError("checkout_git_source requires a GIT source")
    if destination.exists():
        raise HydrationError(f"Git destination already exists: {destination}")
    _run_git(
        (
            "git",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--no-checkout",
            "--",
            source.source_url,
            str(destination),
        ),
        run,
    )
    _run_git(("git", "-C", str(destination), "checkout", "--detach", source.git_commit), run)
    completed = _run_git(("git", "-C", str(destination), "rev-parse", "HEAD"), run)
    if completed.stdout.strip().lower() != source.git_commit:
        raise HydrationError(
            f"Git commit mismatch for {source.component_id}: expected {source.git_commit}"
        )


def _receipt_bytes(manifest_hash: HashRef, source: ToolSource) -> bytes:
    payload = {
        "archive_sha256": source.archive_sha256,
        "archive_size_bytes": source.archive.byte_size if source.archive else None,
        "component_id": source.component_id,
        "git_commit": source.git_commit,
        "manifest_hash": manifest_hash,
        "schema_version": 1,
        "source_kind": source.source_kind,
        "source_url": source.source_url,
        "version": source.version,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _host_identity() -> tuple[str, str]:
    machine = platform.machine().lower()
    return platform.system().lower(), {"amd64": "x86_64"}.get(machine, machine)


def hydrate_toolchain(
    manifest_or_loaded: ToolchainSourceManifest | LoadedToolchainSourceManifest,
    tool_root: Path,
    *,
    cache_directory: Path | None = None,
    opener: Callable[[Request], Any] = urlopen,
    git_run: Callable[..., Any] = subprocess.run,
    host_system: tuple[str, str] | None = None,
) -> HydrationResult:
    """Safely hydrate all manifest components and publish each only with a receipt."""

    loaded = (
        manifest_or_loaded
        if isinstance(manifest_or_loaded, LoadedToolchainSourceManifest)
        else LoadedToolchainSourceManifest(
            manifest_or_loaded, manifest_content_identity_hash(manifest_or_loaded)
        )
    )
    actual_host = host_system or _host_identity()
    expected_host = (loaded.manifest.host.os, loaded.manifest.host.architecture)
    if actual_host != expected_host:
        raise HydrationError(
            f"unsupported host: expected {expected_host[0]}/{expected_host[1]}, "
            f"got {actual_host[0]}/{actual_host[1]}"
        )

    root = tool_root.resolve()
    components_directory = root / "components"
    receipts_directory = root / "receipts"
    archive_cache = cache_directory.resolve() if cache_directory else root / "cache"
    results: list[HydratedComponent] = []
    for source in loaded.manifest.components:
        destination = components_directory / source.component_id
        receipt_path = receipts_directory / f"{source.component_id}.json"
        internal_receipt = destination / ".nova-hydration-receipt.json"
        receipt = _receipt_bytes(loaded.content_identity_hash, source)
        if destination.is_symlink():
            raise HydrationError(f"component destination must not be a symlink: {destination}")
        if (
            destination.is_dir()
            and internal_receipt.is_file()
            and internal_receipt.read_bytes() == receipt
        ):
            if not receipt_path.is_file() or receipt_path.read_bytes() != receipt:
                _atomic_write(receipt_path, receipt)
            results.append(
                HydratedComponent(source.component_id, destination, receipt_path, reused=True)
            )
            continue
        if destination.exists():
            raise HydrationError(
                f"unreceipted component exists and will not be activated: {destination}"
            )

        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=root, prefix=f".{source.component_id}.staging-"))
        staged_component = staging / source.component_id
        try:
            if source.source_kind == "ARCHIVE":
                archive = download_archive(source, archive_cache, opener=opener)
                safe_extract_archive(archive, staged_component, source.archive)
            else:
                checkout_git_source(source, staged_component, run=git_run)
            _atomic_write(staged_component / ".nova-hydration-receipt.json", receipt)
            components_directory.mkdir(parents=True, exist_ok=True)
            os.replace(staged_component, destination)
            _atomic_write(receipt_path, receipt)
        except Exception:
            if destination.exists() and not internal_receipt.exists():
                # The component was never validly published; leave no active partial install.
                shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        results.append(
            HydratedComponent(source.component_id, destination, receipt_path, reused=False)
        )
    return HydrationResult(loaded.content_identity_hash, tuple(results))


__all__ = [
    "HydratedComponent",
    "HydrationError",
    "HydrationResult",
    "LoadedToolchainSourceManifest",
    "checkout_git_source",
    "download_archive",
    "hydrate_toolchain",
    "load_toolchain_source_manifest",
    "manifest_content_identity_hash",
    "safe_extract_archive",
]
