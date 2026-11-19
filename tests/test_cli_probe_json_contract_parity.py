from __future__ import annotations

import json
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class ProbeJsonContractParityTests(unittest.TestCase):
    def test_probe_json_happy_path_is_single_sorted_document(self) -> None:
        with tempfile.TemporaryDirectory() as path:
            proc = run_cli("probe", "--json", "--path", path, "--eb", "missing-eb")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertTrue(proc.stdout.endswith("\n"))
            document = json.loads(proc.stdout)
            self.assertEqual(
                list(document.keys()),
                [
                    "bwrap",
                    "easybuild",
                    "filesystem",
                    "flock",
                    "rename_exchange",
                    "user_namespaces",
                    "userns_sandbox",
                ],
            )
            self.assertEqual(proc.stdout, json.dumps(document, indent=2, sort_keys=True) + "\n")

            binary = run_cli_optional_binary("probe", "--json", "--path", path, "--eb", "missing-eb")
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(binary.stderr, "")
                self.assertEqual(binary.stdout, proc.stdout)


if __name__ == "__main__":
    unittest.main()
