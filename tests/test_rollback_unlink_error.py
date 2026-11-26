"""A failed unlink during rollback is an OSError, and the published tree stays."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import _program, _skip, classic_install, golden, scrub

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")


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
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE}" "$@"\n')
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

    def _compare(self, errno, op, nth, needle, replaced=False):
        # With `replaced`, another symlink takes the place of run1's class
        # link before the rollback, and a clean rollback runs afterwards.
        tool = os.environ["TOOL_BIN"]
        root, env, run, back = self._layout()
        self._publish([tool], root, env, run)
        if replaced:
            link = os.path.join(root, "modules", "tools", "demo", "1.lua")
            os.unlink(link)
            os.symlink("elsewhere.lua", link)
        zig = self._traced([tool, *back], root, env, errno, op, nth, needle)
        zig_err = scrub(zig.stderr, ROOT=root)
        if not replaced:
            golden(self, "returncode", f"{zig.returncode}\n")
            golden(self, "stderr", zig_err)
            golden(self, "payload", _payload(root))
            golden(self, "state", f"{_state(root)}\n")
            return None
        link = os.path.join(root, "modules", "tools", "demo", "1.lua")
        outcome = {
            "rc": zig.returncode,
            "stderr": zig_err,
            "state": _state(root),
            "message": _journal(root, "run1").get("message"),
            "links": _links(root, "run1"),
            "link": os.readlink(link),
            "payload": _payload(root),
        }
        again = subprocess.run([tool] + back, cwd=root, env=env, capture_output=True, text=True, check=False)
        outcome["again_rc"] = again.returncode
        outcome["final"] = _state(root)
        outcome["final_links"] = _links(root, "run1")
        outcome["final_link"] = os.readlink(link)
        outcome["final_payload"] = _payload(root)
        return outcome

    def test_unlink_eio_keeps_the_published_tree(self):
        self._compare(5, "unlink", 1, "modules/tools/demo/1.lua")

    def test_link_dir_open_eio_keeps_the_published_tree(self):
        self._compare(5, "open", 1, "modules/tools/demo")

    def test_link_dir_fsync_eio_keeps_the_published_tree(self):
        self._compare(5, "fsync", 1, "modules/tools/demo")

    def test_link_dir_fsync_eio_with_a_replaced_link_keeps_the_published_tree(self):
        # The entry that replaced the link is kept, and its directory is
        # still flushed before the link is recorded as removed.
        zig = self._compare(5, "fsync", 1, "modules/tools/demo", replaced=True)
        self.assertEqual(zig["rc"], 2)
        self.assertEqual(zig["stderr"], "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/tools/demo'\n")
        self.assertEqual(zig["state"], "complete")
        self.assertEqual(zig["message"], "rollback interrupted; published tree restored")
        self.assertEqual(zig["links"], [(True, "kept")])
        self.assertEqual(zig["link"], "elsewhere.lua")
        self.assertEqual(zig["payload"], "new\n")
        self.assertEqual(zig["again_rc"], 0)
        self.assertEqual(zig["final"], "rolled_back")
        self.assertEqual(zig["final_links"], [(False, "removed")])
        self.assertEqual(zig["final_link"], "elsewhere.lua")
        self.assertEqual(zig["final_payload"], "old\n")

    def _rollback_to(self, argv, retry_fsync=False, replaced=False, module_gone=False):
        # run2 creates the class link run1 made and lost; run3 finds it in
        # place. Two runs over run1 keep rollback --to off the one-run path.
        # With `replaced`, another symlink takes the link's place afterwards.
        # With `module_gone`, run2 also creates the module file run1 lost, and
        # the injected flush failure is on the module directory.
        root, env, run_args, _ = self._layout()
        state = os.path.join(root, "state")
        link = os.path.join(root, "modules", "tools", "demo", "1.lua")
        module = os.path.join(root, "modules", "all", "demo", "1.lua")
        needle = "modules/all/demo" if module_gone else "modules/tools/demo"
        at = run_args.index("--run-id") + 1
        for run_id in ("run1", "run2", "run3"):
            if run_id == "run2":
                os.unlink(link)
                if module_gone:
                    os.unlink(module)
            self._publish(argv, root, env, run_args[:at] + [run_id] + run_args[at + 1:])
        self.assertEqual(_links(root, "run2"), [(True, "created")])
        self.assertEqual(_links(root, "run3"), [(False, "present")])
        if replaced:
            os.unlink(link)
            os.symlink("elsewhere.lua", link)
        back = [*argv, "rollback", "--state-root", state, "--lock-timeout", "0", "--to", "run1"]
        failed = self._traced(back, root, env, 5, "fsync", 1, needle)
        outcome = {
            "rc": failed.returncode,
            "stderr": scrub(failed.stderr, ROOT=root),
            "link": scrub(os.readlink(link), ROOT=root) if os.path.islink(link) else os.path.lexists(link),
            "module": os.path.lexists(module),
            "states": [_state(root, run_id) for run_id in ("run2", "run3")],
            "run2_links": _links(root, "run2"),
        }
        if retry_fsync:
            retry = self._traced(back, root, env, 5, "fsync", 1, needle)
            outcome["retry_rc"] = retry.returncode
            outcome["retry_stderr"] = scrub(retry.stderr, ROOT=root)
            outcome["retry_states"] = [_state(root, run_id) for run_id in ("run2", "run3")]
            outcome["retry_run2_links"] = _links(root, "run2")
        again = subprocess.run(back, cwd=root, env=env, capture_output=True, text=True, check=False)
        outcome["again_rc"] = again.returncode
        outcome["final"] = [_state(root, run_id) for run_id in ("run2", "run3")]
        outcome["final_run2_links"] = _links(root, "run2")
        outcome["final_link"] = scrub(os.readlink(link), ROOT=root) if os.path.islink(link) else os.path.lexists(link)
        outcome["final_module"] = os.path.lexists(module)
        outcome["payload"] = _payload(root)
        return outcome

    def test_rollback_to_link_dir_fsync_eio_keeps_the_run_rolling_back(self):
        # The link is flushed away whether rollback --to unlinks it or finds
        # another entry in its place, which it keeps.
        for replaced, link in ((False, False), (True, "elsewhere.lua")):
            with self.subTest(replaced=replaced):
                zig = self._rollback_to([os.environ["TOOL_BIN"]], replaced=replaced)
                golden(self, f"outcome-replaced-{replaced}", json.dumps(zig, indent=2, sort_keys=True) + "\n")
                self.assertEqual(zig["rc"], 2)
                self.assertEqual(
                    zig["stderr"],
                    "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/tools/demo'\n",
                )
                self.assertEqual(zig["link"], link)
                self.assertEqual(zig["states"], ["rolling_back", "rolled_back"])
                self.assertEqual(zig["run2_links"], [(True, "created")])
                self.assertEqual(zig["again_rc"], 0)
                self.assertEqual(zig["final"], ["rolled_back", "rolled_back"])
                self.assertEqual(zig["final_run2_links"], [(False, "removed")])
                self.assertEqual(zig["final_link"], link)
                self.assertEqual(zig["payload"], "new\n")

    def test_rollback_to_missing_module_is_flushed_again(self):
        # run2 created the module file, so rollback --to removes it. Its
        # directory flush fails twice; the retry that finds the file gone
        # still flushes before the runs are rolled back.
        zig = self._rollback_to([os.environ["TOOL_BIN"]], retry_fsync=True, module_gone=True)
        golden(self, "outcome", json.dumps(zig, indent=2, sort_keys=True) + "\n")
        self.assertEqual(zig["rc"], 2)
        self.assertEqual(zig["stderr"], "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/all/demo'\n")
        self.assertFalse(zig["module"])
        self.assertEqual(zig["states"], ["rolling_back", "rolling_back"])
        self.assertEqual(zig["retry_rc"], 2)
        self.assertEqual(
            zig["retry_stderr"],
            "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/all/demo'\n",
        )
        self.assertEqual(zig["retry_states"], ["rolling_back", "rolling_back"])
        self.assertEqual(zig["retry_run2_links"], [(True, "created")])
        self.assertEqual(zig["again_rc"], 0)
        self.assertEqual(zig["final"], ["rolled_back", "rolled_back"])
        self.assertEqual(zig["final_run2_links"], [(False, "removed")])
        self.assertFalse(zig["final_module"])
        self.assertEqual(zig["payload"], "new\n")

    def test_rollback_to_missing_link_is_flushed_again(self):
        zig = self._rollback_to([os.environ["TOOL_BIN"]], retry_fsync=True)
        golden(self, "outcome", json.dumps(zig, indent=2, sort_keys=True) + "\n")
        self.assertEqual(zig["retry_rc"], 2)
        self.assertEqual(
            zig["retry_stderr"],
            "eb-staged-rebuild: [Errno 5] Input/output error: 'ROOT/modules/tools/demo'\n",
        )
        self.assertEqual(zig["retry_states"], ["rolling_back", "rolled_back"])
        self.assertEqual(zig["retry_run2_links"], [(True, "created")])
        self.assertEqual(zig["again_rc"], 0)
        self.assertEqual(zig["final"], ["rolled_back", "rolled_back"])
