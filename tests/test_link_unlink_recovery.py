"""Publish recovery reports a failed removal of a created link.

A directory that stays unwritable is a persistent failure: Python asks for
intervention and leaves the journal publishing. A single injected failure is
retried by the restore, so the original operating-system error is what the
command prints and the journal is left built. The tracer has to be the parent
of the process, which run_cli cannot do.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import _program, _python314, _skip, classic_install, run_cli, run_cli_optional_binary

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")

_FAKE_EB = """#!/usr/bin/env python3
import os
import sys

if "--dry-run" in sys.argv:
    print(" * [R] /tmp/demo-1.eb (module: demo/1)")
    sys.exit(0)

software = next(
    arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--installpath-software=")
)
modules = next(
    arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--installpath-modules=")
)
install = os.path.join(software, "demo", "1")
os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("new\\n")
with open(os.path.join(install, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
    handle.write("name = 'demo'\\n")
module = os.path.join(modules, "all", "demo", "1.lua")
os.makedirs(os.path.dirname(module), exist_ok=True)
with open(module, "w", encoding="utf-8") as handle:
    handle.write(f'local root = "{install}"\\n')
for rel in ("alpha/demo/1.lua", "tools/demo/1.lua"):
    link = os.path.join(modules, rel)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(module, link)
"""


def _build_fixture(root: Path):
    software = root / "software"
    modules = root / "modules"
    state_root = root / "state"
    (software / "demo" / "1" / "easybuild").mkdir(parents=True)
    (software / "demo" / "1" / "PAYLOAD").write_text("old\n", encoding="utf-8")
    (software / "demo" / "1" / "easybuild" / "demo-1.eb").write_text(
        "name = 'demo'\n", encoding="utf-8"
    )
    (modules / "all" / "demo").mkdir(parents=True)
    (modules / "all" / "demo" / "1.lua").write_text(
        f'local root = "{software / "demo" / "1"}"\n', encoding="utf-8"
    )
    fake_eb = root / "fake-eb"
    fake_eb.write_text(_FAKE_EB, encoding="utf-8")
    fake_eb.chmod(0o755)
    return software, modules, state_root, fake_eb


def _publish_run(runner, software, modules, state_root, fake_eb):
    return runner(
        "run",
        "--backend",
        "installpath",
        "--software",
        str(software),
        "--modules",
        str(modules),
        "--state-root",
        str(state_root),
        "--eb",
        str(fake_eb),
        "--run-id",
        "r1",
        "--skip-live-check",
        "--",
        "demo-1.eb",
    )


class LinkUnlinkRecoveryTests(unittest.TestCase):
    def _assert_rollback_restores_on_unlink_error(self, runner) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission checks are not meaningful as root")
        with tempfile.TemporaryDirectory(prefix="rollback-link-unlink-") as tmp:
            root = Path(tmp)
            software, modules, state_root, fake_eb = _build_fixture(root)
            published = _publish_run(runner, software, modules, state_root, fake_eb)
            if published is None:
                self.skipTest("TOOL_BIN is unset")
            self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
            locked_link = modules / "tools" / "demo" / "1.lua"
            locked_link.parent.chmod(0o555)
            try:
                result = runner("rollback", "--state-root", str(state_root), "r1")
            finally:
                locked_link.parent.chmod(0o755)
            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(
                result.stderr,
                f"eb-staged-rebuild: [Errno 13] Permission denied: '{locked_link}'\n",
            )
            journal = json.loads((state_root / "runs" / "r1" / "journal.json").read_text(encoding="utf-8"))
            self.assertEqual(journal["state"], "complete")
            self.assertEqual(journal["message"], "rollback interrupted; published tree restored")
            self.assertEqual((software / "demo" / "1" / "PAYLOAD").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(locked_link.is_symlink())
            self.assertTrue(all(link["created"] is True for link in journal["links_plan"]))

    def _assert_resume_refuses_on_unlink_error(self, runner) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission checks are not meaningful as root")
        with tempfile.TemporaryDirectory(prefix="resume-link-unlink-") as tmp:
            root = Path(tmp)
            software, modules, state_root, fake_eb = _build_fixture(root)
            published = _publish_run(runner, software, modules, state_root, fake_eb)
            if published is None:
                self.skipTest("TOOL_BIN is unset")
            self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
            journal_file = state_root / "runs" / "r1" / "journal.json"
            journal = json.loads(journal_file.read_text(encoding="utf-8"))
            journal["state"] = "publishing"
            journal["message"] = ""
            journal.pop("completed_at", None)
            journal_file.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            locked_link = modules / "tools" / "demo" / "1.lua"
            locked_link.parent.chmod(0o555)
            try:
                result = runner("resume", "--state-root", str(state_root), "r1")
            finally:
                locked_link.parent.chmod(0o755)
            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(
                result.stderr,
                "eb-staged-rebuild: publish failed and the old live tree could not be restored: "
                f"[Errno 13] Permission denied: '{locked_link}'\n",
            )
            after = json.loads(journal_file.read_text(encoding="utf-8"))
            self.assertEqual(after["state"], "publishing")
            by_rel = {link["rel"]: link for link in after["links_plan"]}
            self.assertIs(by_rel["tools/demo/1.lua"]["created"], True)
            self.assertTrue(locked_link.is_symlink())
            self.assertEqual((software / "demo" / "1" / "PAYLOAD").read_text(encoding="utf-8"), "old\n")

    def test_rollback_restores_published_tree_when_link_unlink_fails_python(self) -> None:
        self._assert_rollback_restores_on_unlink_error(run_cli)

    def test_rollback_restores_published_tree_when_link_unlink_fails_zig(self) -> None:
        self._assert_rollback_restores_on_unlink_error(run_cli_optional_binary)

    def test_resume_refuses_when_created_link_unlink_fails_python(self) -> None:
        self._assert_resume_refuses_on_unlink_error(run_cli)

    def test_resume_refuses_when_created_link_unlink_fails_zig(self) -> None:
        self._assert_resume_refuses_on_unlink_error(run_cli_optional_binary)


class LinkRecoveryInjectionTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        self.python = _python314()
        if self.python is None:
            _skip(self, "python3.14 is not installed")
        self.program = _program(self, "eb-fs-fail-link-recovery")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="rec-link-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec {self.python} {_FAKE} "$@"\n')
        os.chmod(eb, stat.S_IRWXU)
        env = {
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
        }
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
        resume_args = ["resume", "--state-root", state, "--lock-timeout", "0", "run1"]
        return root, env, run_args, resume_args

    def _publish_and_rewind(self, argv, root, env, run_args):
        done = subprocess.run(argv + run_args, cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        path = os.path.join(root, "state", "runs", "run1", "journal.json")
        with open(path, encoding="utf-8") as handle:
            journal = json.load(handle)
        journal["state"] = "publishing"
        journal["message"] = ""
        journal.pop("completed_at", None)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _traced(self, argv, root, env, errno, op, nth, needle):
        return subprocess.run(
            [self.program, str(errno), op, str(nth), needle, "--", *argv],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _journal(self, root):
        path = os.path.join(root, "state", "runs", "run1", "journal.json")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _compare(self, errno, op, nth, needle):
        tool = os.environ["TOOL_BIN"]
        py_root, py_env, py_run, py_resume = self._layout()
        zig_root, zig_env, zig_run, zig_resume = self._layout()
        self._publish_and_rewind([self.python, "-m", "eb_staged_rebuild.cli"], py_root, py_env, py_run)
        self._publish_and_rewind([tool], zig_root, zig_env, zig_run)
        py = self._traced(
            [self.python, "-m", "eb_staged_rebuild.cli", *py_resume],
            py_root,
            py_env,
            errno,
            op,
            nth,
            needle,
        )
        zig = self._traced([tool, *zig_resume], zig_root, zig_env, errno, op, nth, needle)
        py_err = py.stderr.replace(py_root, "ROOT")
        zig_err = zig.stderr.replace(zig_root, "ROOT")
        self.assertEqual(py.returncode, zig.returncode, py_err + "\n---\n" + zig_err)
        self.assertEqual(py_err, zig_err)
        py_j = self._journal(py_root)
        zig_j = self._journal(zig_root)
        self.assertEqual(py_j.get("state"), zig_j.get("state"))
        self.assertEqual(py_j.get("message"), zig_j.get("message"))
        self.assertEqual(py_j.get("state"), "built")
        self.assertEqual(py_j.get("message"), "publish interrupted; old live tree restored")
        with open(os.path.join(py_root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            py_payload = handle.read()
        with open(os.path.join(zig_root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
            zig_payload = handle.read()
        self.assertEqual(py_payload, zig_payload)
        self.assertEqual(py_payload, "old\n")
        py_created = [link.get("created") for link in py_j.get("links_plan") or []]
        zig_created = [link.get("created") for link in zig_j.get("links_plan") or []]
        self.assertEqual(py_created, zig_created)
        self.assertEqual(py_created, [False])

    def test_oneshot_unlink_leaves_the_journal_built(self):
        self._compare(5, "unlink", 1, "modules/tools/demo/1.lua")

    def test_oneshot_directory_open_leaves_the_journal_built(self):
        self._compare(5, "open", 1, "modules/tools/demo")

    def test_oneshot_directory_flush_leaves_the_journal_built(self):
        self._compare(5, "fsync", 1, "modules/tools/demo")


if __name__ == "__main__":
    unittest.main()
