"""Resolve output with an empty quoted Tcl root."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class ResolveEmptyTclRootParityTests(unittest.TestCase):
    def test_resolve_empty_quoted_tcl_root_matches_zig(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            module = os.path.join(root, "empty.tcl")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write('set root ""\n')

            zig = run_cli("resolve", module)
            self.assertEqual(zig.returncode, 1, zig.stdout + zig.stderr)
            self.assertEqual(zig.stderr, "")
            doc = json.loads(zig.stdout)
            doc.pop("path", None)
            self.assertNotIn("error", doc)
            self.assertEqual(doc["syntax"], "tcl")
            self.assertEqual(doc["root"], "")
            golden(self, "stdout", scrub(zig.stdout, ROOT=root))


if __name__ == "__main__":
    unittest.main()
