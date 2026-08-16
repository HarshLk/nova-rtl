"""Secure, deterministic acquisition of pinned portable toolchain components."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import lzma
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml
import zstandard as zstd

from nova_rtl.contracts.base import HashRef, canonical_json_bytes
from nova_rtl.contracts.platform import ArchiveMetadata, ToolchainSourceManifest, ToolSource

HTTP_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 300
MAX_REDIRECTS = 5
LOCK_TIMEOUT_SECONDS = 30


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_DEFAULT_OPENER = build_opener(_NoRedirect()).open


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


def _validate_redirect_url(url: str, allowed_hosts: set[str]) -> None:
    final = urlsplit(url)
    if (
        final.scheme.lower() != "https"
        or final.hostname is None
        or final.username is not None
        or final.password is not None
        or final.hostname.lower() not in allowed_hosts
    ):
        raise HydrationError("redirect host violates the manifest allowlist")


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


def _call_opener(opener: Callable[..., Any], request: Request) -> Any:
    try:
        return opener(request, timeout=HTTP_TIMEOUT_SECONDS)
    except HTTPError as error:
        return error
    except TypeError:
        try:
            return opener(request)
        except HTTPError as error:
            return error


def _copy_response_bounded(
    source: Any,
    destination: Any,
    limit: int,
    initial_size: int,
    limit_message: str = "archive response exceeds expected size",
) -> int:
    written = initial_size
    while True:
        remaining = limit - written
        chunk = source.read(min(1024 * 1024, remaining + 1))
        if not chunk:
            return written
        if len(chunk) > remaining:
            raise HydrationError(limit_message)
        destination.write(chunk)
        written += len(chunk)


def download_archive(
    source: ToolSource,
    cache_directory: Path,
    *,
    opener: Callable[..., Any] = _DEFAULT_OPENER,
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
    if offset > source.archive.byte_size:
        partial.unlink(missing_ok=True)
        offset = 0
    descriptor, temporary_name = tempfile.mkstemp(
        dir=cache_directory, prefix=f".{digest}.transfer-", suffix=".part"
    )
    transfer = Path(temporary_name)
    invalid = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if offset:
                with partial.open("rb") as previous:
                    shutil.copyfileobj(previous, stream, length=1024 * 1024)
            current_url = source.source_url
            source_host = urlsplit(source.source_url).hostname
            assert source_host is not None
            allowed_hosts = {source_host.lower(), *source.allowed_redirect_hosts}
            for redirect_count in range(MAX_REDIRECTS + 1):
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                response = _call_opener(opener, Request(current_url, headers=headers))
                try:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    response_url = response.geturl()
                    if response_url != current_url:
                        raise HydrationError("redirect handling must remain explicit")
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_count == MAX_REDIRECTS:
                            raise HydrationError("redirect limit or Location policy violation")
                        current_url = urljoin(current_url, location)
                        _validate_redirect_url(current_url, allowed_hosts)
                        continue
                    if status not in {200, 206}:
                        raise HydrationError(
                            f"unexpected HTTP status while downloading archive: {status}"
                        )
                    if offset and status == 200:
                        stream.seek(0)
                        stream.truncate()
                        offset = 0
                    _copy_response_bounded(response, stream, source.archive.byte_size, offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                finally:
                    response.close()
            else:
                raise HydrationError("redirect limit exceeded")
        _verify_archive(transfer, source)
        os.replace(transfer, cached)
        partial.unlink(missing_ok=True)
        return cached
    except HydrationError as error:
        invalid = "exceeds expected size" in str(error) or (
            transfer.exists() and transfer.stat().st_size > source.archive.byte_size
        )
        if invalid:
            partial.unlink(missing_ok=True)
        raise
    except OSError as error:
        raise HydrationError(f"archive download failed: {error}") from error
    finally:
        if transfer.exists():
            if not invalid and transfer.stat().st_size <= source.archive.byte_size:
                os.replace(transfer, partial)
            else:
                transfer.unlink(missing_ok=True)


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
    regular_file_bytes = 0
    member_count = 0
    for member in archive.getmembers():
        member_count += 1
        if member_count > metadata.max_entries:
            raise HydrationError("archive entry-count budget exceeded")
        relative = _safe_member_path(member.name, metadata.strip_components)
        if relative is None:
            continue
        if not (member.isdir() or member.isfile() or member.issym()):
            raise HydrationError(f"unsupported archive entry: {member.name!r}")
        if member.issym():
            _safe_link_target(relative, member.linkname)
        if member.isfile():
            regular_file_bytes += member.size
            if regular_file_bytes > metadata.max_regular_file_bytes:
                raise HydrationError("archive regular-file byte budget exceeded")
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


def _decompress_to_tar(
    compressed_path: Path,
    temporary_directory: Path,
    compression: str,
    maximum_bytes: int,
) -> Path:
    """Bounded-stream a declared compression format to a private tar file."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=temporary_directory, prefix=".data-", suffix=".tar"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, compressed_path.open("rb") as compressed:
            if compression == "GZ":
                reader = gzip.GzipFile(fileobj=compressed)
            elif compression == "XZ":
                reader = lzma.LZMAFile(compressed)  # noqa: SIM115 - closed through closing(reader)
            elif compression == "ZST":
                reader = zstd.ZstdDecompressor().stream_reader(compressed)
            else:
                raise HydrationError(f"unsupported declared archive compression: {compression}")
            with closing(reader):
                _copy_response_bounded(
                    reader,
                    output,
                    maximum_bytes,
                    0,
                    "archive decompressed byte budget exceeded",
                )
        return temporary
    except (HydrationError, OSError, lzma.LZMAError, zstd.ZstdError):
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
            deb_compressions = {".gz": "GZ", ".xz": "XZ", ".zst": "ZST"}
            compression = deb_compressions.get(data_archive.suffix)
            if compression is None:
                raise HydrationError("unsupported Debian data archive compression")
            tar_input: str | Path = _decompress_to_tar(
                data_archive, destination.parent, compression, metadata.max_decompressed_bytes
            )
            temporary_paths.append(Path(tar_input))
        elif metadata.archive_format == "TAR_GZ":
            tar_input = _decompress_to_tar(
                archive_path, destination.parent, "GZ", metadata.max_decompressed_bytes
            )
            temporary_paths.append(Path(tar_input))
        elif metadata.archive_format == "TAR_XZ":
            tar_input = _decompress_to_tar(
                archive_path, destination.parent, "XZ", metadata.max_decompressed_bytes
            )
            temporary_paths.append(Path(tar_input))
        else:
            raise HydrationError(f"unsupported archive format: {metadata.archive_format}")
        with tarfile.open(tar_input, mode="r:") as archive:
            _extract_tar(archive, destination, metadata)
    except zstd.ZstdError as error:
        raise HydrationError(f"zstd Debian payload could not be decompressed: {error}") from error
    except (OSError, tarfile.TarError) as error:
        raise HydrationError(
            f"declared archive format mismatch or extraction failed: {error}"
        ) from error
    finally:
        for temporary_path in reversed(temporary_paths):
            temporary_path.unlink(missing_ok=True)


