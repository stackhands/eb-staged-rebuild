"""Regression test for non-UTF-8 probe command output."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import golden, run_cli


class ProbeNonUtf8VersionOutputTests(unittest.TestCase):
    def test_non_utf8_eb_version_output_keeps_probe_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            eb = os.path.join(root, "eb")
            with open(eb, "wb") as handle:
                handle.write(
                    b"#!/bin/sh\n"
                    b'if [ "$1" = "--version" ]; then printf "\\300\\252\\n"; exit 0; fi\n'
                    b"exit 0\n"
                )
            os.chmod(eb, 0o755)

            zig = run_cli("probe", "--json", "--path", root, "--eb", eb)
            self.assertEqual(zig.returncode, 0, zig.stdout + zig.stderr)
            self.assertNotIn("Traceback", zig.stderr)
            easybuild = json.loads(zig.stdout)["easybuild"]
            golden(self, "framework_version", json.dumps(easybuild["framework_version"], sort_keys=True))
            golden(self, "stage_path_keys", json.dumps(easybuild["stage_path_keys"], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
