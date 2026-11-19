"""Parity check for resolve output with an empty quoted Tcl root."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class ResolveEmptyTclRootParityTests(unittest.TestCase):
    def test_resolve_empty_quoted_tcl_root_matches_zig(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            module = os.path.join(root, "empty.tcl")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write('set root ""\n')

            py = run_cli("resolve", module)
            self.assertEqual(py.returncode, 1, py.stdout + py.stderr)
            self.assertEqual(py.stderr, "")
            py_doc = json.loads(py.stdout)
            py_doc.pop("path", None)
            self.assertEqual(py_doc["syntax"], "tcl")
            self.assertEqual(py_doc["root"], "")

            zig = run_cli_optional_binary("resolve", module)
            if zig is None:
                return
            self.assertEqual(zig.returncode, 1, zig.stdout + zig.stderr)
            self.assertEqual(zig.stderr, "")
            zig_doc = json.loads(zig.stdout)
            zig_doc.pop("path", None)
            self.assertEqual(py_doc, zig_doc)


if __name__ == "__main__":
    unittest.main()
