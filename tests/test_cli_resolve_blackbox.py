"""Black-box CLI checks for the resolve subcommand."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class ResolveBlackBoxTests(unittest.TestCase):
    def test_resolve_missing_module_reports_json_and_exit_one(self) -> None:
        proc = run_cli("resolve", "/tmp/definitely-missing-module.lua")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        data = json.loads(proc.stdout)
        self.assertEqual(data["kind"], "missing")
        self.assertFalse(data["complete"])

    def test_resolve_complete_module_reports_complete_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            software = os.path.join(tmp, "software", "Demo", "1.0")
            os.makedirs(os.path.join(software, "easybuild"), exist_ok=True)
            with open(os.path.join(software, "easybuild", "Demo-1.0.eb"), "w", encoding="utf-8") as handle:
                handle.write("name = 'Demo'\n")
            module = os.path.join(tmp, "modules", "all", "Demo", "1.0.lua")
            os.makedirs(os.path.dirname(module), exist_ok=True)
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{software}"\n')

            proc = run_cli("resolve", module)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            data = json.loads(proc.stdout)
            self.assertEqual(data["kind"], "file")
            self.assertTrue(data["complete"])
            self.assertEqual(data["root"], software)

    @unittest.expectedFailure
    def test_resolve_malformed_module_should_report_parse_reason(self) -> None:
        """Reproducer: eb-staged-rebuild resolve /tmp/bad.lua"""
        with tempfile.TemporaryDirectory() as tmp:
            module = os.path.join(tmp, "bad.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write("local root =\n")

            proc = run_cli("resolve", module)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertIn("error", data)
            self.assertTrue(data["error"])


if __name__ == "__main__":
    unittest.main()
