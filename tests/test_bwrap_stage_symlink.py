"""A bwrap stage path must stay inside its generation.

Publish, resume, and rollback leave a directory alone when the stage
path reaches it through a symlink. The check follows a trailing slash
on the link and a relative path to the link. Publish also refuses a
live destination whose parent is a symlink to a directory outside the
prefix. Resume of a publishing journal refuses that parent symlink,
and a symlink two directories above the live destination.
Publish refuses when ``state/generations`` is a symlink to a directory
outside the state root, and leaves that directory and the live tree
unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.support import pause_fifo, run_cli, tool_bin, wait_for_pause

_EB = """\
#!/usr/bin/env python3
import json, os, sys

def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)
stage = opt("--bwrap-installpath")
if not stage:
    print("missing --bwrap-installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(stage, exist_ok=True)
outside = os.environ["OUTSIDE"]
softlink = os.path.join(stage, "softlink")
modlink = os.path.join(stage, "modlink")
if not os.path.lexists(softlink):
    os.symlink(os.path.join(outside, "secret-soft"), softlink)
if not os.path.lexists(modlink):
    os.symlink(os.path.join(outside, "secret-mod"), modlink)
if os.environ["STAGE_STYLE"] == "relative":
    software = "softlink/"
    modules = "modlink/"
else:
    software = softlink + "/"
    modules = modlink + "/"
install = os.path.join(softlink, "demo", "1")
lua_dir = os.path.join(modlink, "all", "demo")
os.makedirs(lua_dir, exist_ok=True)
with open(os.path.join(lua_dir, "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % install)
info = {
    "bwrap_cmd": ["bwrap"],
    "bwrap_installpath": stage,
    "bwrap_installpath_software": software,
    "bwrap_installpath_modules": modules,
    "installpath_software": os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"],
    "installpath_modules": os.environ.get("EASYBUILD_INSTALLPATH_MODULES", ""),
    "modules_to_install": ["demo/1"],
}
with open(os.path.join(stage, "bwrap_info.json"), "w", encoding="utf-8") as handle:
    json.dump(info, handle)
"""


class BwrapStageSymlinkTests(unittest.TestCase):
    def _tree(self, style: str) -> tuple[str, str, str, dict[str, str], str | None]:
        root = tempfile.mkdtemp(prefix="bwrap-stage-link-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(state)
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(
            os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f'local root = "{live}"\n')
        os.makedirs(os.path.join(outside, "secret-soft", "demo", "1", "easybuild"))
        os.makedirs(os.path.join(outside, "secret-mod", "all", "demo"))
        secret = os.path.join(outside, "secret-soft", "demo", "1", "PAYLOAD")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("SECRET\n")
        easyconfig = os.path.join(
            outside, "secret-soft", "demo", "1", "easybuild", "demo-1.eb"
        )
        with open(easyconfig, "w", encoding="utf-8") as handle:
            handle.write("name = 'demo'\n")
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        bwrap = os.path.join(bindir, "bwrap")
        with open(bwrap, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(bwrap, 0o755)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(_EB)
        os.chmod(eb, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["OUTSIDE"] = outside
        env["STAGE_STYLE"] = style
        generation = os.path.join(state, "generations", "r1")
        cwd = None
        if style == "relative":
            os.makedirs(generation)
            cwd = generation
        return software, modules, state, env, cwd

    def _remove(self, root: str) -> None:
        for directory, dirs, _files in os.walk(root, followlinks=False):
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IRWXU)
        shutil.rmtree(root)

    def _run(self, style: str) -> None:
        software, modules, state, env, cwd = self._tree(style)
        secret = os.path.join(env["OUTSIDE"], "secret-soft", "demo", "1", "PAYLOAD")
        eb = os.path.abspath(os.path.join(env["OUTSIDE"], os.pardir, "eb"))
        done = run_cli(
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            "demo-1.eb",
            env=env,
            cwd=cwd,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("eb-staged-rebuild: stage software path escapes this run:", detail)
        self.assertNotIn("bwrap stage", detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        resumed = run_cli("resume", "--state-root", state, "r1", env=env, cwd=cwd)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, resumed_detail)
        self.assertIn("generation entry escapes this run", resumed_detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        rolled = run_cli("rollback", "--state-root", state, "r1", env=env)
        rolled_detail = rolled.stdout + rolled.stderr
        self.assertEqual(rolled.returncode, 2, rolled_detail)
        self.assertIn("only a complete run can roll back", rolled_detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")

    def test_trailing_slash_on_a_stage_symlink_is_refused(self) -> None:
        """Regression test."""
        self._run("slash")

    def test_relative_stage_symlink_is_refused(self) -> None:
        """Regression test."""
        self._run("relative")


_CASE_EB = """\
#!/usr/bin/env python3
import json, os, sys

def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)
stage = opt("--bwrap-installpath")
if not stage:
    print("missing --bwrap-installpath", file=sys.stderr)
    raise SystemExit(2)
case = os.environ["CASE"]
modules = os.path.join(stage, "modules")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
if case == "loop":
    loop = os.path.join(stage, "loop")
    if not os.path.lexists(loop):
        os.symlink(loop, loop)
    software = loop
else:
    software = os.path.join(stage, "software")
    os.makedirs(software, exist_ok=True)
    nest = os.environ.get("NEST", "")
    install = os.path.join(software, nest, "demo", "1") if nest else os.path.join(software, "demo", "1")
    if case == "parent":
        link = os.path.join(software, "demo")
        if not os.path.lexists(link):
            os.symlink(os.path.join(os.environ["OUTSIDE"], "demo"), link)
    elif case == "symlink":
        # The install path itself is a symlink. Count builds beside the
        # modules tree: resume deletes modules, and the second build writes
        # through the link so a skipped escape check is visible.
        target = os.path.join(os.environ["OUTSIDE"], "demo", "1")
        count_path = os.path.join(stage, "build-count")
        n = 0
        if os.path.exists(count_path):
            with open(count_path, encoding="utf-8") as handle:
                n = int(handle.read() or "0")
        n += 1
        with open(count_path, "w", encoding="utf-8") as handle:
            handle.write("%d\\n" % n)
        os.makedirs(os.path.dirname(install), exist_ok=True)
        if not os.path.lexists(install):
            os.symlink(target, install)
        if n >= 2:
            with open(os.path.join(target, "FROM_EB"), "w", encoding="utf-8") as handle:
                handle.write("ran\\n")
    else:
        os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
        with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("new\\n")
        with open(os.path.join(install, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
            handle.write("name = 'demo'\\n")
    with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
        handle.write('local root = "%s"\\n' % install)
info = {
    "bwrap_cmd": ["bwrap"],
    "bwrap_installpath": stage,
    "bwrap_installpath_software": software,
    "bwrap_installpath_modules": modules,
    "installpath_software": os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"],
    "installpath_modules": os.environ.get("EASYBUILD_INSTALLPATH_MODULES", ""),
    "modules_to_install": ["demo/1"],
}
with open(os.path.join(stage, "bwrap_info.json"), "w", encoding="utf-8") as handle:
    json.dump(info, handle)
"""

_PREPARE = """\
#!/usr/bin/env python3
import json, os, sys
flag, marker = sys.argv[2], sys.argv[3]
staged = json.load(open(sys.argv[1], encoding="utf-8"))["modules"][0]["staged"]
if not os.path.exists(flag):
    open(flag, "w", encoding="utf-8").write("first\\n")
    raise SystemExit(9)
open(os.path.join(staged, marker), "w", encoding="utf-8").write("touched\\n")
"""


class BwrapStageEscapeTests(unittest.TestCase):
    def _root(self, case: str) -> tuple[str, str, str, str, dict[str, str]]:
        root = tempfile.mkdtemp(prefix="bwrap-stage-escape-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(state)
        os.makedirs(os.path.join(outside, "demo", "1", "easybuild"))
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("SECRET\n")
        with open(
            os.path.join(outside, "demo", "1", "easybuild", "demo-1.eb"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("name = 'demo'\n")
        os.chmod(os.path.join(outside, "demo", "1"), 0o755)
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        bwrap = os.path.join(bindir, "bwrap")
        with open(bwrap, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(bwrap, 0o755)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(_CASE_EB)
        os.chmod(eb, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["OUTSIDE"] = outside
        env["CASE"] = case
        return software, modules, state, outside, env

    def _remove(self, root: str) -> None:
        for directory, dirs, _files in os.walk(root, followlinks=False):
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IRWXU)
        shutil.rmtree(root)

    def _run(self, case: str, *extra: str):
        software, modules, state, outside, env = self._root(case)
        eb = os.path.join(os.path.dirname(outside), "eb")
        done = run_cli(
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            *extra,
            "demo-1.eb",
            env=env,
        )
        return done, software, modules, state, outside, env

    def _journal(self, state: str) -> dict:
        with open(os.path.join(state, "runs", "r1", "journal.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_symlinked_parent_under_stage_software_is_refused(self) -> None:
        """Regression test."""
        done, _software, _modules, state, outside, _env = self._run("parent")
        detail = done.stdout + done.stderr
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        stamp = os.path.join(outside, "demo", "1", ".eb-staged-rebuild-complete")
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("staged installation escapes this run", detail)
        self.assertFalse(os.path.lexists(stamp), detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        self.assertEqual(self._journal(state)["state"], "failed", detail)
        self.assertEqual(os.stat(os.path.join(outside, "demo", "1")).st_mode & 0o777, 0o755)

    def test_resume_of_a_failed_run_refuses_before_saving_the_journal(self) -> None:
        """Regression test."""
        done, _software, _modules, state, _outside, env = self._run("parent")
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        journal_path = os.path.join(state, "runs", "r1", "journal.json")
        with open(journal_path, "rb") as handle:
            before = handle.read()
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, resumed_detail)
        self.assertIn("staged installation escapes this run", resumed_detail)
        with open(journal_path, "rb") as handle:
            self.assertEqual(handle.read(), before, resumed_detail)
        verified = run_cli("resume", "--verify", "--state-root", state, "r1", env=env)
        verified_detail = verified.stdout + verified.stderr
        self.assertEqual(verified.returncode, 2, verified_detail)
        self.assertIn("staged installation escapes this run", verified_detail)
        self.assertNotIn("rebuilding", verified_detail)
        with open(journal_path, "rb") as handle:
            self.assertEqual(handle.read(), before, verified_detail)

    def test_resume_refuses_a_symlink_install_and_non_utf8_module_text(self) -> None:
        """Regression test."""
        done, _software, _modules, state, outside, env = self._run("symlink")
        detail = done.stdout + done.stderr
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        touched = os.path.join(outside, "demo", "1", "FROM_EB")
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("staged installation is not a real directory", detail)
        self.assertEqual(self._journal(state)["state"], "failed", detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        self.assertFalse(os.path.exists(touched), detail)
        journal_path = os.path.join(state, "runs", "r1", "journal.json")
        with open(journal_path, "rb") as handle:
            before = handle.read()
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, resumed_detail)
        self.assertIn("staged installation escapes this run", resumed_detail)
        self.assertFalse(os.path.exists(touched), resumed_detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        with open(journal_path, "rb") as handle:
            self.assertEqual(handle.read(), before, resumed_detail)

        parent, _software, _modules, state2, _outside, env2 = self._run("parent")
        parent_detail = parent.stdout + parent.stderr
        self.assertEqual(parent.returncode, 2, parent_detail)
        lua = os.path.join(self._journal(state2)["generation_root"], "modules", "all", "demo", "1.lua")
        with open(lua, "wb") as handle:
            handle.write(b"\xff\xfe")
        journal_path2 = os.path.join(state2, "runs", "r1", "journal.json")
        with open(journal_path2, "rb") as handle:
            before2 = handle.read()
        bad = run_cli("resume", "--state-root", state2, "r1", env=env2)
        bad_detail = bad.stdout + bad.stderr
        self.assertEqual(bad.returncode, 2, bad_detail)
        self.assertIn("is not UTF-8 text", bad_detail)
        with open(journal_path2, "rb") as handle:
            self.assertEqual(handle.read(), before2, bad_detail)

    def test_stage_resolution_error_fails_closed(self) -> None:
        """Regression test."""
        done, _software, _modules, _state, _outside, _env = self._run("loop")
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("eb-staged-rebuild: stage software path escapes this run:", detail)
        self.assertNotIn("bwrap stage", detail)
        self.assertNotIn("not a real directory", detail)

    def test_prepare_swap_source_refuses_an_escaped_stage(self) -> None:
        """Regression test."""
        software, modules, state, outside, env = self._root("inside")
        root = os.path.dirname(outside)
        flag = os.path.join(root, "prepare-first")
        marker = "PREPARE_TOUCHED"
        prepare = os.path.join(root, "prepare")
        # The tool passes only the context path. The flag and marker are
        # fixed in the program so resume calls the same paths.
        text = _PREPARE.replace("sys.argv[2], sys.argv[3]", "%r, %r" % (flag, marker), 1)
        with open(prepare, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(prepare, 0o755)
        eb = os.path.join(root, "eb")
        done = run_cli(
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--prepare",
            prepare,
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 9, detail)
        self.assertIn("publication preparation exited 9", detail)
        journal = self._journal(state)
        self.assertEqual(journal["state"], "built", detail)
        gen = journal["generation_root"]
        src = os.path.join(gen, "software", "demo")
        dst = os.path.join(outside, "demo")
        if os.path.lexists(dst):
            shutil.rmtree(dst)
        os.rename(src, dst)
        os.symlink(dst, src)
        touched = os.path.join(dst, "1", marker)
        self.assertFalse(os.path.lexists(touched))
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, resumed_detail)
        self.assertIn("staged installation escapes this run", resumed_detail)
        self.assertFalse(os.path.lexists(touched), resumed_detail)

    def test_symlinked_parent_of_the_live_destination_is_refused(self) -> None:
        """Regression test."""
        software, modules, state, outside, env = self._root("inside")
        root = os.path.dirname(outside)
        landing = os.path.join(outside, "landed")
        os.makedirs(landing)
        prepare = os.path.join(root, "prepare")
        # After the plan, replace software/demo with a symlink. The retired
        # name is a child of that directory, so a path rename would follow it.
        text = """\
#!/usr/bin/env python3
import json, os, sys
ctx = json.load(open(sys.argv[1], encoding="utf-8"))
parent = os.path.dirname(ctx["modules"][0]["retired"])
if not os.path.islink(parent):
    os.rename(parent, parent + ".real")
    os.symlink(%s, parent)
""" % repr(landing)
        with open(prepare, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(prepare, 0o755)
        eb = os.path.join(root, "eb")
        done = run_cli(
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--prepare",
            prepare,
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertEqual(os.listdir(landing), [], detail)
        self.assertIn("Not a directory", detail)
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SECRET\n")
        kept = os.path.join(software, "demo.real", "1", "PAYLOAD")
        with open(kept, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old\n")
        staged = self._journal(state)["modules_plan"][0]["staged"]
        self.assertTrue(os.path.isdir(staged), detail)
        with open(os.path.join(staged, "PAYLOAD"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "new\n")

    def _publishing_journal(self, nest: str = ""):
        software, modules, state, outside, env = self._root("inside")
        if nest:
            # software/demo/1 has to stay, because the module name is checked
            # there. The module file names a deeper tree, so a symlink can sit
            # two levels above that live destination and still inside software.
            live = os.path.join(software, nest, "demo", "1")
            os.makedirs(os.path.join(live, "easybuild"))
            with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
                handle.write("old\n")
            with open(os.path.join(live, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
                handle.write("name = 'demo'\n")
            with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{live}"\n')
            env["NEST"] = nest
        root = os.path.dirname(outside)
        ready, fifo = pause_fifo(root)
        paused = dict(env)
        paused["EB_STAGED_REBUILD_PAUSE_AT"] = "swap:all/demo/1.lua.exchange.before"
        paused["EB_STAGED_REBUILD_PAUSE_READY"] = ready
        paused["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
        command = [
            tool_bin(),
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            os.path.join(root, "eb"),
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            "demo-1.eb",
        ]
        proc = subprocess.Popen(command, env=paused, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            reached = wait_for_pause(proc, fifo)
            proc.kill()
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
        self.assertTrue(reached, "run exited before the publishing swap")
        journal = self._journal(state)
        self.assertEqual(journal["state"], "publishing")
        return state, outside, env, journal

    def _payload(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _resume_refuses_when_a_live_ancestor_is_a_symlink(self, levels: int) -> None:
        state, outside, env, journal = self._publishing_journal("nest" if levels > 1 else "")
        plan = journal["modules_plan"][0]
        live = plan["live_software"]
        retired = plan["retired"]
        anchor = live
        for _ in range(levels):
            anchor = os.path.dirname(anchor)
        landing = os.path.join(outside, "landed")
        os.rename(anchor, landing)
        os.symlink(landing, anchor)
        live_payload = os.path.join(landing, os.path.relpath(live, anchor), "PAYLOAD")
        retired_payload = os.path.join(landing, os.path.relpath(retired, anchor), "PAYLOAD")
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        self.assertEqual(self._payload(live_payload), "old\n")
        self.assertEqual(self._payload(retired_payload), "new\n")
        self.assertEqual(self._payload(secret), "SECRET\n")
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, detail)
        self.assertEqual(self._payload(live_payload), "old\n", detail)
        self.assertEqual(self._payload(retired_payload), "new\n", detail)
        self.assertEqual(self._payload(secret), "SECRET\n", detail)
        self.assertTrue(os.path.islink(anchor), detail)

    def test_resume_refuses_a_symlinked_parent_of_the_live_destination(self) -> None:
        """Regression test."""
        self._resume_refuses_when_a_live_ancestor_is_a_symlink(1)

    def test_resume_refuses_a_symlink_two_levels_above_the_live_destination(self) -> None:
        """Regression test."""
        self._resume_refuses_when_a_live_ancestor_is_a_symlink(2)

    def test_symlinked_generations_directory_is_not_published(self) -> None:
        """Regression test."""
        # state/generations is a symlink to a directory outside the state
        # root. A publish that opens the generation in one syscall follows
        # that link and renames the outside tree into the live software tree.
        software, modules, state, outside, env = self._root("inside")
        generations = os.path.join(state, "generations")
        os.symlink(outside, generations)
        root = os.path.dirname(outside)
        done = run_cli(
            "run",
            "--backend",
            "bwrap",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            os.path.join(root, "eb"),
            "--skip-live-check",
            "--run-id",
            "r1",
            "--module",
            "demo/1",
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        live = os.path.join(software, "demo", "1", "PAYLOAD")
        secret = os.path.join(outside, "demo", "1", "PAYLOAD")
        retired = os.path.join(software, "demo", ".1.r1")
        staged = os.path.join(outside, "r1", "software", "demo", "1", "PAYLOAD")
        self.assertEqual(done.returncode, 2, detail)
        self.assertEqual(self._payload(live), "old\n", detail)
        self.assertEqual(self._payload(secret), "SECRET\n", detail)
        self.assertFalse(os.path.lexists(retired), detail)
        self.assertTrue(os.path.isfile(staged), detail)
        self.assertEqual(self._payload(staged), "new\n", detail)
        self.assertTrue(os.path.islink(generations), detail)
        self.assertEqual(os.listdir(os.path.join(software, "demo")), ["1"], detail)


if __name__ == "__main__":
    unittest.main()
