"""Regression test for non-UTF-8 probe command output."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


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

            py = run_cli("probe", "--json", "--path", root, "--eb", eb)
            self.assertEqual(py.returncode, 0, py.stdout + py.stderr)
            self.assertNotIn("Traceback", py.stderr)
            py_easybuild = json.loads(py.stdout)["easybuild"]

            zig = run_cli_optional_binary("probe", "--json", "--path", root, "--eb", eb)
            if zig is None:
                return
            self.assertEqual(zig.returncode, 0, zig.stdout + zig.stderr)
            zig_easybuild = json.loads(zig.stdout)["easybuild"]
            self.assertEqual(py_easybuild["framework_version"], zig_easybuild["framework_version"])
            self.assertEqual(py_easybuild["stage_path_keys"], zig_easybuild["stage_path_keys"])


if __name__ == "__main__":
    unittest.main()
