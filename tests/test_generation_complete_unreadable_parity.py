"""CLI parity when a generation's easybuild metadata directory is unreadable."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary

_FAKE_EB = """#!/usr/bin/env python3
import os
import sys

if "--dry-run" in sys.argv:
    sys.exit(0)

sw = os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"]
mods = os.environ["EASYBUILD_INSTALLPATH_MODULES"]
root = os.path.join(sw, "demo", "1")
meta = os.path.join(root, "easybuild")
os.makedirs(meta, exist_ok=True)
with open(os.path.join(meta, "demo-1.eb"), "w", encoding="utf-8") as handle:
    handle.write("name = 'demo'\\n")
mod_dir = os.path.join(mods, "demo")
os.makedirs(mod_dir, exist_ok=True)
with open(os.path.join(mod_dir, "1.lua"), "w", encoding="utf-8") as handle:
    handle.write(f'local root = "{root}"\\n')
os.chmod(meta, 0)
"""


def _run_unreadable_staged_case(testcase: unittest.TestCase, runner) -> None:
    with tempfile.TemporaryDirectory(prefix="run-unreadable-meta-") as tmp:
        software = os.path.join(tmp, "software")
        modules = os.path.join(tmp, "modules", "all")
        state = os.path.join(tmp, "state")
        live_root = os.path.join(software, "demo", "1")
        os.makedirs(live_root)
        with open(os.path.join(live_root, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        os.makedirs(os.path.join(modules, "demo"))
        with open(os.path.join(modules, "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live_root}"\n')
        fake_eb = os.path.join(tmp, "fake-eb")
        with open(fake_eb, "w", encoding="utf-8") as handle:
            handle.write(_FAKE_EB)
        os.chmod(fake_eb, 0o755)

        staged = os.path.join(state, "generations", "unreadable-meta", "software", "demo", "1")
        meta = os.path.join(staged, "easybuild")
        try:
            done = runner(
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
                fake_eb,
                "--run-id",
                "unreadable-meta",
                "--module",
                "demo/1",
                "--skip-live-check",
                "--",
                "demo-1.eb",
            )
            journal_path = os.path.join(state, "runs", "unreadable-meta", "journal.json")
            with open(journal_path, encoding="utf-8") as handle:
                journal = json.load(handle)
        finally:
            if os.path.exists(meta):
                os.chmod(meta, 0o700)

        expected = f"{staged} is not a complete staged installation"
        testcase.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        testcase.assertEqual(done.stderr, "")
        testcase.assertIn(expected, done.stdout)
        testcase.assertEqual(journal["state"], "failed")
        testcase.assertEqual(journal["message"], expected)


class UnreadableEasybuildMetadataParityTests(unittest.TestCase):
    def test_resolve_treats_unreadable_easybuild_dir_as_incomplete(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        with tempfile.TemporaryDirectory(prefix="resolve-unreadable-meta-") as tmp:
            root = os.path.join(tmp, "software", "demo", "1")
            meta = os.path.join(root, "easybuild")
            os.makedirs(meta)
            with open(os.path.join(meta, "demo-1.eb"), "w", encoding="utf-8") as handle:
                handle.write("name = 'demo'\n")
            module = os.path.join(tmp, "demo-1.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{root}"\n')

            os.chmod(meta, 0)
            try:
                py_done = run_cli("resolve", module)
                zig_done = run_cli_optional_binary("resolve", module)
            finally:
                os.chmod(meta, 0o700)

            self.assertEqual(py_done.returncode, 1, py_done.stdout + py_done.stderr)
            self.assertEqual(py_done.stderr, "")
            self.assertEqual(
                json.loads(py_done.stdout),
                {
                    "complete": False,
                    "kind": "file",
                    "path": module,
                    "root": root,
                    "root_kind": "dir",
                    "syntax": "lua",
                },
            )
            if zig_done is not None:
                self.assertEqual(zig_done.returncode, 1, zig_done.stdout + zig_done.stderr)
                self.assertEqual(py_done.stdout, zig_done.stdout)
                self.assertEqual(py_done.stderr, zig_done.stderr)

    def test_run_fails_journal_when_staged_easybuild_dir_is_unreadable(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        _run_unreadable_staged_case(self, run_cli)
        if run_cli_optional_binary("--help") is not None:
            _run_unreadable_staged_case(self, run_cli_optional_binary)


if __name__ == "__main__":
    unittest.main()
