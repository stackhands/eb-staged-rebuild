"""A user attribute the filesystem will not copy stops the prefix rewrite.

The release binary has no fault hook, so ``tests/xattr_enotsup.c`` is a
one-file seccomp shim in front of ``setxattr`` that returns ``ENOTSUP``.
The test mounts no filesystem.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest

from tests.support import _skip

_SHIM_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xattr_enotsup.c")


class PrefixUserXattrParityTests(unittest.TestCase):
    def test_refused_user_xattr_keeps_the_original_file(self) -> None:
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        self._binary(tool)

    def _binary(self, tool: str) -> None:
        with tempfile.TemporaryDirectory(prefix="xattr-bin-") as root:
            live = os.path.join(root, "software", "demo", "1")
            module_dir = os.path.join(root, "modules", "all", "demo")
            generation = os.path.join(root, "generations", "r1")
            kept_dir = os.path.join(generation, "software", "demo", "1")
            os.makedirs(live)
            os.makedirs(module_dir)
            os.makedirs(kept_dir)
            with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            with open(os.path.join(module_dir, "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{live}"\n')
            kept = os.path.join(kept_dir, "kept.txt")
            with open(kept, "w", encoding="utf-8") as handle:
                handle.write(f"prefix={generation}/software/demo/1\n")
            os.setxattr(kept, "user.ebsr", b"kept")
            eb = os.path.join(root, "fake-eb")
            with open(eb, "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        """\
                        #!/bin/sh
                        for arg in "$@"; do
                          case "$arg" in
                            --installpath-software=*) sw="${arg#*=}" ;;
                            --installpath-modules=*) mods="${arg#*=}" ;;
                          esac
                        done
                        mkdir -p "$sw/demo/1" "$mods/all/demo"
                        printf 'new\\n' > "$sw/demo/1/PAYLOAD"
                        printf 'local root = "%s/demo/1"\\n' "$sw" > "$mods/all/demo/1.lua"
                        """
                    )
                )
            os.chmod(eb, 0o755)
            shim = os.path.join(root, "xattr_enotsup")
            compiled = subprocess.run(
                ["cc", "-O2", "-o", shim, _SHIM_SRC],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            done = subprocess.run(
                [
                    shim,
                    tool,
                    "run",
                    "--software",
                    os.path.join(root, "software"),
                    "--modules",
                    os.path.join(root, "modules"),
                    "--state-root",
                    root,
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
                check=False,
                capture_output=True,
                text=True,
            )
            detail = done.stdout + done.stderr
            self.assertEqual(done.returncode, 2, detail)
            self.assertIn("Operation not supported", detail)
            with open(os.path.join(live, "PAYLOAD"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old\n")
            self.assertEqual(os.getxattr(kept, "user.ebsr"), b"kept")

    @staticmethod
    def _rmtree(root: str) -> None:
        for directory, dirs, files in os.walk(root, topdown=False):
            for name in files:
                os.unlink(os.path.join(directory, name))
            for name in dirs:
                os.rmdir(os.path.join(directory, name))
        os.rmdir(root)


if __name__ == "__main__":
    unittest.main()
