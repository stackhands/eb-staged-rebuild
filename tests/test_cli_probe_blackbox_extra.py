"""Extra black-box CLI checks for the probe subcommand."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


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

    def test_probe_path_that_is_a_file_refuses_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "path-file")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("marker\n")
            proc = run_cli("probe", "--path", path, "--json")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("[Errno 17] File exists", proc.stderr)
            binary = run_cli_optional_binary("probe", "--path", path, "--json")
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(proc.stdout, binary.stdout)
                self.assertEqual(proc.stderr, binary.stderr)


if __name__ == "__main__":
    unittest.main()
