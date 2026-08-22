from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova_rtl.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactMissingError,
    ArtifactStore,
)
from nova_rtl.contracts.base import StrictContract


class ExamplePayload(StrictContract):
    name: str
    count: int


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def test_put_is_content_addressed_read_only_and_verified(store: ArtifactStore) -> None:
    first = store.put_bytes(
        b"same", media_type="text/plain", classification="INTERNAL"
    )
    second = store.put_bytes(
        b"same", media_type="text/plain", classification="INTERNAL"
    )

    assert first == second
    assert first.sha256 == "sha256:0967115f2813a3541eaef77de9d9d5773f1c0c04314b0bbfe4ff3b3b1c55b5d5"
    assert first.uri == (
        "artifact://sha256/0967115f2813a3541eaef77de9d9d5773f1c0c04314b0bbfe4ff3b3b1c55b5d5"
    )
    assert store.open_verified(first).read() == b"same"
    assert stat.S_IMODE(store.blob_path(first).stat().st_mode) == 0o444


def test_put_json_uses_canonical_contract_bytes(store: ArtifactStore) -> None:
    ref = store.put_json(
        ExamplePayload(name="counter", count=2), classification="INTERNAL"
    )

    assert ref.media_type == "application/json"
    assert store.open_verified(ref).read() == b'{"count":2,"name":"counter"}'


def test_concurrent_puts_publish_one_verified_blob(store: ArtifactStore) -> None:
    def put_once(_: int):
        return store.put_bytes(
            b"concurrent", media_type="application/octet-stream", classification="INTERNAL"
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = tuple(executor.map(put_once, range(32)))

    assert len(set(refs)) == 1
    assert store.open_verified(refs[0]).read() == b"concurrent"
    assert not tuple(store.root.rglob("*.tmp"))


def test_open_verified_fails_closed_for_corruption_and_missing_blob(
    store: ArtifactStore,
) -> None:
    ref = store.put_bytes(
        b"trusted", media_type="text/plain", classification="RESTRICTED_RTL"
    )
    blob = store.blob_path(ref)
    os.chmod(blob, 0o644)
    blob.write_bytes(b"changed")

    with pytest.raises(ArtifactIntegrityError, match=ref.artifact_id):
        store.open_verified(ref)

    blob.unlink()
    with pytest.raises(ArtifactMissingError, match=ref.artifact_id):
        store.open_verified(ref)


def test_verified_bytes_survive_checkout_permission_normalization(
    store: ArtifactStore,
) -> None:
    ref = store.put_bytes(
        b"portable", media_type="text/plain", classification="INTERNAL"
    )
    os.chmod(store.blob_path(ref), 0o644)

    assert store.open_verified(ref).read() == b"portable"
    assert store.put_bytes(
        b"portable", media_type="text/plain", classification="INTERNAL"
    ) == ref
    assert stat.S_IMODE(store.blob_path(ref).stat().st_mode) == 0o444


def test_blob_path_rejects_refs_outside_the_content_namespace(store: ArtifactStore) -> None:
    ref = store.put_bytes(
        b"trusted", media_type="text/plain", classification="INTERNAL"
    )
    forged = ref.model_copy(update={"uri": "artifact://runs/run_001/report.txt"})

    with pytest.raises(ArtifactIntegrityError, match="content-addressed"):
        store.open_verified(forged)


@pytest.mark.parametrize(
    ("media_type", "classification"),
    [
        ("not-a-media-type", "INTERNAL"),
        ("application/octet-stream", "UNREGISTERED"),
    ],
)
def test_invalid_metadata_leaves_no_published_blob(
    store: ArtifactStore, media_type: str, classification: str
) -> None:
    with pytest.raises(ValidationError):
        store.put_bytes(
            b"sensitive",
            media_type=media_type,
            classification=classification,  # type: ignore[arg-type]
        )

    assert not tuple(path for path in store.root.rglob("*") if path.is_file())


def test_publication_syncs_read_only_inode_and_new_directory_entry(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = os.fsync
    file_modes_at_sync: list[int] = []
    synced_directories: list[Path] = []

    def tracking_fsync(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        if stat.S_ISREG(descriptor_stat.st_mode):
            file_modes_at_sync.append(stat.S_IMODE(descriptor_stat.st_mode))
        elif stat.S_ISDIR(descriptor_stat.st_mode):
            synced_directories.append(
                Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve()
            )
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    ref = store.put_bytes(
        b"durable", media_type="text/plain", classification="INTERNAL"
    )
    fanout_directory = store.blob_path(ref).parent

    assert file_modes_at_sync == [0o444]
    assert fanout_directory in synced_directories
    assert fanout_directory.parent in synced_directories
