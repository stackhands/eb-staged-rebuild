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

    def test_probe_json_accepts_non_utf8_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, (b"path-\xff").decode("utf-8", "surrogateescape"))
            os.makedirs(path, exist_ok=True)
            proc = run_cli("probe", "--path", path, "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            data = json.loads(proc.stdout)
            self.assertEqual(data["filesystem"]["path"], path)
            binary = run_cli_optional_binary("probe", "--path", path, "--json")
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(binary.stderr, "")
                self.assertEqual(json.loads(binary.stdout), data)

    def test_probe_empty_path_does_not_search_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "probe-empty-path-eb")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
                handle.write("echo 'This is EasyBuild 5.4.2'\n")
            os.chmod(script, 0o755)
            env = {"PATH": ""}
            proc = run_cli("probe", "--json", "--eb", "probe-empty-path-eb", env=env, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            binary = run_cli_optional_binary("probe", "--json", "--eb", "probe-empty-path-eb", env=env, cwd=tmp)
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(proc.stdout, binary.stdout)
                self.assertEqual(proc.stderr, binary.stderr)
            report = json.loads(proc.stdout)
            self.assertFalse(report["easybuild"]["found"])
            self.assertIsNone(report["easybuild"]["path"])


if __name__ == "__main__":
    unittest.main()
