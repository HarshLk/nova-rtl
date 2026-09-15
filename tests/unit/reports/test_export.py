from __future__ import annotations

import io
import tarfile
from pathlib import Path

import zstandard

from nova_rtl.reports.export import build_submission_archive, write_text_pdf


def test_pdf_and_submission_archive_are_deterministic(tmp_path: Path) -> None:
    formal = tmp_path / "formal.pdf"
    timing = tmp_path / "timing.pdf"
    write_text_pdf(formal, "Formal equivalence", ("STRICT_SEQ_EQUIV: PASS",))
    write_text_pdf(timing, "Timing and PPA", ("Outcome: VALID_NEGATIVE_RESULT",))
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")

    files = {
        "evidence/evidence.json": evidence,
        "reports/formal.pdf": formal,
        "reports/timing.pdf": timing,
    }
    first = build_submission_archive(files, tmp_path / "first.tar.zst")
    second = build_submission_archive(files, tmp_path / "second.tar.zst")

    assert formal.read_bytes().startswith(b"%PDF-1.4")
    assert first == second
    raw = zstandard.ZstdDecompressor().decompress((tmp_path / "first.tar.zst").read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        assert archive.getnames() == [
            "evidence/evidence.json",
            "reports/formal.pdf",
            "reports/timing.pdf",
        ]
