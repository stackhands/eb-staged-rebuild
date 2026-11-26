"""A failed unlink of a finished hard-link replacement refuses publication.

The failure is the test artifact's prefix.ready.unlink step, which the
release binary does not read; TOOL_BIN_TEST_OPS names the test artifact.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import _run, _skip

_STEP = "prefix.ready.unlink"


class PrefixReadyUnlinkParityTests(unittest.TestCase):
    def test_ready_unlink_error_refuses_before_tree_publication(self) -> None:
        tool = os.environ.get("TOOL_BIN_TEST_OPS")
        if not tool:
            _skip(self, "TOOL_BIN_TEST_OPS is unset")
        self._run_one([tool])

    def _run_one(self, argv: list[str]) -> None:
        with tempfile.TemporaryDirectory(prefix="prefix-ready-unlink-") as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            live = os.path.join(software, "demo", "1")
            os.makedirs(live, exist_ok=True)
            with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            module_dir = os.path.join(modules, "all", "demo")
            os.makedirs(module_dir, exist_ok=True)
            with open(os.path.join(module_dir, "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{live}"\n')
            eb = os.path.join(root, "fake-eb")
            with open(eb, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    'for arg in "$@"; do\n'
                    '  case "$arg" in\n'
                    '    --installpath-software=*) sw="${arg#*=}" ;;\n'
                    '    --installpath-modules=*) mods="${arg#*=}" ;;\n'
                    "  esac\n"
                    "done\n"
                    'mkdir -p "$sw/demo/1/sub" "$mods/all/demo"\n'
                    "printf 'new\\n' > \"$sw/demo/1/PAYLOAD\"\n"
                    "printf '%s\\n' \"$sw/demo/1/bin/python\" > \"$sw/demo/1/a\"\n"
                    'ln "$sw/demo/1/a" "$sw/demo/1/sub/z"\n'
                    'printf \'local root = "%s/demo/1"\\n\' "$sw" > "$mods/all/demo/1.lua"\n'
                )
            os.chmod(eb, 0o755)
            result = _run(
                [
                    *argv,
                    "run",
                    "--software",
                    software,
                    "--modules",
                    modules,
                    "--state-root",
                    state,
                    "--backend",
                    "installpath",
                    "--eb",
                    eb,
                    "--module",
                    "demo/1",
                    "--run-id",
                    "r1",
                    "--",
                    "demo-1.eb",
                ],
                {"EB_STAGED_REBUILD_FAIL_AT": _STEP},
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("Permission denied", result.stderr)
            self.assertIn(".ebsr-ready", result.stderr)
            self.assertIn("running", result.stdout)
            with open(os.path.join(live, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old\n")
            self.assertFalse(os.path.lexists(os.path.join(live, "a")))
            parent = os.path.join(state, "generations", "r1", "software", "demo", "1")
            ready = [
                os.path.join(dirpath, name)
                for dirpath, _dirs, files in os.walk(parent)
                for name in files
                if name.endswith(".ebsr-ready")
            ]
            self.assertEqual(len(ready), 1)
