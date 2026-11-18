"""Extra black-box CLI checks for the probe subcommand."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


class ProbeBlackBoxExtraTests(unittest.TestCase):
    def test_probe_json_includes_easybuild_section(self) -> None:
        proc = run_cli("probe", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        data = json.loads(proc.stdout)
        self.assertIn("easybuild", data)
        self.assertIn("found", data["easybuild"])

    def test_probe_no_create_missing_path_returns_exit_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing")
            proc = run_cli("probe", "--path", missing, "--no-create")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"probe path does not exist: {missing}", proc.stderr)


if __name__ == "__main__":
    unittest.main()
