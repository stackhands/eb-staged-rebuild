"""Parity when a generation's easybuild directory cannot be listed."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary

_STRICT = {"PYTHONIOENCODING": "utf-8:strict"}


def _module(path: str, root: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f'local root = "{root}"\n')


class GenerationEasybuildListParityTests(unittest.TestCase):
    def test_resolve_reports_an_unreadable_easybuild_directory(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        with tempfile.TemporaryDirectory(prefix="gen-eb-resolve-") as tmp:
            install = os.path.join(tmp, "demo", "1")
            meta = os.path.join(install, "easybuild")
            os.makedirs(meta)
            with open(os.path.join(meta, "demo-1.eb"), "w", encoding="utf-8") as handle:
                handle.write("name = 'demo'\n")
            module = os.path.join(tmp, "modules", "demo", "1.lua")
            _module(module, install)
            readable = run_cli("resolve", module, env=_STRICT)
            self.assertEqual(readable.returncode, 0, readable.stdout + readable.stderr)
            self.assertIn('"complete": true', readable.stdout)
            binary = run_cli_optional_binary("resolve", module, env=_STRICT)
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(readable.stdout, binary.stdout)
            os.chmod(meta, 0)
            try:
                expected = f"[Errno 13] Permission denied: '{meta}'"
                done = run_cli("resolve", module, env=_STRICT)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn(expected, done.stderr)
                other = run_cli_optional_binary("resolve", module, env=_STRICT)
                if other is not None:
                    self.assertEqual(other.returncode, 2, other.stdout + other.stderr)
                    self.assertEqual(other.stdout, "")
                    self.assertIn(expected, other.stderr)
                    self.assertEqual(done.stderr, other.stderr)
            finally:
                os.chmod(meta, 0o755)

    def _run_case(self, runner) -> None:
        with tempfile.TemporaryDirectory(prefix="gen-eb-run-") as tmp:
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            state = os.path.join(tmp, "state")
            os.makedirs(os.path.join(software, "demo", "1"))
            os.makedirs(modules)
            eb = os.path.join(tmp, "eb")
            with open(eb, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    "software= modules=\n"
                    'for arg in "$@"; do\n'
                    '  case "$arg" in\n'
                    "    --installpath-software=*) software=${arg#--installpath-software=} ;;\n"
                    "    --installpath-modules=*) modules=${arg#--installpath-modules=} ;;\n"
                    "  esac\n"
                    "done\n"
                    'inst="$software/demo/1"\n'
                    'mkdir -p "$inst/easybuild" "$(dirname "$modules/all/demo/1.lua")"\n'
                    "printf 'name = \"demo\"\\n' > \"$inst/easybuild/demo.eb\"\n"
                    "printf 'local root = \"%s\"\\n' \"$inst\" > \"$modules/all/demo/1.lua\"\n"
                    'chmod 000 "$inst/easybuild"\n'
                )
            os.chmod(eb, 0o755)
            meta = os.path.join(state, "generations", "r1", "software", "demo", "1", "easybuild")
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
                "--skip-live-check",
                "--",
                "demo.eb",
            )
            expected = f"[Errno 13] Permission denied: '{meta}'"
            try:
                done = runner(*args, env=_STRICT)
                self.assertIsNotNone(done)
                assert done is not None
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(expected, done.stderr)
                self.assertNotIn("not a complete staged installation", done.stdout + done.stderr)
                with open(os.path.join(state, "runs", "r1", "journal.json"), encoding="utf-8") as handle:
                    journal = json.load(handle)
                self.assertEqual(journal["state"], "building", done.stdout + done.stderr)
                self.assertEqual(journal["message"], "")
            finally:
                if os.path.isdir(meta):
                    os.chmod(meta, stat.S_IRWXU)

    def test_run_reports_an_unreadable_easybuild_directory(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        self._run_case(run_cli)
        if run_cli_optional_binary("--help") is not None:
            self._run_case(run_cli_optional_binary)
