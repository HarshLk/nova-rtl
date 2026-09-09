from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.optimization import signoff


def test_m4_dependency_uses_independent_frozen_m3_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = tmp_path / "m3-signoff.json"
    packet.write_bytes(b"packet")
    expected_report = object()
    expected_hash = "sha256:" + ("a" * 64)
    calls: list[tuple[Path, Path, str]] = []

    def verify_dependency(
        report_path: Path,
        *,
        repository_root: Path,
        descendant_commit: str,
    ) -> tuple[object, str]:
        calls.append((report_path, repository_root, descendant_commit))
        return expected_report, expected_hash

    monkeypatch.setattr(signoff, "verify_m3_dependency_snapshot", verify_dependency)

    report, packet_hash = signoff._m3_dependency(
        packet,
        repository_root=tmp_path,
        current_commit="b" * 40,
    )

    assert report is expected_report
    assert packet_hash == expected_hash
    assert calls == [(packet, tmp_path, "b" * 40)]


def test_m4_dependency_fails_closed_when_m3_reconstruction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args: object, **_kwargs: object) -> tuple[object, str]:
        raise ValueError("tampered M3 evidence")

    monkeypatch.setattr(signoff, "verify_m3_dependency_snapshot", reject)

    with pytest.raises(signoff.M4SignoffError, match="M3 dependency reconstruction"):
        signoff._m3_dependency(
            tmp_path / "m3-signoff.json",
            repository_root=tmp_path,
            current_commit="b" * 40,
        )
