"""CLI behaviour when a staged module file is not UTF-8 text or cannot be read."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli

_NON_UTF8_EB = """#!/usr/bin/env python3
import os
import sys

if "--dry-run" in sys.argv:
    sys.exit(0)

sw = os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"]
mods = os.environ["EASYBUILD_INSTALLPATH_MODULES"]
root = os.path.join(sw, "demo", "1")
os.makedirs(root, exist_ok=True)
with open(os.path.join(root, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("new\\n")
mod_dir = os.path.join(mods, "demo")
os.makedirs(mod_dir, exist_ok=True)
with open(os.path.join(mod_dir, "1.lua"), "wb") as handle:
    handle.write(f'local root = "{root}"\\n-- '.encode("utf-8") + b"\\xff\\n")
"""

_UNREADABLE_EB = """#!/usr/bin/env python3
import os
import sys

if "--dry-run" in sys.argv:
    sys.exit(0)

sw = os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"]
mods = os.environ["EASYBUILD_INSTALLPATH_MODULES"]
root = os.path.join(sw, "demo", "1")
os.makedirs(root, exist_ok=True)
with open(os.path.join(root, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("new\\n")
mod_dir = os.path.join(mods, "demo")
os.makedirs(mod_dir, exist_ok=True)
mod_path = os.path.join(mod_dir, "1.lua")
with open(mod_path, "w", encoding="utf-8") as handle:
    handle.write(f'local root = "{root}"\\n')
os.chmod(mod_path, 0)
"""


def _setup_live_tree(tmp: str, fake_eb_source: str) -> tuple[str, str, str, str, str]:
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
        handle.write(fake_eb_source)
    os.chmod(fake_eb, 0o755)
    return software, modules, state, live_root, fake_eb


def _run_staged_module_case(
    testcase: unittest.TestCase,
    fake_eb_source: str,
    expected_message_fn,
) -> None:
    with tempfile.TemporaryDirectory(prefix="staged-module-read-") as tmp:
        software, modules, state, live_root, fake_eb = _setup_live_tree(tmp, fake_eb_source)
        staged_mod = os.path.join(state, "generations", "r1", "modules", "demo", "1.lua")
        try:
            done = run_cli(
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
                "r1",
                "--module",
                "demo/1",
                "--skip-live-check",
                "--",
                "demo-1.eb",
            )
            journal_path = os.path.join(state, "runs", "r1", "journal.json")
            with open(journal_path, encoding="utf-8") as handle:
                journal = json.load(handle)
            gc_done = run_cli("gc", "--state-root", state, "--dry-run")
        finally:
            if os.path.exists(staged_mod):
                os.chmod(staged_mod, 0o600)

        expected = expected_message_fn(staged_mod)
        testcase.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        testcase.assertEqual(done.stderr, "")
        testcase.assertNotIn("Traceback", done.stdout + done.stderr)
        testcase.assertIn(f"eb-staged-rebuild: {expected}", done.stdout)
        testcase.assertEqual(journal["state"], "failed")
        testcase.assertEqual(journal["message"], expected)
        with open(os.path.join(live_root, "PAYLOAD"), encoding="utf-8") as handle:
            testcase.assertEqual(handle.read(), "old\n")
        testcase.assertEqual(gc_done.returncode, 0, gc_done.stdout + gc_done.stderr)
        testcase.assertEqual(gc_done.stderr, "")


class StagedModuleReadParityTests(unittest.TestCase):
    def test_python_refuses_non_utf8_staged_module_file(self) -> None:
        _run_staged_module_case(
            self,
            _NON_UTF8_EB,
            lambda path: f"{path} is not UTF-8 text",
        )

    def test_binary_refuses_non_utf8_staged_module_file(self) -> None:
        _run_staged_module_case(
            self,
            _NON_UTF8_EB,
            lambda path: f"{path} is not UTF-8 text",
        )

    def test_python_refuses_unreadable_staged_module_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file read permissions")
        _run_staged_module_case(
            self,
            _UNREADABLE_EB,
            lambda path: f"cannot read staged module file {path}: [Errno 13] Permission denied: '{path}'",
        )

    def test_binary_refuses_unreadable_staged_module_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file read permissions")
        _run_staged_module_case(
            self,
            _UNREADABLE_EB,
            lambda path: f"cannot read staged module file {path}: [Errno 13] Permission denied: '{path}'",
        )


if __name__ == "__main__":
    unittest.main()
