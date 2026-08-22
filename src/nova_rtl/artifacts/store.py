"""Atomic content-addressed storage for immutable NOVA-RTL evidence bytes."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from nova_rtl.contracts.base import (
    ArtifactRef,
    Classification,
    MediaType,
    StrictContract,
    canonical_json_bytes,
)


class ArtifactStoreError(RuntimeError):
    """Base error for fail-closed artifact persistence and retrieval."""


class ArtifactMissingError(ArtifactStoreError):
    """Raised when an immutable artifact reference has no stored blob."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when stored bytes or a reference violate content identity."""


class _ArtifactMetadata(StrictContract):
    media_type: MediaType
    classification: Classification


class ArtifactStore:
    """Filesystem-backed SHA-256 store with verified, atomic publication."""

    def __init__(self, root: Path, *, _read_only: bool = False) -> None:
        self.root = root.resolve()
        self._objects = self.root / "objects" / "sha256"
        self._locks = self.root / ".locks"
        self._read_only = _read_only
        if _read_only:
            if not self._objects.is_dir():
                raise ArtifactMissingError(f"artifact store does not exist: {self.root}")
        else:
            self._ensure_directory(self._objects)
            self._ensure_directory(self._locks)

    @classmethod
    def open_existing(cls, root: Path) -> ArtifactStore:
        """Open an existing store without creating or modifying directories."""

        return cls(root, _read_only=True)

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        classification: Classification,
    ) -> ArtifactRef:
        """Atomically publish bytes once and return their content-derived reference."""

        if self._read_only:
            raise ArtifactStoreError("artifact store is read-only")
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        metadata = _ArtifactMetadata(
            media_type=media_type,
            classification=classification,
        )
        digest = sha256(data).hexdigest()
        target = self._blob_path_for_digest(digest)
        self._ensure_directory(target.parent)
        lock_path = self._locks / f"{digest}.lock"
        temporary_path: Path | None = None
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if target.exists():
                    self._verify_path(target, digest=digest, expected_size=len(data))
                    if stat.S_IMODE(target.stat().st_mode) != 0o444:
                        os.chmod(target, 0o444)
                        self._fsync_file(target)
                else:
                    with tempfile.NamedTemporaryFile(
                        mode="w+b",
                        dir=target.parent,
                        prefix=f".{digest}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        temporary.write(data)
                        temporary.flush()
                        os.fchmod(temporary.fileno(), 0o444)
                        os.fsync(temporary.fileno())
                    os.replace(temporary_path, target)
                    temporary_path = None
                    self._fsync_directory(target.parent)
                    self._verify_path(target, digest=digest, expected_size=len(data))
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        created_at = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
        return ArtifactRef(
            artifact_id=f"artifact_{digest}",
            uri=f"artifact://sha256/{digest}",
            sha256=f"sha256:{digest}",
            media_type=metadata.media_type,
            size_bytes=len(data),
            created_at=created_at,
            producer_stage_result_id=None,
            classification=metadata.classification,
        )

    def put_json(
        self,
        value: StrictContract,
        *,
        classification: Classification,
    ) -> ArtifactRef:
        """Serialize a strict contract canonically before immutable publication."""

        if not isinstance(value, StrictContract):
            raise TypeError("put_json requires a StrictContract")
        return self.put_bytes(
            canonical_json_bytes(value),
            media_type="application/json",
            classification=classification,
        )

    def blob_path(self, ref: ArtifactRef) -> Path:
        """Resolve a content URI only when all reference identities agree."""

        prefix = "artifact://sha256/"
        if not ref.uri.startswith(prefix):
            raise ArtifactIntegrityError(
                f"artifact {ref.artifact_id} is outside the content-addressed namespace"
            )
        digest = ref.uri.removeprefix(prefix)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ArtifactIntegrityError(
                f"artifact {ref.artifact_id} has an invalid content-addressed URI"
            )
        if ref.sha256 != f"sha256:{digest}" or ref.artifact_id != f"artifact_{digest}":
            raise ArtifactIntegrityError(
                f"artifact {ref.artifact_id} content identities disagree"
            )
        return self._blob_path_for_digest(digest)

    def open_verified(self, ref: ArtifactRef) -> BinaryIO:
        """Return an in-memory reader only after size, mode, and SHA-256 verification."""

        path = self.blob_path(ref)
        if not path.exists():
            raise ArtifactMissingError(f"artifact {ref.artifact_id} is missing")
        data = self._verify_path(
            path,
            digest=ref.sha256.removeprefix("sha256:"),
            expected_size=ref.size_bytes,
        )
        return BytesIO(data)

    def _blob_path_for_digest(self, digest: str) -> Path:
        return self._objects / digest[:2] / digest

    @staticmethod
    def _verify_path(path: Path, *, digest: str, expected_size: int) -> bytes:
        file_stat = path.lstat()
        artifact_id = f"artifact_{path.name}"
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise ArtifactIntegrityError(f"artifact {artifact_id} is not a regular file")
        data = path.read_bytes()
        if len(data) != expected_size or sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError(
                f"artifact {artifact_id} failed size or SHA-256 verification"
            )
        return data

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _ensure_directory(cls, directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            with suppress(FileExistsError):
                path.mkdir()
            cls._fsync_directory(path)
            cls._fsync_directory(path.parent)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactMissingError",
    "ArtifactStore",
    "ArtifactStoreError",
]
