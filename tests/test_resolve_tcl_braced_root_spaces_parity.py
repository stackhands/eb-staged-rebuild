"""Resolve output with a braced Tcl root containing spaces."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class ResolveTclBracedRootSpacesParityTests(unittest.TestCase):
    def test_resolve_braced_tcl_root_with_spaces_matches_zig(self) -> None:
        with tempfile.TemporaryDirectory(prefix="resolve-tcl-braced-") as root:
            install = os.path.join(root, "space dir", "pkg", "1.0")
            os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
            with open(os.path.join(install, "easybuild", "x.eb"), "w", encoding="utf-8") as handle:
                handle.write("name = 'x'\n")
            module = os.path.join(root, "demo.tcl")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f"set root {{{install}}}\n")

            zig = run_cli("resolve", module)
            self.assertEqual(zig.returncode, 0, zig.stdout + zig.stderr)
            self.assertEqual(zig.stderr, "")
            doc = json.loads(zig.stdout)
            self.assertEqual(doc["syntax"], "tcl")
            self.assertEqual(doc["root"], install)
            self.assertEqual(doc["root_kind"], "dir")
            self.assertTrue(doc["complete"])
            golden(self, "stdout", scrub(zig.stdout, ROOT=root))


if __name__ == "__main__":
    unittest.main()