def _run_git(command: tuple[str, ...], run: Callable[..., Any], timeout_seconds: int) -> Any:
    try:
        completed = run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as error:
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
    timeout_seconds: int = GIT_TIMEOUT_SECONDS,
) -> None:
    """Clone and detach exactly at the manifest's full Git commit."""

    if source.source_kind != "GIT" or source.git_commit is None:
        raise HydrationError("checkout_git_source requires a GIT source")
    if destination.exists():
        raise HydrationError(f"Git destination already exists: {destination}")
    if timeout_seconds <= 0:
        raise HydrationError("Git timeout must be positive")
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
        timeout_seconds,
    )
    _run_git(
        ("git", "-C", str(destination), "checkout", "--detach", source.git_commit),
        run,
        timeout_seconds,
    )
    completed = _run_git(
        ("git", "-C", str(destination), "rev-parse", "HEAD"), run, timeout_seconds
    )
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


def _reject_symlink_ancestors(path: Path) -> Path:
    absolute = path.absolute()
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink():
            raise HydrationError(f"storage path must not traverse a symlink: {ancestor}")
    return absolute


def _confined_path(path: Path, root: Path, label: str) -> Path:
    checked = _reject_symlink_ancestors(path)
    try:
        checked.relative_to(root)
    except ValueError as error:
        raise HydrationError(f"{label} must be confined beneath the tool root") from error
    return checked


@contextmanager
def _root_lock(root: Path, timeout_seconds: float):
    if timeout_seconds < 0:
        raise HydrationError("lock timeout must not be negative")
    lock_path = _confined_path(root / ".hydrate.lock", root, "lock")
    with lock_path.open("a+") as lock:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise HydrationError("hydration lock timeout") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def hydrate_toolchain(
    manifest_or_loaded: ToolchainSourceManifest | LoadedToolchainSourceManifest,
    tool_root: Path,
    *,
    cache_directory: Path | None = None,
    opener: Callable[..., Any] = _DEFAULT_OPENER,
    git_run: Callable[..., Any] = subprocess.run,
    host_system: tuple[str, str] | None = None,
    lock_timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    git_timeout_seconds: int = GIT_TIMEOUT_SECONDS,
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

    if tool_root.name != loaded.manifest.tool_root_name:
        raise HydrationError("tool root basename does not match the manifest")
    root = _reject_symlink_ancestors(tool_root)
    components_directory = root / "components"
    receipts_directory = root / "receipts"
    archive_cache = cache_directory or root / "cache"
    results: list[HydratedComponent] = []
    root.mkdir(parents=True, exist_ok=True)
    with _root_lock(root, lock_timeout_seconds):
        components_directory = _confined_path(components_directory, root, "components directory")
        receipts_directory = _confined_path(receipts_directory, root, "receipts directory")
        archive_cache = _confined_path(archive_cache, root, "cache directory")
        for source in loaded.manifest.components:
            destination = _confined_path(
                components_directory / source.component_id, root, "component"
            )
            receipt_path = _confined_path(
                receipts_directory / f"{source.component_id}.json", root, "receipt"
            )
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
                    receipt_path = _confined_path(receipt_path, root, "receipt")
                    _atomic_write(receipt_path, receipt)
                results.append(
                    HydratedComponent(source.component_id, destination, receipt_path, reused=True)
                )
                continue
            if destination.exists():
                raise HydrationError(
                    f"unreceipted component exists and will not be activated: {destination}"
                )

            staging = _confined_path(
                Path(tempfile.mkdtemp(dir=root, prefix=f".{source.component_id}.staging-")),
                root,
                "staging directory",
            )
            staged_component = staging / source.component_id
            try:
                if source.source_kind == "ARCHIVE":
                    archive = download_archive(source, archive_cache, opener=opener)
                    safe_extract_archive(archive, staged_component, source.archive)
                else:
                    checkout_git_source(
                        source, staged_component, run=git_run, timeout_seconds=git_timeout_seconds
                    )
                _atomic_write(staged_component / ".nova-hydration-receipt.json", receipt)
                components_directory = _confined_path(
                    components_directory, root, "components directory"
                )
                components_directory.mkdir(parents=True, exist_ok=True)
                os.replace(staged_component, destination)
                receipt_path = _confined_path(receipt_path, root, "receipt")
                _atomic_write(receipt_path, receipt)
            except Exception:
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
