"""Parity checks for errors removing a completed hard-link replacement."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

from tests.support import run_cli, run_cli_optional_binary


def _skip_if_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise unittest.SkipTest("root bypasses directory permission checks")


class PrefixReadyUnlinkParityTests(unittest.TestCase):
    def test_ready_unlink_error_refuses_before_tree_publication(self) -> None:
        _skip_if_root()
        with tempfile.TemporaryDirectory(prefix="prefix-ready-unlink-") as tmp:
            python_result = self._run_case(tmp, run_cli)
            self.assertEqual(python_result.returncode, 2, python_result.stderr)
            self.assertIn("Permission denied", python_result.stderr)
            self.assertIn(".ebsr-ready", python_result.stderr)

            binary = run_cli_optional_binary
            if os.environ.get("TOOL_BIN"):
                zig_result = self._run_case(tmp, binary)
                self.assertIsNotNone(zig_result)
                assert zig_result is not None
                self.assertEqual(zig_result.returncode, 2, zig_result.stderr)
                self.assertIn("Permission denied", zig_result.stderr)
                self.assertIn(".ebsr-ready", zig_result.stderr)

    def _run_case(self, tmp: str, runner):
        root = os.path.join(tmp, runner.__name__)
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

        source = os.path.join(state, "generations", "r1", "software", "demo", "1", "a")
        parent = os.path.dirname(source)
        changed = threading.Event()

        def deny_ready_unlink() -> None:
            old_inode = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    inode = os.stat(source).st_ino
                except FileNotFoundError:
                    time.sleep(0.0001)
                    continue
                if old_inode is None:
                    old_inode = inode
                elif inode != old_inode:
                    os.chmod(parent, 0o555)
                    changed.set()
                    return
                time.sleep(0.0001)

        watcher = threading.Thread(target=deny_ready_unlink, daemon=True)
        watcher.start()
        args = (
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
        )
        try:
            result = runner(*args, env={"PYTHONIOENCODING": "utf-8:strict"})
            watcher.join(1)
            self.assertTrue(changed.is_set(), "the ready file was not reached")
            self.assertIn("running", result.stdout)
            with open(os.path.join(live, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old\n")
            self.assertFalse(os.path.lexists(os.path.join(live, "a")))
            ready = [
                path
                for path, _dirs, files in os.walk(parent)
                for name in files
                if name.endswith(".ebsr-ready")
                for path in [os.path.join(parent, name)]
            ]
            self.assertEqual(len(ready), 1)
            return result
        finally:
            watcher.join(1)
            for directory, _dirs, _files in os.walk(root, topdown=False):
                os.chmod(directory, 0o755)


if __name__ == "__main__":
    unittest.main()
