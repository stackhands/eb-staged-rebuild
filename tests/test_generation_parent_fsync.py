"""A failed flush of a removed generation's parent stops garbage collection.

The generation directory is removed and its parent is flushed afterwards.
Python raises. The binary ignored the flush, recorded the removal, and
exited 0. The tracer has to be the parent of the release binary.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _python314():
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    found = shutil.which("python3.14")
    if not found:
        return None
    completed = subprocess.run(
        [found, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)"],
        check=False,
    )
    return found if completed.returncode == 0 else None


def _program():
    """Build tests/fs_fail.c. The release binary is static, so the tracer has to be its parent."""
    src = os.path.join(_REPO, "tests", "fs_fail.c")
    out = os.path.join(tempfile.gettempdir(), "eb-fs-fail-n23")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        subprocess.run(["gcc", "-O2", "-o", out, src], check=True)
    return out


class GenerationParentFsyncTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            if os.environ.get("EB_STAGED_REBUILD_REQUIRE_TESTS") == "1":
                self.fail("TOOL_BIN is unset")
            self.skipTest("TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            self.skipTest("python3.14 is not installed")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="gen-fsync-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "state")
        generation = os.path.join(state, "generations", "run1")
        os.makedirs(generation)
        with open(os.path.join(generation, "marker"), "w", encoding="utf-8") as handle:
            handle.write("kept\n")
        run = os.path.join(state, "runs", "run1")
        os.makedirs(run)
        journal = {
            "schema": 2,
            "publication": "directory-swap",
            "run_id": "run1",
            "state": "failed",
            "created_at": 1,
            "updated_at": 1,
            "modules_plan": [],
        }
        with open(os.path.join(run, "journal.json"), "w", encoding="utf-8") as handle:
            json.dump(journal, handle)
        return root, state

    def _journal(self, state):
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_failed_parent_flush_leaves_the_journal_unrecorded(self):
        # The first fsync under the generations directory is the flush of
        # that directory after the settled generation is removed.
        tool = os.environ["TOOL_BIN"]
        program = _program()
        py_root, py_state = self._layout()
        zig_root, zig_state = self._layout()
        args = [
            "gc",
            "--state-root",
            py_state,
            "--include-failed",
            "--grace-hours",
            "0",
            "--lock-timeout",
            "0",
        ]
        zig_args = [
            "gc",
            "--state-root",
            zig_state,
            "--include-failed",
            "--grace-hours",
            "0",
            "--lock-timeout",
            "0",
        ]
        env = {
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
        }
        py = subprocess.run(
            [program, "5", "fsync", "1", "generations", "--", self.python, "-m", "eb_staged_rebuild.cli", *args],
            cwd=py_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        zig = subprocess.run(
            [program, "5", "fsync", "1", "generations", "--", tool, *zig_args],
            cwd=zig_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        detail = py.stderr + "\n---\n" + zig.stderr + zig.stdout
        self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
        self.assertEqual(zig.returncode, py.returncode, detail)
        self.assertIn("Input/output error", py.stderr)
        self.assertIn("Input/output error", zig.stderr)
        self.assertFalse(os.path.exists(os.path.join(py_state, "generations", "run1")))
        self.assertEqual(
            os.path.exists(os.path.join(zig_state, "generations", "run1")),
            os.path.exists(os.path.join(py_state, "generations", "run1")),
        )
        self.assertNotIn("generation_removed_at", self._journal(py_state))
        self.assertEqual("generation_removed_at" in self._journal(zig_state), "generation_removed_at" in self._journal(py_state))


if __name__ == "__main__":
    unittest.main()
