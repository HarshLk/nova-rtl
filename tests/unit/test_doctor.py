from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from nova_rtl.platform.doctor import run_doctor


class DoctorTests(unittest.TestCase):
    def test_missing_tools_are_all_reported_and_fail(self) -> None:
        report = run_doctor(
            required_tools=("missing_yosys", "missing_opensta"),
            which=lambda _: None,
        )

        self.assertEqual(report.status, "FAIL")
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(
            [(check.name, check.status) for check in report.checks],
            [("missing_yosys", "FAIL"), ("missing_opensta", "FAIL")],
        )

    def test_available_tool_is_versioned_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "sample_tool"
            executable.write_text("#!/bin/sh\nprintf 'sample-tool 1.2.3\\n'\n")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            report = run_doctor(
                required_tools=("sample_tool",),
                which=lambda _: str(executable),
            )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.exit_code, 0)
        fingerprint = report.checks[0].tool_fingerprint
        self.assertIsNotNone(fingerprint)
        assert fingerprint is not None
        self.assertEqual(fingerprint.version, "sample-tool 1.2.3")
        self.assertRegex(fingerprint.build_hash, r"^sha256:[0-9a-f]{64}$")

    def test_missing_platform_lock_is_reported_after_tool_checks(self) -> None:
        report = run_doctor(
            required_tools=("missing_yosys",),
            platform_lock=Path("does-not-exist.yaml"),
            which=lambda _: None,
        )

        self.assertEqual(
            [check.name for check in report.checks],
            ["missing_yosys", "platform_lock"],
        )
        self.assertEqual(report.checks[-1].status, "FAIL")
        self.assertIsNone(report.platform_lock_hash)


if __name__ == "__main__":
    unittest.main()
