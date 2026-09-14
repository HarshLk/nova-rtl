from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from typer.testing import CliRunner

from nova_rtl.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANSI_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain_output(output: str) -> str:
    return ANSI_CONTROL_SEQUENCE.sub("", output)


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
        self.assertIn("PROJECT", _plain_output(init_help.output))
        self.assertIn("--stages", _plain_output(analyze_help.output))
        self.assertIn("evidence,opportunities", _plain_output(analyze_help.output))

    def test_cli_exposes_full_benchmark_calibration_command(self) -> None:
        result = self.runner.invoke(app, ["benchmark", "calibrate", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        output = _plain_output(result.output)
        self.assertIn("--config", output)
        self.assertIn("--output", output)
        self.assertIn("--max-samples", output)

    def test_cli_exposes_m2_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m2", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m2", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        signoff_output = _plain_output(signoff.output)
        verify_output = _plain_output(verify.output)
        self.assertIn("RUN_DIRECTORY", signoff_output)
        self.assertIn("--calibration-directory", signoff_output)
        self.assertIn("--m1-packet", signoff_output)
        self.assertNotIn("full-calibration-final", signoff_output)
        self.assertIn("REPORT", verify_output)
        self.assertIn("--m1-packet", verify_output)
        self.assertNotIn("full-calibration-final", verify_output)

    def test_cli_exposes_m3_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m3", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m3", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        self.assertIn("RUN_DIRECTORY", _plain_output(signoff.output))
        self.assertIn("--m2-packet", _plain_output(signoff.output))
        self.assertIn("REPORT", _plain_output(verify.output))
        self.assertIn("--m2-packet", _plain_output(verify.output))

    def test_cli_exposes_m4_optimization_and_candidate_commands(self) -> None:
        optimize = self.runner.invoke(app, ["optimize", "--help"])
        inspect = self.runner.invoke(app, ["candidate", "inspect", "--help"])
        verify = self.runner.invoke(app, ["verify", "--help"])

        self.assertEqual(optimize.exit_code, 0, optimize.output)
        self.assertIn("--planner", _plain_output(optimize.output))
        self.assertIn("--provider-response", _plain_output(optimize.output))
        self.assertIn("--max-candidates", _plain_output(optimize.output))
        self.assertIn("--operations", _plain_output(optimize.output))
        self.assertIn("--seed", _plain_output(optimize.output))
        self.assertIn("--formal-budget", _plain_output(optimize.output))
        self.assertIn("--physical-budget", _plain_output(optimize.output))
        self.assertIn("--stagnation-window", _plain_output(optimize.output))
        self.assertIn("--recovery", _plain_output(optimize.output))
        self.assertEqual(inspect.exit_code, 0, inspect.output)
        self.assertIn("CANDIDATE_ID_OR_BUNDLE", _plain_output(inspect.output))
        self.assertEqual(verify.exit_code, 0, verify.output)

    def test_cli_exposes_m7_failure_inspection(self) -> None:
        result = self.runner.invoke(app, ["failure", "inspect", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("FAILURE_ID", _plain_output(result.output))
        self.assertIn("--runs-root", _plain_output(result.output))

    def test_cli_exposes_m7_signoff_and_verification(self) -> None:
        signoff = self.runner.invoke(app, ["m7", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m7", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        for output in (signoff.output, verify.output):
            plain = _plain_output(output)
            self.assertIn("--m6-packet", plain)
            self.assertIn("--m5-packet", plain)
            self.assertIn("--m4-packet", plain)
            self.assertIn("--m3-packet", plain)

    def test_cli_exposes_m4_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m4", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m4", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        self.assertIn("CANDIDATE_BUNDLE", _plain_output(signoff.output))
        self.assertIn("--m3-packet", _plain_output(signoff.output))
        self.assertIn("REPORT", _plain_output(verify.output))
        self.assertIn("--m3-packet", _plain_output(verify.output))

    def test_cli_exposes_m5_search_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m5", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m5", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        self.assertIn("SEARCH_BUNDLE", _plain_output(signoff.output))
        self.assertIn("--m4-packet", _plain_output(signoff.output))
        self.assertIn("--m3-packet", _plain_output(signoff.output))
        self.assertIn("REPORT", _plain_output(verify.output))
        self.assertIn("--m4-packet", _plain_output(verify.output))
        self.assertIn("--m3-packet", _plain_output(verify.output))

    def test_cli_exposes_m6_signoff_and_verification_commands(self) -> None:
        signoff = self.runner.invoke(app, ["m6", "signoff", "--help"])
        verify = self.runner.invoke(app, ["m6", "verify", "--help"])

        self.assertEqual(signoff.exit_code, 0, signoff.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        for output in (signoff.output, verify.output):
            plain = _plain_output(output)
            self.assertIn("--m5-packet", plain)
            self.assertIn("--m4-packet", plain)
            self.assertIn("--m3-packet", plain)

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
