from __future__ import annotations

import json
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class ProbeJsonContractParityTests(unittest.TestCase):
    def test_probe_json_happy_path_is_single_sorted_document(self) -> None:
        with tempfile.TemporaryDirectory() as path:
            binary = run_cli("probe", "--json", "--path", path, "--eb", "missing-eb")
            self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
            self.assertEqual(binary.stderr, "")
            self.assertTrue(binary.stdout.endswith("\n"))
            document = json.loads(binary.stdout)
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
            self.assertEqual(binary.stdout, json.dumps(document, indent=2, sort_keys=True) + "\n")
            golden(self, "stdout", scrub(binary.stdout, PATH=path))


if __name__ == "__main__":
    unittest.main()
