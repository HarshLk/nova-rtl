"""Deterministic, dependency-light M9 submission exports."""

from __future__ import annotations

import io
import tarfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

import zstandard


def _hash(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def write_text_pdf(path: Path, title: str, lines: tuple[str, ...]) -> str:
    """Write a small valid PDF whose content is stable across machines."""

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = (title, *lines)
    commands = ["BT", "/F1 12 Tf", "50 790 Td"]
    for index, line in enumerate(text):
        if index:
            commands.append("0 -18 Td")
        commands.append(f"({escape(line)}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii", errors="replace")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode())
        content.extend(obj)
        content.extend(b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return _hash(bytes(content))


def build_submission_archive(files: dict[str, Path], output: Path) -> str:
    """Create a canonical tar.zst with fixed metadata and sorted confined paths."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, source in sorted(files.items()):
            target = PurePosixPath(name)
            if target.is_absolute() or ".." in target.parts or name != target.as_posix():
                raise ValueError(f"submission archive path is not confined: {name}")
            content = source.resolve(strict=True).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    compressed = zstandard.ZstdCompressor(level=3, threads=0).compress(buffer.getvalue())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(compressed)
    return _hash(compressed)


__all__ = ["build_submission_archive", "write_text_pdf"]
