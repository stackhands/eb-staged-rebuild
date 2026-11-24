"""Parity checks for recovery of a private prefix temporary."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


def _skip_if_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise unittest.SkipTest("root bypasses directory permission checks")


class PrefixDiscardUnlinkParityTests(unittest.TestCase):
    def test_sealed_partial_refuses_before_publication(self) -> None:
        _skip_if_root()
        with tempfile.TemporaryDirectory(prefix="prefix-discard-") as tmp:
            python_result = self._run_case(os.path.join(tmp, "python"), run_cli)
            self.assertEqual(python_result.returncode, 2, python_result.stderr)
            self.assertIn("[Errno 13] Permission denied:", python_result.stderr)
            self.assertIn(".ebsr-partial", python_result.stderr)
            self.assertEqual(python_result.payload, "old\n")
            self.assertFalse(python_result.live_tool)
            self.assertFalse(python_result.live_debris)
            self.assertTrue(python_result.generation_debris)
            self.assertIn("generations", python_result.generation_tool)

            if os.environ.get("TOOL_BIN"):
                zig_result = self._run_case(os.path.join(tmp, "zig"), run_cli_optional_binary)
                self.assertEqual(zig_result.returncode, 2, zig_result.stdout + zig_result.stderr)
                self.assertIn("[Errno 13] Permission denied:", zig_result.stderr)
                self.assertIn(".ebsr-partial", zig_result.stderr)
                self.assertEqual(zig_result.payload, "old\n")
                self.assertFalse(zig_result.live_tool)
                self.assertFalse(zig_result.live_debris)
                self.assertTrue(zig_result.generation_debris)
                self.assertIn("generations", zig_result.generation_tool)

    def _run_case(self, root: str, runner):
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
                'mkdir -p "$sw/demo/1/sealed" "$mods/all/demo"\n'
                "printf 'new\\n' > \"$sw/demo/1/PAYLOAD\"\n"
                "printf '%s\\n' \"$sw/demo/1/bin/python\" > \"$sw/demo/1/tool\"\n"
                "printf 'debris\\n' > \"$sw/demo/1/sealed/.old.ebsr-partial\"\n"
                'chmod 555 "$sw/demo/1/sealed"\n'
                'printf \'local root = "%s/demo/1"\\n\' "$sw" > "$mods/all/demo/1.lua"\n'
            )
        os.chmod(eb, 0o755)
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
            self.assertIsNotNone(result)
            assert result is not None
            generation = os.path.join(state, "generations", "r1", "software", "demo", "1")
            tool = os.path.join(generation, "tool")
            generation_tool = ""
            if os.path.isfile(tool):
                with open(tool, encoding="utf-8") as handle:
                    generation_tool = handle.read()
            with open(os.path.join(live, "PAYLOAD"), encoding="utf-8") as handle:
                payload = handle.read()
            result.payload = payload
            result.live_tool = os.path.lexists(os.path.join(live, "tool"))
            result.live_debris = os.path.lexists(os.path.join(live, "sealed", ".old.ebsr-partial"))
            result.generation_debris = os.path.lexists(os.path.join(generation, "sealed", ".old.ebsr-partial"))
            result.generation_tool = generation_tool
            return result
        finally:
            sealed_dirs = (
                os.path.join(state, "generations", "r1", "software", "demo", "1", "sealed"),
                os.path.join(live, "sealed"),
            )
            for directory in sealed_dirs:
                if os.path.isdir(directory):
                    os.chmod(directory, 0o755)


if __name__ == "__main__":
    unittest.main()
