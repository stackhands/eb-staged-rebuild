"""A failed unlink during rollback is an OSError, and the published tree stays."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.support import _program, _python314, _skip, classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


def _scrub(text, root):
    return text.replace(root, "ROOT")


def _payload(root):
    path = os.path.join(root, "software", "demo", "1", "PAYLOAD")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _journal(root, run_id):
    path = os.path.join(root, "state", "runs", run_id, "journal.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _state(root, run_id="run1"):
    return _journal(root, run_id).get("state")


def _links(root, run_id):
    return [(link["created"], link["status"]) for link in _journal(root, run_id)["links_plan"]]


class RollbackUnlinkErrorTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            _skip(self, "python3.14 is not installed")
        self.program = _program(self, "eb-fs-fail-rollback-unlink")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="rb-unlink-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nexec {self.python} {_FAKE} \"$@\"\n")
        os.chmod(eb, stat.S_IRWXU)
        run_args = [
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
            "--run-id",
            "run1",
            "--skip-live-check",
            "--lock-timeout",
            "0",
            "--module",
            "demo/1",
            "--",
            "demo-1.eb",
        ]
        back_args = ["rollback", "--state-root", state, "--lock-timeout", "0", "run1"]
        env = {
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
        }
        return root, env, run_args, back_args

    def _traced(self, argv, root, env, errno, op, nth, needle):
        # run_cli cannot do this: the tracer must be the parent of the tool.
        return subprocess.run(
            [self.program, str(errno), op, str(nth), needle, "--", *argv],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _publish(self, argv, root, env, run_args):
        done = subprocess.run(argv + run_args, cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def _compare(self, errno, op, nth, needle):
        tool = os.environ["TOOL_BIN"]
        py_root, py_env, py_run, py_back = self._layout()
        zig_root, zig_env, zig_run, zig_back = self._layout()
        self._publish([self.python, "-m", "eb_staged_rebuild.cli"], py_root, py_env, py_run)
        self._publish([tool], zig_root, zig_env, zig_run)
        py = self._traced(
            [self.python, "-m", "eb_staged_rebuild.cli", *py_back], py_root, py_env, errno, op, nth, needle
        )
        zig = self._traced([tool, *zig_back], zig_root, zig_env, errno, op, nth, needle)
        py_err = _scrub(py.stderr, py_root)
        zig_err = _scrub(zig.stderr, zig_root)
        self.assertEqual(py.returncode, zig.returncode, py_err + "\n---\n" + zig_err)
        self.assertEqual(py_err, zig_err)
        self.assertEqual(_payload(py_root), _payload(zig_root))
        self.assertEqual(_state(py_root), _state(zig_root))

    def test_unlink_eio_keeps_the_published_tree(self):
        self._compare(5, "unlink", 1, "modules/tools/demo/1.lua")

    def test_link_dir_open_eio_keeps_the_published_tree(self):
        self._compare(5, "open", 1, "modules/tools/demo")

    def test_link_dir_fsync_eio_keeps_the_published_tree(self):
        self._compare(5, "fsync", 1, "modules/tools/demo")

    def _rollback_to(self, argv):
        # run2 creates the class link run1 made and lost; run3 finds it in
        # place. Two runs over run1 keep rollback --to off the one-run path.
        root, env, run_args, _ = self._layout()
        state = os.path.join(root, "state")
        link = os.path.join(root, "modules", "tools", "demo", "1.lua")
        at = run_args.index("--run-id") + 1
        for run_id in ("run1", "run2", "run3"):
            if run_id == "run2":
                os.unlink(link)
            self._publish(argv, root, env, run_args[:at] + [run_id] + run_args[at + 1:])
        self.assertEqual(_links(root, "run2"), [(True, "created")])
        self.assertEqual(_links(root, "run3"), [(False, "present")])
        back = [*argv, "rollback", "--state-root", state, "--lock-timeout", "0", "--to", "run1"]
        failed = self._traced(back, root, env, 5, "fsync", 1, "modules/tools/demo")
        outcome = {
            "rc": failed.returncode,
            "stderr": _scrub(failed.stderr, root),
            "link": os.path.lexists(link),
            "states": [_state(root, run_id) for run_id in ("run2", "run3")],
            "run2_links": _links(root, "run2"),
        }
        again = subprocess.run(back, cwd=root, env=env, capture_output=True, text=True, check=False)
        outcome["again_rc"] = again.returncode
        outcome["final"] = [_state(root, run_id) for run_id in ("run2", "run3")]
        outcome["final_run2_links"] = _links(root, "run2")
        outcome["payload"] = _payload(root)
        return outcome

    def test_rollback_to_link_dir_fsync_eio_keeps_the_run_rolling_back(self):
        py = self._rollback_to([self.python, "-m", "eb_staged_rebuild.cli"])
        zig = self._rollback_to([os.environ["TOOL_BIN"]])
        self.assertEqual(py, zig)
        self.assertEqual(zig["rc"], 2)
        self.assertEqual(zig["stderr"], "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/tools/demo'\n")
        self.assertFalse(zig["link"])
        self.assertEqual(zig["states"], ["rolling_back", "rolled_back"])
        self.assertEqual(zig["run2_links"], [(True, "created")])
        self.assertEqual(zig["again_rc"], 0)
        self.assertEqual(zig["final"], ["rolled_back", "rolled_back"])
        self.assertEqual(zig["final_run2_links"], [(False, "removed")])
        self.assertEqual(zig["payload"], "new\n")
