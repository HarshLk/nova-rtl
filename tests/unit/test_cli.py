from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from typer.testing import CliRunner

from nova_rtl.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_version_reports_the_installed_nova_rtl_version(self) -> None:
        result = self.runner.invoke(app, ["version"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "NOVA-RTL 0.1.0")

    def test_doctor_json_reports_missing_tool_and_nonzero_exit(self) -> None:
        result = self.runner.invoke(
            app,
            ["doctor", "--tool", "nova_tool_that_does_not_exist", "--json"],
        )

        self.assertEqual(result.exit_code, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["checks"][0]["name"], "nova_tool_that_does_not_exist")
        self.assertEqual(payload["checks"][0]["status"], "FAIL")

    def test_cli_exposes_baseline_initialization_and_analysis_commands(self) -> None:
        init_help = self.runner.invoke(app, ["init", "--help"])
        analyze_help = self.runner.invoke(app, ["analyze", "--help"])

        self.assertEqual(init_help.exit_code, 0, init_help.output)
        self.assertEqual(analyze_help.exit_code, 0, analyze_help.output)
        self.assertIn("PROJECT", init_help.output)
        self.assertIn("--stages", analyze_help.output)

    def test_cli_exposes_full_benchmark_calibration_command(self) -> None:
        result = self.runner.invoke(app, ["benchmark", "calibrate", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--config", result.output)
        self.assertIn("--output", result.output)
        self.assertIn("--max-samples", result.output)

    def test_cli_exposes_m2_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m2", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m2", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        self.assertIn("RUN_DIRECTORY", signoff.output)
        self.assertIn("--calibration-directory", signoff.output)
        self.assertIn("--m1-packet", signoff.output)
        self.assertNotIn("full-calibration-final", signoff.output)
        self.assertIn("REPORT", verify.output)
        self.assertIn("--m1-packet", verify.output)
        self.assertNotIn("full-calibration-final", verify.output)

    def test_m1_cli_exposes_signoff_and_offline_verification(self) -> None:
        result = self.runner.invoke(app, ["m1", "--help"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("signoff", result.stdout)
        self.assertIn("verify", result.stdout)

    def test_core_cli_import_does_not_require_optional_pytest(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                "import sys; sys.modules['pytest'] = None; import nova_rtl.cli",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
