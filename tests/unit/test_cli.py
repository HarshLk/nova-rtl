from __future__ import annotations

import json
import unittest

from typer.testing import CliRunner

from nova_rtl.cli import app


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

    def test_m1_cli_exposes_signoff_and_offline_verification(self) -> None:
        result = self.runner.invoke(app, ["m1", "--help"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("signoff", result.stdout)
        self.assertIn("verify", result.stdout)


if __name__ == "__main__":
    unittest.main()
