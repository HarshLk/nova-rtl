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
        self.assertEqual(
            [check.issues[0].code for check in report.checks],
            ["TOOL_MISSING", "TOOL_MISSING"],
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

    def test_hydrated_mapping_and_probe_environment_bypass_caller_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "sample_tool"
            executable.write_text(
                "#!/bin/sh\n"
                '[ "$NOVA_TEST_ENV" = ready ] || exit 3\n'
                "printf 'sample-tool 1.2.3\\n'\n"
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            report = run_doctor(
                required_tools=("sample_tool",),
                which=lambda _: (_ for _ in ()).throw(AssertionError("PATH must not be used")),
                hydrated_tools={"sample_tool": executable},
                probe_environment={"NOVA_TEST_ENV": "ready"},
            )

        self.assertEqual(report.status, "PASS")

    def test_missing_hydrated_mapping_entry_never_falls_back_to_caller_path(self) -> None:
        report = run_doctor(
            required_tools=("yosys",),
            which=lambda _: (_ for _ in ()).throw(AssertionError("PATH must not be used")),
            hydrated_tools={},
        )

        self.assertEqual(report.status, "FAIL")
        self.assertEqual(report.checks[0].issues[0].code, "TOOL_MISSING")

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
        self.assertEqual(report.checks[-1].issues[0].code, "PLATFORM_LOCK_MISSING")

    def test_platform_lock_symlink_loop_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop = Path(directory) / "loop.lock.yaml"
            loop.symlink_to(loop.name)

            report = run_doctor(required_tools=(), platform_lock=loop)

        self.assertEqual(report.status, "FAIL")
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.checks[0].issues[0].code, "PLATFORM_LOCK_IO_ERROR")


if __name__ == "__main__":
    unittest.main()
