from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
import zstandard as zstd

from nova_rtl.contracts.platform import (
    ArchiveMetadata,
    HostPlatform,
    ToolchainSourceManifest,
    ToolExecutableSource,
    ToolSource,
)
from nova_rtl.platform.hydration import (
    HydrationError,
    checkout_git_source,
    download_archive,
    hydrate_toolchain,
    load_toolchain_source_manifest,
    safe_extract_archive,
)


def hash_ref(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def source(data: bytes, *, component_id: str = "suite", size: int | None = None) -> ToolSource:
    return ToolSource(
        component_id=component_id,
        source_kind="ARCHIVE",
        version="1.0",
        source_url="https://downloads.example.test/releases/suite.tar.gz",
        archive_sha256=hash_ref(data),
        git_commit=None,
        license="ISC",
        executables=(
            ToolExecutableSource(
                tool_id="yosys", relative_path="bin/yosys", version_args=("-V",)
            ),
        ),
        archive=ArchiveMetadata(
            byte_size=len(data) if size is None else size,
            archive_format="TAR_GZ",
            strip_components=1,
            max_decompressed_bytes=1024 * 1024,
            max_regular_file_bytes=1024 * 1024,
            max_entries=128,
        ),
        runtime_environment=(),
        allowed_redirect_hosts=(),
    )


def manifest(data: bytes, **kwargs: object) -> ToolchainSourceManifest:
    return ToolchainSourceManifest(
        host=HostPlatform(os="linux", architecture="x86_64"),
        tool_root_name=".nova-tools",
        components=(source(data, **kwargs),),
    )


def tar_bytes(entries: dict[str, bytes], *, link: tuple[str, str] | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if link is not None:
            member = tarfile.TarInfo(link[0])
            member.type = tarfile.SYMTYPE
            member.linkname = link[1]
            archive.addfile(member)
    return stream.getvalue()


def raw_tar_bytes(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def deb_bytes(data_tar: bytes, *, data_name: str = "data.tar.gz") -> bytes:
    def member(name: str, data: bytes) -> bytes:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{0o100644:<8}{len(data):<10}`\n"
        return header.encode("ascii") + data + (b"\n" if len(data) % 2 else b"")

    return b"!<arch>\n" + member("debian-binary", b"2.0\n") + member(data_name, data_tar)


class Response(io.BytesIO):
    def __init__(
        self, data: bytes, url: str, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(data)
        self._url = url
        self.status = status
        self.headers = headers or {}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def opener_for(data: bytes, *, final_url: str | None = None):
    calls: list[object] = []

    def opener(request: object) -> Response:
        calls.append(request)
        return Response(data, final_url or "https://downloads.example.test/releases/suite.tar.gz")

    return opener, calls


def test_hydration_rejects_archive_size_mismatch_before_extraction(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, _ = opener_for(data)

    with pytest.raises(HydrationError, match="archive size mismatch"):
        hydrate_toolchain(
            manifest(data, size=len(data) + 1), tmp_path / ".nova-tools", opener=opener
        )

    assert not (tmp_path / ".nova-tools" / "components" / "suite").exists()


def test_hydration_rejects_archive_hash_mismatch_before_extraction(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, _ = opener_for(data)
    bad_source = source(data).model_copy(update={"archive_sha256": hash_ref(b"other")})
    source_manifest = manifest(data).model_copy(
        update={"components": (bad_source,)}
    )

    with pytest.raises(HydrationError, match="SHA-256 mismatch"):
        hydrate_toolchain(source_manifest, tmp_path / ".nova-tools", opener=opener)

    assert not (tmp_path / ".nova-tools" / "components" / "suite").exists()


def test_safe_extraction_rejects_parent_traversal(tmp_path: Path) -> None:
    data = tar_bytes({"suite/../../outside": b"unsafe"})
    archive = tmp_path / "unsafe.tar.gz"
    archive.write_bytes(data)

    with pytest.raises(HydrationError, match="unsafe archive path"):
        safe_extract_archive(archive, tmp_path / "destination", source(data).archive)

    assert not (tmp_path / "outside").exists()


def test_safe_extraction_rejects_escaping_symbolic_link(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"}, link=("suite/bin/escape", "../../outside"))
    archive = tmp_path / "unsafe.tar.gz"
    archive.write_bytes(data)

    with pytest.raises(HydrationError, match="unsafe archive link"):
        safe_extract_archive(archive, tmp_path / "destination", source(data).archive)


def test_safe_extraction_rejects_special_file(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("suite/device")
        member.type = tarfile.CHRTYPE
        archive.addfile(member)
    archive = tmp_path / "unsafe.tar.gz"
    archive.write_bytes(stream.getvalue())

    with pytest.raises(HydrationError, match="unsupported archive entry"):
        safe_extract_archive(archive, tmp_path / "destination", source(stream.getvalue()).archive)


def test_safe_debian_extraction_uses_the_data_archive_only(tmp_path: Path) -> None:
    package = deb_bytes(tar_bytes({"usr/bin/openroad": b"tool"}))
    archive = tmp_path / "openroad.deb"
    archive.write_bytes(package)
    metadata = ArchiveMetadata(
        byte_size=len(package),
        archive_format="DEB",
        strip_components=0,
        max_decompressed_bytes=1024 * 1024,
        max_regular_file_bytes=1024 * 1024,
        max_entries=128,
    )

    safe_extract_archive(archive, tmp_path / "destination", metadata)

    assert (tmp_path / "destination" / "usr" / "bin" / "openroad").read_bytes() == b"tool"


def test_safe_debian_extraction_streams_zstd_data_archive(tmp_path: Path) -> None:
    compressed = zstd.ZstdCompressor().compress(raw_tar_bytes({"usr/bin/openroad": b"tool"}))
    package = deb_bytes(compressed, data_name="data.tar.zst")
    archive = tmp_path / "openroad.deb"
    archive.write_bytes(package)
    metadata = ArchiveMetadata(
        byte_size=len(package),
        archive_format="DEB",
        strip_components=0,
        max_decompressed_bytes=1024 * 1024,
        max_regular_file_bytes=1024 * 1024,
        max_entries=128,
    )

    safe_extract_archive(archive, tmp_path / "destination", metadata)

    assert (tmp_path / "destination" / "usr" / "bin" / "openroad").read_bytes() == b"tool"


def test_malformed_zstd_debian_payload_never_publishes_a_component(tmp_path: Path) -> None:
    package = deb_bytes(b"not zstd", data_name="data.tar.zst")
    archive_source = source(package).model_copy(
        update={
            "archive": ArchiveMetadata(
                byte_size=len(package),
                archive_format="DEB",
                strip_components=0,
                max_decompressed_bytes=1024 * 1024,
                max_regular_file_bytes=1024 * 1024,
                max_entries=128,
            )
        }
    )
    source_manifest = manifest(package).model_copy(update={"components": (archive_source,)})
    opener, _ = opener_for(package)
    root = tmp_path / ".nova-tools"

    with pytest.raises(HydrationError, match="zstd"):
        hydrate_toolchain(source_manifest, root, opener=opener)

    assert not (root / "components" / "suite").exists()
    assert not (root / "receipts" / "suite.json").exists()
    assert not tuple(root.glob(".suite.staging-*"))


def test_hydration_rejects_redirect_to_another_host(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, _ = opener_for(data, final_url="https://untrusted.example.test/suite.tar.gz")

    with pytest.raises(HydrationError, match="redirect"):
        hydrate_toolchain(manifest(data), tmp_path / ".nova-tools", opener=opener)


def test_download_resumes_a_partial_archive_only_when_server_confirms_range(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    archive_source = source(data)
    cache = tmp_path / "cache"
    cache.mkdir()
    partial = cache / f".{archive_source.archive_sha256.removeprefix('sha256:')}.part"
    offset = len(data) // 2
    partial.write_bytes(data[:offset])
    observed_ranges: list[str | None] = []

    def opener(request: object) -> Response:
        observed_ranges.append(request.get_header("Range"))  # type: ignore[attr-defined]
        return Response(data[offset:], archive_source.source_url, status=206)

    downloaded = download_archive(archive_source, cache, opener=opener)

    assert observed_ranges == [f"bytes={offset}-"]
    assert downloaded.read_bytes() == data


def test_hydration_never_publishes_a_partial_component(tmp_path: Path) -> None:
    bad_data = tar_bytes({"suite/../../outside": b"unsafe"})
    opener, _ = opener_for(bad_data)
    root = tmp_path / ".nova-tools"

    with pytest.raises(HydrationError):
        hydrate_toolchain(manifest(bad_data), root, opener=opener)

    assert not (root / "components" / "suite").exists()
    assert not (root / "receipts" / "suite.json").exists()


def test_git_checkout_rejects_a_different_resolved_commit(tmp_path: Path) -> None:
    git_source = ToolSource(
        component_id="orfs",
        source_kind="GIT",
        version="pinned",
        source_url="https://github.com/example/orfs.git",
        archive_sha256=None,
        git_commit="a" * 40,
        license="BSD-3-Clause",
        executables=(),
        archive=None,
        runtime_environment=(),
        allowed_redirect_hosts=(),
    )

    def run(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        if command[-2:] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout=("b" * 40 + "\n"), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(HydrationError, match="Git commit mismatch"):
        checkout_git_source(git_source, tmp_path / "checkout", run=run)


def test_hydration_rejects_an_unsupported_host(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, _ = opener_for(data)

    with pytest.raises(HydrationError, match="unsupported host"):
        hydrate_toolchain(
            manifest(data),
            tmp_path / ".nova-tools",
            opener=opener,
            host_system=("darwin", "x86_64"),
        )


def test_repeated_hydration_uses_the_existing_receipt_without_network(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, calls = opener_for(data)
    root = tmp_path / ".nova-tools"

    first = hydrate_toolchain(manifest(data), root, opener=opener)
    second = hydrate_toolchain(manifest(data), root, opener=opener)

    assert first.components[0].reused is False
    assert second.components[0].reused is True
    assert len(calls) == 1
    assert (root / "receipts" / "suite.json").is_file()


def test_manifest_content_identity_is_independent_of_json_or_yaml_spelling(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    source_manifest = manifest(data)
    json_path = tmp_path / "sources.json"
    yaml_path = tmp_path / "sources.yaml"
    json_path.write_text(json.dumps(source_manifest.model_dump(mode="json")), encoding="utf-8")
    yaml_path.write_text(yaml.safe_dump(source_manifest.model_dump(mode="json")), encoding="utf-8")

    from_json = load_toolchain_source_manifest(json_path)
    from_yaml = load_toolchain_source_manifest(yaml_path)

    assert from_json.manifest == from_yaml.manifest
    assert from_json.content_identity_hash == from_yaml.content_identity_hash


def test_download_rejects_and_cleans_an_overlong_response(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    archive_source = source(data)
    cache = tmp_path / "cache"
    opener, _ = opener_for(data + b"unexpected bytes")

    with pytest.raises(HydrationError, match="exceeds expected size"):
        download_archive(archive_source, cache, opener=opener)

    assert not tuple(cache.glob("*.part*"))


def test_safe_extraction_rejects_regular_file_budget_bomb(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"too large"})
    archive = tmp_path / "bomb.tar.gz"
    archive.write_bytes(data)
    metadata = source(data).archive.model_copy(update={"max_regular_file_bytes": 1})

    with pytest.raises(HydrationError, match="regular-file byte budget"):
        safe_extract_archive(archive, tmp_path / "destination", metadata)

    assert not (tmp_path / "destination").exists()


def test_safe_extraction_rejects_zstd_decompression_bomb(tmp_path: Path) -> None:
    payload = raw_tar_bytes({"usr/bin/openroad": b"x" * 4096})
    package = deb_bytes(zstd.ZstdCompressor().compress(payload), data_name="data.tar.zst")
    archive = tmp_path / "bomb.deb"
    archive.write_bytes(package)
    metadata = ArchiveMetadata(
        byte_size=len(package),
        archive_format="DEB",
        strip_components=0,
        max_decompressed_bytes=16,
        max_regular_file_bytes=8192,
        max_entries=8,
    )

    with pytest.raises(HydrationError, match="decompressed byte budget"):
        safe_extract_archive(archive, tmp_path / "destination", metadata)

    assert not (tmp_path / "destination").exists()
    assert not tuple(tmp_path.glob(".data-*.tar"))


def test_download_validates_each_redirect_against_manifest_allowlist(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    archive_source = source(data).model_copy(
        update={"allowed_redirect_hosts": ("release-assets.githubusercontent.com",)}
    )
    responses = iter(
        (
            Response(
                b"",
                archive_source.source_url,
                status=302,
                headers={"Location": "https://untrusted.example.test/archive"},
            ),
        )
    )

    with pytest.raises(HydrationError, match="redirect host"):
        download_archive(archive_source, tmp_path / "cache", opener=lambda _: next(responses))


def test_download_follows_an_explicitly_allowed_https_redirect(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    archive_source = source(data).model_copy(
        update={"allowed_redirect_hosts": ("release-assets.githubusercontent.com",)}
    )
    redirected_url = "https://release-assets.githubusercontent.com/suite.tar.gz"
    responses = iter(
        (
            Response(
                b"",
                archive_source.source_url,
                status=302,
                headers={"Location": redirected_url},
            ),
            Response(data, redirected_url),
        )
    )

    downloaded = download_archive(
        archive_source, tmp_path / "cache", opener=lambda _: next(responses)
    )

    assert downloaded.read_bytes() == data


def test_hydration_rejects_a_root_or_cache_outside_manifest_tool_root(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    opener, _ = opener_for(data)

    with pytest.raises(HydrationError, match="tool root"):
        hydrate_toolchain(manifest(data), tmp_path / "wrong-root", opener=opener)

    root = tmp_path / ".nova-tools"
    with pytest.raises(HydrationError, match="cache directory"):
        hydrate_toolchain(manifest(data), root, cache_directory=tmp_path / "cache", opener=opener)


def test_hydration_rejects_a_symlinked_tool_root(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    root = tmp_path / ".nova-tools"
    root.symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(HydrationError, match="symlink"):
        hydrate_toolchain(manifest(data), root, opener=opener_for(data)[0])


def test_hydration_rejects_symlinked_receipts_without_writing_outside_root(tmp_path: Path) -> None:
    data = tar_bytes({"suite/bin/yosys": b"tool"})
    root = tmp_path / ".nova-tools"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "receipts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HydrationError, match="symlink"):
        hydrate_toolchain(manifest(data), root, opener=opener_for(data)[0])

    assert not tuple(outside.iterdir())


def test_declared_tar_format_must_match_archive_bytes(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as archive:
        member = tarfile.TarInfo("suite/bin/yosys")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"tool"))
    archive = tmp_path / "wrong-format.tar"
    archive.write_bytes(stream.getvalue())
    metadata = source(stream.getvalue()).archive

    with pytest.raises(HydrationError, match="format"):
        safe_extract_archive(archive, tmp_path / "destination", metadata)


def test_git_checkout_passes_a_finite_subprocess_timeout(tmp_path: Path) -> None:
    git_source = ToolSource(
        component_id="orfs",
        source_kind="GIT",
        version="pinned",
        source_url="https://github.com/example/orfs.git",
        archive_sha256=None,
        git_commit="a" * 40,
        license="BSD-3-Clause",
        executables=(),
        archive=None,
        runtime_environment=(),
        allowed_redirect_hosts=(),
    )
    timeouts: list[int] = []

    def run(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        timeouts.append(kwargs["timeout"])  # type: ignore[arg-type]
        stdout = "a" * 40 + "\n" if command[-2:] == ("rev-parse", "HEAD") else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    checkout_git_source(git_source, tmp_path / "checkout", run=run)

    assert timeouts and all(timeout > 0 for timeout in timeouts)


def test_git_checkout_avoids_downloading_unneeded_blob_history(tmp_path: Path) -> None:
    git_source = ToolSource(
        component_id="orfs",
        source_kind="GIT",
        version="pinned",
        source_url="https://github.com/example/orfs.git",
        archive_sha256=None,
        git_commit="a" * 40,
        license="BSD-3-Clause",
        executables=(),
        archive=None,
        runtime_environment=(),
        allowed_redirect_hosts=(),
    )
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        commands.append(command)
        stdout = "a" * 40 + "\n" if command[-2:] == ("rev-parse", "HEAD") else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    checkout_git_source(git_source, tmp_path / "checkout", run=run)

    assert commands[0] == (
        "git",
        "-c",
        "http.followRedirects=false",
        "-c",
        "protocol.file.allow=never",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        "--no-recurse-submodules",
        "--",
        "https://github.com/example/orfs.git",
        str(tmp_path / "checkout"),
    )


def test_hydration_times_out_when_another_process_owns_the_root_lock(tmp_path: Path) -> None:
    import fcntl

    data = tar_bytes({"suite/bin/yosys": b"tool"})
    root = tmp_path / ".nova-tools"
    root.mkdir()
    lock_path = root / ".hydrate.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(HydrationError, match="lock timeout"):
            hydrate_toolchain(
                manifest(data), root, opener=opener_for(data)[0], lock_timeout_seconds=0
            )
