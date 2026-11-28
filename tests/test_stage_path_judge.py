"""The run judges the generation before a log, a journal save, or eb.

A symlink or a special file that is a child of the generation is refused.
software and modules must be directories. A component of a recorded stage
path, from the generation through the install directory, is refused when
it is a symlink or not a directory. A link inside an install or a module
tree is left. An absent path, whose existing ancestors are real
directories, may rebuild.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import tempfile
import unittest

from tests.support import Site, run_cli, tool_bin

_VERSION = """\
if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)
"""

_INSIDE_LINK = """\
#!/usr/bin/env python3
import json, os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

stage = opt("--bwrap-installpath")
if not stage:
    print("missing --bwrap-installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(stage, exist_ok=True)
count_path = os.path.join(stage, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
software = os.path.join(stage, "software")
os.makedirs(software, exist_ok=True)
kept = os.path.join(software, "kept", "1")
os.makedirs(os.path.join(kept, "easybuild"), exist_ok=True)
install = os.path.join(software, "demo", "1")
os.makedirs(os.path.dirname(install), exist_ok=True)
if not os.path.lexists(install):
    os.symlink(kept, install)
if n >= 2:
    with open(os.path.join(os.environ["OUTSIDE"], "FROM_EB"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
    with open(os.path.join(kept, "THROUGH"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
modules = os.path.join(stage, "modules")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % install)
info = {
    "bwrap_cmd": ["bwrap"],
    "bwrap_installpath": stage,
    "bwrap_installpath_software": software,
    "bwrap_installpath_modules": modules,
}
with open(os.path.join(stage, "bwrap_info.json"), "w", encoding="utf-8") as handle:
    json.dump(info, handle)
raise SystemExit(3)
"""

_MISSING_FINAL = """\
#!/usr/bin/env python3
import json, os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

stage = opt("--bwrap-installpath")
if not stage:
    print("missing --bwrap-installpath", file=sys.stderr)
    raise SystemExit(2)
software = os.path.join(stage, "software")
os.makedirs(software, exist_ok=True)
link = os.path.join(software, "demo")
outside = os.path.join(os.environ["OUTSIDE"], "demo")
os.makedirs(outside, exist_ok=True)
if not os.path.lexists(link):
    os.symlink(outside, link)
install = os.path.join(link, "1")
os.makedirs(install, exist_ok=True)
with open(os.path.join(install, "FROM_EB"), "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
modules = os.path.join(stage, "modules")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % os.path.join(software, "demo", "1"))
info = {
    "bwrap_cmd": ["bwrap"],
    "bwrap_installpath": stage,
    "bwrap_installpath_software": software,
    "bwrap_installpath_modules": modules,
}
with open(os.path.join(stage, "bwrap_info.json"), "w", encoding="utf-8") as handle:
    json.dump(info, handle)
raise SystemExit(3)
"""

_LIVE_ROOT = """\
#!/usr/bin/env python3
import json, os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

stage = opt("--bwrap-installpath")
if not stage:
    print("missing --bwrap-installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(stage, exist_ok=True)
count_path = os.path.join(stage, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
software = os.path.join(stage, "software")
os.makedirs(software, exist_ok=True)
live = os.path.join(os.environ["EASYBUILD_INSTALLPATH_SOFTWARE"], "demo", "1")
modules = os.path.join(stage, "modules")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % live)
if n >= 2:
    rebuilt = os.path.join(software, "demo", "1")
    os.makedirs(os.path.join(rebuilt, "easybuild"), exist_ok=True)
    with open(os.path.join(rebuilt, "PAYLOAD"), "w", encoding="utf-8") as handle:
        handle.write("rebuilt\\n")
    with open(os.path.join(rebuilt, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
        handle.write("name = 'demo'\\n")
    with open(os.path.join(software, "REBUILT"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
info = {
    "bwrap_cmd": ["bwrap"],
    "bwrap_installpath": stage,
    "bwrap_installpath_software": software,
    "bwrap_installpath_modules": modules,
}
with open(os.path.join(stage, "bwrap_info.json"), "w", encoding="utf-8") as handle:
    json.dump(info, handle)
raise SystemExit(0 if n >= 2 else 3)
"""

_INSTALLPATH = """\
#!/usr/bin/env python3
import os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

software = opt("--installpath-software")
modules = opt("--installpath-modules")
if not software or not modules:
    print("missing installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(software, exist_ok=True)
os.makedirs(modules, exist_ok=True)
with open(os.path.join(software, "FROM_EB"), "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
raise SystemExit(3)
"""


class StagePathJudgeTests(unittest.TestCase):
    def _remove(self, root: str) -> None:
        for directory, dirs, _files in os.walk(root, followlinks=False):
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IRWXU)
        shutil.rmtree(root)

    def _bwrap_tree(self, script: str):
        root = tempfile.mkdtemp(prefix="stage-judge-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(state)
        os.makedirs(outside)
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        bwrap = os.path.join(bindir, "bwrap")
        with open(bwrap, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(bwrap, 0o755)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(eb, 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["OUTSIDE"] = outside
        env["EASYBUILD_INSTALLPATH_SOFTWARE"] = software
        return software, modules, state, outside, env, eb

    def _run_bwrap(self, script: str):
        software, modules, state, outside, env, eb = self._bwrap_tree(script)
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
        )
        return done, software, modules, state, outside, env

    def _journal_bytes(self, state: str) -> bytes:
        with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
            return handle.read()

    def _assert_resume_refuses(self, state: str, env: dict[str, str], outside: str, marker: str) -> None:
        before = self._journal_bytes(state)
        for args in (("resume",), ("resume", "--verify")):
            done = run_cli(*args, "--state-root", state, "r1", env=env)
            detail = done.stdout + done.stderr
            self.assertEqual(done.returncode, 2, detail)
            self.assertIn(marker, detail)
            self.assertNotIn("rebuilding", detail)
            self.assertFalse(os.path.lexists(os.path.join(outside, "FROM_EB")), detail)
            with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
                self.assertEqual(handle.read(), before, detail)

    def test_symlink_install_inside_the_generation_is_refused(self) -> None:
        """Regression test."""
        done, _software, _modules, state, outside, env = self._run_bwrap(_INSIDE_LINK)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        gen = os.path.join(state, "generations", "r1")
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        self._assert_resume_refuses(state, env, outside, "staged installation escapes this run")
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        self.assertFalse(os.path.lexists(os.path.join(gen, "software", "kept", "1", "THROUGH")))

    def test_missing_final_component_under_a_symlink_parent_is_refused(self) -> None:
        """Regression test."""
        done, _software, _modules, state, outside, env = self._run_bwrap(_MISSING_FINAL)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        final = os.path.join(outside, "demo", "1")
        shutil.rmtree(final)
        self.assertFalse(os.path.lexists(os.path.join(final, "FROM_EB")))
        self._assert_resume_refuses(state, env, outside, "staged installation escapes this run")
        self.assertFalse(os.path.lexists(final))

    def test_installpath_and_userns_stage_roots_are_refused(self) -> None:
        """Regression test."""
        for backend in ("installpath", "userns"):
            with self.subTest(backend=backend):
                self._refuse_non_bwrap(backend)

    def _refuse_non_bwrap(self, backend: str) -> None:
        root = tempfile.mkdtemp(prefix="stage-judge-plain-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(os.path.join(outside, "software"))
        os.makedirs(os.path.join(outside, "modules"))
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(_INSTALLPATH)
        os.chmod(eb, 0o755)
        env = dict(os.environ)
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        journal_path = os.path.join(state, "runs", "r1", "journal.json")
        with open(journal_path, encoding="utf-8") as handle:
            text = handle.read()
            journal = json.loads(text)
        if backend != "installpath":
            text, count = re.subn(
                r'("backend"\s*:\s*)"installpath"',
                rf'\1"{backend}"',
                text,
                count=1,
            )
            self.assertEqual(count, 1, text)
            with open(journal_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        gen = journal["generation_root"]
        stage_software = os.path.join(gen, "software")
        stage_modules = os.path.join(gen, "modules")
        # software is a symlink out of the generation. modules is absent, so
        # it is not a directory: the old check returned before it looked at
        # software, and resume then wrote through the link.
        shutil.rmtree(stage_software)
        shutil.rmtree(stage_modules)
        os.symlink(os.path.join(outside, "software"), stage_software)
        self._assert_resume_refuses(state, env, os.path.join(outside, "software"), "stage software path escapes this run")
        self.assertEqual(os.listdir(os.path.join(outside, "software")), [])

    def test_absent_staged_path_for_a_live_module_root_rebuilds(self) -> None:
        """Regression test."""
        done, software, _modules, state, outside, env = self._run_bwrap(_LIVE_ROOT)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        live = os.path.join(software, "demo", "1")
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertNotIn(f"escapes this run: {live}", resumed_detail)
        self.assertFalse(os.path.lexists(os.path.join(outside, "FROM_EB")), resumed_detail)
        self.assertFalse(os.path.lexists(os.path.join(outside, "REBUILT")), resumed_detail)
        gen = os.path.join(state, "generations", "r1")
        rebuilt = os.path.join(gen, "software", "REBUILT")
        self.assertTrue(os.path.isfile(rebuilt), resumed_detail)
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2\n")

    def test_symlink_ancestor_of_the_software_and_modules_prefixes_is_followed(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-prefix-")
        self.addCleanup(self._remove, root)
        site = Site(os.path.join(root, "real"))
        link = os.path.join(root, "via")
        os.symlink(site.root, link)
        # The live module must name the path the run was given. The prefix
        # itself is a real directory; only an ancestor is a symlink.
        live_through = os.path.join(link, "software", "demo", "1")
        with open(os.path.join(link, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live_through}"\nsetenv("EBROOTDEMO", root)\n')
        done = run_cli(
            "run",
            "--software",
            os.path.join(link, "software"),
            "--modules",
            os.path.join(link, "modules"),
            "--state-root",
            site.state,
            "--backend",
            "installpath",
            "--eb",
            site.eb,
            "--run-id",
            "r1",
            "--skip-live-check",
            "--module",
            "demo/1",
            "--",
            "demo-1.eb",
            f"--buildpath={site.root}/build",
            env=site.env,
            cwd=site.root,
            clear=site.cleared(),
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertNotIn("is a symlink", detail)
        payload = os.path.join(site.software, "demo", "1", "PAYLOAD")
        with open(payload, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "from-eb\n", detail)

    def _write_eb(self, root: str, script: str) -> str:
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(eb, 0o755)
        return eb

    def _tree(self) -> tuple[str, str, str, str, str, dict[str, str]]:
        root = tempfile.mkdtemp(prefix="stage-judge-list-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(state)
        os.makedirs(outside)
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        env = dict(os.environ)
        env["OUTSIDE"] = outside
        env["EASYBUILD_INSTALLPATH_SOFTWARE"] = software
        return root, software, modules, state, outside, env

    def test_resume_judges_the_plan_when_the_modules_root_is_absent(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env = self._tree()
        eb = self._write_eb(
            root,
            """#!/usr/bin/env python3
import os, sys
gen = os.environ["GEN"]
count_path = os.path.join(gen, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
with open(os.path.join(os.environ["OUTSIDE"], "FROM_EB"), "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
raise SystemExit(0)
""",
        )
        gen = os.path.join(state, "generations", "r1")
        stage_software = os.path.join(gen, "software")
        link = os.path.join(stage_software, "demo", "1")
        os.makedirs(os.path.dirname(link))
        os.symlink(outside, link)
        with open(os.path.join(gen, "build-count"), "w", encoding="utf-8") as handle:
            handle.write("1\n")
        env["GEN"] = gen
        run_dir = os.path.join(state, "runs", "r1")
        os.makedirs(run_dir)
        journal = {
            "schema": 2,
            "publication": "directory-swap",
            "run_id": "r1",
            "state": "failed",
            "backend": "installpath",
            "software": software,
            "modules": modules,
            "state_root": state,
            "generation_root": gen,
            "eb_executable": eb,
            "eb_args": ["demo-1.eb"],
            "skip_live_check": True,
            "module_rels": ["demo/1"],
            "modules_plan": [
                {
                    "rel": "demo/1",
                    "live_module": os.path.join(modules, "all", "demo", "1.lua"),
                    "prepared": os.path.join(run_dir, "prepared.lua"),
                    "staged": link,
                    "live_software": os.path.join(software, "demo", "1"),
                    "retired": os.path.join(software, "demo", ".1.r1"),
                    "status": "pending",
                }
            ],
            "links_plan": [],
            "message": "",
        }
        journal_path = os.path.join(run_dir, "journal.json")
        with open(journal_path, "w", encoding="utf-8") as handle:
            json.dump(journal, handle)
            handle.write("\n")
        self._assert_resume_refuses(state, env, outside, "staged installation escapes this run")
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        self.assertFalse(os.path.lexists(os.path.join(outside, "FROM_EB")))

    def test_failed_build_records_the_staged_install(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env = self._tree()
        eb = self._write_eb(
            root,
            """#!/usr/bin/env python3
import os, sys
"""
            + _VERSION
            + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

if "--version" in sys.argv or "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    raise SystemExit(0)
software = opt("--installpath-software")
modules = opt("--installpath-modules")
if not software or not modules:
    print("missing installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(software, exist_ok=True)
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
other = os.path.join(software, "other", "9")
os.makedirs(os.path.dirname(other), exist_ok=True)
if not os.path.lexists(other):
    os.symlink(os.environ["OUTSIDE"], other)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % other)
gen = os.path.dirname(software)
count_path = os.path.join(gen, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
if n >= 2:
    with open(os.path.join(os.environ["OUTSIDE"], "FROM_EB"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
raise SystemExit(3)
""",
        )
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        gen = os.path.join(state, "generations", "r1")
        shutil.rmtree(os.path.join(gen, "modules"))
        self.assertFalse(os.path.lexists(os.path.join(outside, "FROM_EB")))
        self._assert_resume_refuses(state, env, outside, "staged installation escapes this run")
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")

    def test_publish_judges_before_eb_version_and_dry_run(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env = self._tree()
        gen = os.path.join(state, "generations", "r1")
        os.makedirs(gen)
        os.symlink(outside, os.path.join(gen, "software"))
        log_path = os.path.join(root, "eb.log")
        env["EB_LOG"] = log_path
        env["GEN_SOFTWARE"] = os.path.join(gen, "software")
        eb = self._write_eb(
            root,
            """#!/usr/bin/env python3
import os, sys
with open(os.environ["EB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv) + "\\n")
with open(os.path.join(os.environ["GEN_SOFTWARE"], "FROM_EB"), "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)
raise SystemExit(0)
""",
        )
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        detail = done.stdout + done.stderr
        log_text = ""
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as handle:
                log_text = handle.read()
        self.assertNotIn("--version", log_text, detail + log_text)
        self.assertNotIn("--dry-run", log_text, detail + log_text)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertEqual(os.listdir(outside), [], detail)
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("escapes", detail)
        self.assertNotIn("module preflight", detail)

    def _live_tree(self) -> tuple[str, str, str, str, str, dict[str, str], str, str]:
        root = tempfile.mkdtemp(prefix="stage-judge-before-")
        self.addCleanup(self._remove, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(state)
        os.makedirs(outside)
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        log_path = os.path.join(root, "eb.log")
        eb = self._write_eb(root, _TOUCH_EB)
        env = dict(os.environ)
        env["EB_LOG"] = log_path
        env["OUTSIDE"] = outside
        return root, software, modules, state, outside, env, eb, log_path

    def _assert_stopped_before_eb(self, done, outside: str, log_path: str, state: str) -> None:
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("escapes", detail)
        self.assertNotIn("module preflight", detail)
        self.assertNotIn("dry-run:", detail)
        self.assertNotIn("running ", detail)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertEqual(os.listdir(outside), [], detail)
        self.assertFalse(os.path.exists(os.path.join(state, "runs", "r1")), detail)

    def test_publish_without_module_judges_a_nested_symlink_before_eb(self) -> None:
        """Regression test."""
        # The rel is learned from eb --dry-run. The symlink is already at
        # generations/<id>/software/demo/1, for every backend, including auto.
        for backend in ("installpath", "userns", "auto", "bwrap"):
            with self.subTest(backend=backend):
                root, software, modules, state, outside, env, eb, log_path = self._live_tree()
                if backend == "bwrap":
                    env["PATH"] = os.path.join(root, "bin")
                    os.makedirs(env["PATH"])
                gen = os.path.join(state, "generations", "r1")
                link = os.path.join(gen, "software", "demo", "1")
                os.makedirs(os.path.dirname(link))
                os.symlink(outside, link)
                env["EB_TOUCH"] = os.path.join(link, "FROM_EB")
                done = run_cli(
                    "run",
                    "--backend",
                    backend,
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
                    "demo-1.eb",
                    env=env,
                )
                self._assert_stopped_before_eb(done, outside, log_path, state)

    def test_dry_run_judges_a_software_symlink_before_eb_version(self) -> None:
        """Regression test."""
        _root, software, modules, state, outside, env, eb, log_path = self._live_tree()
        gen = os.path.join(state, "generations", "r1")
        os.makedirs(gen)
        os.symlink(outside, os.path.join(gen, "software"))
        env["EB_TOUCH"] = os.path.join(gen, "software", "FROM_EB")
        done = run_cli(
            "run",
            "--dry-run",
            "--backend",
            "installpath",
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
            "demo-1.eb",
            env=env,
        )
        self._assert_stopped_before_eb(done, outside, log_path, state)

    def test_decoy_bwrap_info_does_not_hide_a_software_symlink(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env, eb, log_path = self._live_tree()
        env["PATH"] = os.path.join(root, "bin")
        os.makedirs(env["PATH"])
        gen = os.path.join(state, "generations", "r1")
        os.makedirs(gen)
        os.symlink(outside, os.path.join(gen, "software"))
        decoy_software = os.path.join(gen, "kept", "software")
        decoy_modules = os.path.join(gen, "kept", "modules")
        os.makedirs(decoy_software)
        os.makedirs(decoy_modules)
        with open(os.path.join(gen, "bwrap_info.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "bwrap_installpath": os.path.join(gen, "kept"),
                    "bwrap_installpath_software": decoy_software,
                    "bwrap_installpath_modules": decoy_modules,
                },
                handle,
            )
        env["EB_TOUCH"] = os.path.join(gen, "software", "FROM_EB")
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
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self._assert_stopped_before_eb(done, outside, log_path, state)
        self.assertNotIn("bwrap is not installed", detail)

    def _partial_build(self, mode: str):
        root, software, modules, state, outside, env, _eb, log_path = self._live_tree()
        eb = self._write_eb(root, _PARTIAL_EB)
        env["BAD_MODE"] = mode
        env["EB_LOG"] = log_path
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        self.assertFalse(os.path.lexists(os.path.join(outside, "VIA_LINK")), detail)
        gen = os.path.join(state, "generations", "r1")
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        with open(os.path.join(state, "runs", "r1", "journal.json"), encoding="utf-8") as handle:
            journal = json.load(handle)
        self.assertIn(os.path.join(gen, "software", "other", "9"), journal.get("staged_paths", []))
        return state, outside, env, log_path, gen, journal

    def test_non_utf8_module_file_blocks_resume_through_an_unrecorded_symlink(self) -> None:
        """Regression test."""
        state, outside, env, log_path, gen, journal = self._partial_build("utf8")
        self.assertIn("UTF-8", journal["stage_scan_error"])
        shutil.rmtree(os.path.join(gen, "modules"))
        before = self._journal_bytes(state)
        log_size = os.path.getsize(log_path)
        done = run_cli("resume", "--state-root", state, "r1", env=env)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("UTF-8", detail)
        self.assertNotIn("rebuilding", detail)
        self.assertFalse(os.path.lexists(os.path.join(outside, "VIA_LINK")), detail)
        self.assertEqual(os.path.getsize(log_path), log_size, detail)
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
            self.assertEqual(handle.read(), before, detail)

    def test_unreadable_module_file_is_kept_and_resume_does_not_rebuild(self) -> None:
        """Regression test."""
        state, outside, env, log_path, gen, journal = self._partial_build("unread")
        self.assertIn("cannot read", journal["stage_scan_error"])
        shutil.rmtree(os.path.join(gen, "modules"))
        link = os.path.join(gen, "software", "other", "9")
        os.remove(link)
        before = self._journal_bytes(state)
        log_size = os.path.getsize(log_path)
        done = run_cli("resume", "--state-root", state, "r1", env=env)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("cannot read", detail)
        self.assertNotIn("rebuilding", detail)
        self.assertFalse(os.path.lexists(os.path.join(outside, "VIA_LINK")), detail)
        self.assertFalse(os.path.lexists(link), detail)
        self.assertEqual(os.path.getsize(log_path), log_size, detail)
        with open(os.path.join(gen, "build-count"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1\n")
        with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
            self.assertEqual(handle.read(), before, detail)

    def _built_site(self, links=None, class_link=False):
        root = tempfile.mkdtemp(prefix="stage-judge-built-")
        self.addCleanup(self._remove, root)
        site = Site(root)
        prepare = os.path.join(root, "prepare")
        with open(prepare, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prepared')\n"
                "if not os.path.exists(flag):\n"
                "    open(flag, 'w').close()\n"
                "    raise SystemExit(9)\n"
                "raise SystemExit(0)\n"
            )
        os.chmod(prepare, 0o755)
        extra = {"links": links} if links else None
        done = site.run("--prepare", prepare, extra=extra, verify=False)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 9, detail)
        self.assertEqual(site.journal()["state"], "built", detail)
        gen = site.journal()["generation_root"]
        if class_link:
            os.symlink("demo.lua", os.path.join(gen, "modules", "all", "class.lua"))
        return site, gen

    def test_resume_publishes_a_built_generation_with_interior_links(self) -> None:
        """Regression test."""
        shapes = (
            ("lib64", {"lib64": "lib"}, False),
            ("python", {"bin/python": "python3"}, False),
            ("class", {}, True),
        )
        for name, links, class_link in shapes:
            with self.subTest(shape=name):
                site, gen = self._built_site(links, class_link)
                staged = site.staged()
                if name == "lib64":
                    self.assertTrue(os.path.islink(os.path.join(staged, "lib64")))
                elif name == "python":
                    self.assertTrue(os.path.islink(os.path.join(staged, "bin", "python")))
                else:
                    self.assertTrue(os.path.islink(os.path.join(gen, "modules", "all", "class.lua")))
                self.assertTrue(os.path.islink(os.path.join(gen, "modules", "tools", "demo", "1.lua")))
                done = site.cli("resume", "--state-root", site.state, "r1")
                detail = done.stdout + done.stderr
                self.assertEqual(done.returncode, 0, detail)
                self.assertIn("published", detail)
                self.assertNotIn("escapes", detail)
                self.assertEqual(site.payload(), "from-eb\n", detail)

    def test_resume_verify_of_a_built_lib64_link_succeeds(self) -> None:
        """Regression test."""
        site, _gen = self._built_site({"lib64": "lib"})
        self.assertTrue(os.path.islink(os.path.join(site.staged(), "lib64")))
        done = site.cli("resume", "--verify", "--state-root", site.state, "r1")
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertIn("published", detail)
        self.assertNotIn("escapes", detail)

    def test_symlinked_version_directory_is_refused_before_eb(self) -> None:
        """Regression test."""
        _root, software, modules, state, outside, env, eb, log_path = self._live_tree()
        gen = os.path.join(state, "generations", "r1")
        link = os.path.join(gen, "software", "demo", "1")
        os.makedirs(os.path.dirname(link))
        os.symlink(outside, link)
        env["EB_TOUCH"] = os.path.join(link, "FROM_EB")
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        self._assert_stopped_before_eb(done, outside, log_path, state)

    def test_symlinked_intermediate_is_refused_before_eb(self) -> None:
        """Regression test."""
        _root, software, modules, state, outside, env, eb, log_path = self._live_tree()
        gen = os.path.join(state, "generations", "r1")
        link = os.path.join(gen, "software", "demo")
        os.makedirs(os.path.dirname(link))
        os.symlink(outside, link)
        env["EB_TOUCH"] = os.path.join(link, "1", "FROM_EB")
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        self._assert_stopped_before_eb(done, outside, log_path, state)

    def _plant_regular(self, state: str, place: str) -> str:
        path = os.path.join(state, "generations", "r1", "software", place)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not-a-directory\n")
        return path

    def _assert_unnamed_file_refused(self, done, outside: str, log_path: str, state: str, path: str) -> None:
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("is a file", detail)
        self.assertIn("escapes", detail)
        self.assertIn(path, detail)
        self.assertNotIn("module preflight", detail)
        self.assertNotIn("dry-run:", detail)
        self.assertNotIn("running ", detail)
        self.assertNotIn("rebuilding", detail)
        self.assertNotIn("bwrap is not installed", detail)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertEqual(os.listdir(outside), [], detail)
        self.assertFalse(os.path.exists(os.path.join(state, "runs", "r1")), detail)

    def _fresh_unnamed_file(self, dry: bool) -> None:
        for place in ("demo", "demo/1"):
            for backend in ("installpath", "userns", "auto", "bwrap"):
                with self.subTest(place=place, backend=backend):
                    root, software, modules, state, outside, env, eb, log_path = self._live_tree()
                    if backend == "bwrap":
                        env["PATH"] = os.path.join(root, "bin")
                        os.makedirs(env["PATH"])
                    path = self._plant_regular(state, place)
                    env["EB_TOUCH"] = os.path.join(outside, "FROM_EB")
                    args = [
                        "run",
                        "--backend",
                        backend,
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
                        "demo-1.eb",
                    ]
                    if dry:
                        args.insert(1, "--dry-run")
                    done = run_cli(*args, env=env)
                    self._assert_unnamed_file_refused(done, outside, log_path, state, path)

    def test_run_refuses_a_regular_file_at_an_unnamed_software_component_before_eb(self) -> None:
        """Regression test."""
        self._fresh_unnamed_file(False)

    def test_dry_run_refuses_a_regular_file_at_an_unnamed_software_component_before_eb(self) -> None:
        """Regression test."""
        self._fresh_unnamed_file(True)

    def _failed_other(self):
        root, software, modules, state, outside, env, _eb, log_path = self._live_tree()
        os.makedirs(os.path.join(software, "other", "9"))
        with open(os.path.join(software, "other", "9", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        os.makedirs(os.path.join(modules, "all", "other"))
        with open(os.path.join(modules, "all", "other", "9.lua"), "w", encoding="utf-8") as handle:
            handle.write('local root = "other"\n')
        eb = self._write_eb(root, _OTHER_EB)
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
            "other/9",
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertNotEqual(done.returncode, 0, detail)
        journal_path = os.path.join(state, "runs", "r1", "journal.json")
        with open(journal_path, "rb") as handle:
            before = handle.read()
        journal = json.loads(before)
        self.assertEqual(journal["module_rels"], ["other/9"], detail)
        self.assertNotIn(b"software/demo", before)
        self.assertGreater(os.path.getsize(log_path), 0, detail)
        return state, outside, env, log_path, before

    def _resume_unnamed_file(self, verify: bool) -> None:
        for place in ("demo", "demo/1"):
            with self.subTest(place=place):
                state, outside, env, log_path, before = self._failed_other()
                path = self._plant_regular(state, place)
                env["EB_TOUCH"] = os.path.join(outside, "FROM_EB")
                log_size = os.path.getsize(log_path)
                args = ["resume", "--state-root", state, "r1"]
                if verify:
                    args.insert(1, "--verify")
                done = run_cli(*args, env=env)
                detail = done.stdout + done.stderr
                self.assertEqual(done.returncode, 2, detail)
                self.assertIn("is a file", detail)
                self.assertIn("escapes", detail)
                self.assertIn(path, detail)
                self.assertNotIn("module preflight", detail)
                self.assertNotIn("rebuilding", detail)
                self.assertNotIn("running ", detail)
                self.assertNotIn("dry-run:", detail)
                self.assertEqual(os.listdir(outside), [], detail)
                self.assertEqual(os.path.getsize(log_path), log_size, detail)
                with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
                    self.assertEqual(handle.read(), before, detail)

    def test_resume_refuses_a_regular_file_at_an_unnamed_software_component_before_eb(self) -> None:
        """Regression test."""
        self._resume_unnamed_file(False)

    def test_resume_verify_refuses_a_regular_file_at_an_unnamed_software_component_before_eb(self) -> None:
        """Regression test."""
        self._resume_unnamed_file(True)

    def _plant_kind(self, state: str, rel: str, kind: str, outside: str) -> str:
        path = os.path.join(state, "generations", "r1", "software", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if kind == "file":
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not-a-directory\n")
        elif kind == "symlink":
            os.symlink(os.path.join(outside, "PWNED"), path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            raise AssertionError(kind)
        return path

    def _assert_kind_refused(self, done, outside: str, log_path: str, state: str, path: str, phrase: str) -> None:
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn(phrase, detail)
        self.assertIn("escapes", detail)
        self.assertIn(path, detail)
        self.assertNotIn("module preflight", detail)
        self.assertNotIn("dry-run:", detail)
        self.assertNotIn("running ", detail)
        self.assertNotIn("rebuilding", detail)
        self.assertNotIn("bwrap is not installed", detail)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertEqual(os.listdir(outside), [], detail)
        self.assertFalse(os.path.exists(os.path.join(state, "runs", "r1")), detail)

    def _fresh_kind(self, rel: str, kind: str, phrase: str) -> None:
        for dry in (False, True):
            for backend in ("installpath", "userns", "auto", "bwrap"):
                with self.subTest(dry=dry, backend=backend):
                    root, software, modules, state, outside, env, eb, log_path = self._live_tree()
                    if backend == "bwrap":
                        env["PATH"] = os.path.join(root, "bin")
                        os.makedirs(env["PATH"])
                    path = self._plant_kind(state, rel, kind, outside)
                    env["EB_TOUCH"] = os.path.join(outside, "FROM_EB") if kind == "fifo" else path
                    args = [
                        "run",
                        "--backend",
                        backend,
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
                        "demo-1.eb",
                    ]
                    if dry:
                        args.insert(1, "--dry-run")
                    done = run_cli(*args, env=env)
                    self._assert_kind_refused(done, outside, log_path, state, path, phrase)

    def _resume_kind(self, rel: str, kind: str, phrase: str) -> None:
        for verify in (False, True):
            with self.subTest(verify=verify):
                state, outside, env, log_path, before = self._failed_other()
                path = self._plant_kind(state, rel, kind, outside)
                env["EB_TOUCH"] = os.path.join(outside, "FROM_EB") if kind == "fifo" else path
                log_size = os.path.getsize(log_path)
                args = ["resume", "--state-root", state, "r1"]
                if verify:
                    args.insert(1, "--verify")
                done = run_cli(*args, env=env)
                detail = done.stdout + done.stderr
                self.assertEqual(done.returncode, 2, detail)
                self.assertIn(phrase, detail)
                self.assertIn("escapes", detail)
                self.assertIn(path, detail)
                self.assertNotIn("module preflight", detail)
                self.assertNotIn("rebuilding", detail)
                self.assertNotIn("running ", detail)
                self.assertNotIn("dry-run:", detail)
                self.assertEqual(os.listdir(outside), [], detail)
                self.assertEqual(os.path.getsize(log_path), log_size, detail)
                with open(os.path.join(state, "runs", "r1", "journal.json"), "rb") as handle:
                    self.assertEqual(handle.read(), before, detail)

    def test_symlink_at_hidden_version_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".hidden/1", "symlink", "is a symlink")
        self._resume_kind(".hidden/1", "symlink", "is a symlink")

    def test_fifo_at_hidden_version_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".hidden/1", "fifo", "is a special file")
        self._resume_kind(".hidden/1", "fifo", "is a special file")

    def test_regular_file_at_hidden_version_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".hidden/1", "file", "is a file")
        self._resume_kind(".hidden/1", "file", "is a file")

    def test_symlink_under_software_locks_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".locks/escaped", "symlink", "is a symlink")
        self._resume_kind(".locks/escaped", "symlink", "is a symlink")

    def test_lock_symlink_under_software_locks_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".locks/escaped.lock", "symlink", "is a symlink")
        self._resume_kind(".locks/escaped.lock", "symlink", "is a symlink")

    def test_lock_fifo_under_software_locks_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".locks/escaped.lock", "fifo", "is a special file")
        self._resume_kind(".locks/escaped.lock", "fifo", "is a special file")

    def test_deep_symlink_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind("Core/GCC/13", "symlink", "is a symlink")

    def test_deep_file_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind("Core/GCC/13", "file", "is a file")

    def test_deep_fifo_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind("Core/GCC/13", "fifo", "is a special file")

    def test_symlink_under_nested_dot_directory_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".hidden/.nested/escape", "symlink", "is a symlink")

    def test_fifo_under_nested_dot_directory_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".hidden/.nested/escape", "fifo", "is a special file")

    def test_symlink_under_three_dot_directories_is_refused_before_eb(self) -> None:
        """Regression test."""
        self._fresh_kind(".a/.b/.c/escape", "symlink", "is a symlink")

    def _plant_interior(self, state: str, rel: str) -> str:
        install = os.path.join(state, "generations", "r1", "software", rel)
        os.makedirs(install)
        link = os.path.join(install, "lib64")
        os.symlink("lib", link)
        return link

    def _assert_proceeded_to_eb(self, done, log_path: str, before: int | None = None) -> None:
        detail = done.stdout + done.stderr
        self.assertNotIn("escapes", detail)
        self.assertTrue(os.path.exists(log_path), detail)
        size = os.path.getsize(log_path)
        if before is None:
            self.assertGreater(size, 0, detail)
        else:
            self.assertGreater(size, before, detail)
        proceeded = (
            "running " in detail
            or "dry-run:" in detail
            or "rebuilding" in detail
            or "module preflight" in detail
            or "bwrap is not installed" in detail
        )
        self.assertTrue(proceeded, detail)

    def test_non_digit_install_with_an_interior_symlink_proceeds_to_eb(self) -> None:
        """Regression test."""
        rels = ("demo/system", "OpenSSL/system", "demo/v1.2", "Compiler/GCCcore/system")
        for rel in rels:
            for dry in (False, True):
                for backend in ("installpath", "userns", "auto", "bwrap"):
                    with self.subTest(rel=rel, dry=dry, backend=backend):
                        _root, software, modules, state, _outside, env, eb, log_path = self._live_tree()
                        self._plant_interior(state, rel)
                        args = [
                            "run",
                            "--backend",
                            backend,
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
                            "demo-1.eb",
                        ]
                        if dry:
                            args.insert(1, "--dry-run")
                        done = run_cli(*args, env=env)
                        self._assert_proceeded_to_eb(done, log_path)
        for rel in rels:
            for verify in (False, True):
                with self.subTest(rel=rel, verify=verify):
                    state, _outside, env, log_path, _before = self._failed_other()
                    self._plant_interior(state, rel)
                    log_size = os.path.getsize(log_path)
                    args = ["resume", "--state-root", state, "r1"]
                    if verify:
                        args.insert(1, "--verify")
                    done = run_cli(*args, env=env)
                    self._assert_proceeded_to_eb(done, log_path, log_size)

    def test_symlink_named_system_is_refused(self) -> None:
        """Regression test."""
        self._fresh_kind("demo/system", "symlink", "is a symlink")
        self._fresh_kind("Compiler/GCCcore/system", "symlink", "is a symlink")
        self._resume_kind("demo/system", "symlink", "is a symlink")
        self._resume_kind("Compiler/GCCcore/system", "symlink", "is a symlink")

    def _plant_long_dot_symlink(self, state: str, outside: str) -> str:
        software = os.path.join(state, "generations", "r1", "software")
        os.makedirs(software, exist_ok=True)
        link_name = "link"
        slack = 4096 - len(software) - 1 - len(link_name)
        name_len = max(2, (slack + 21 - 1) // 21 - 1)
        if name_len > 255:
            name_len = 255
        comp = "." + ("a" * (name_len - 1))
        cur = software
        dir_fd = os.open(software, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(21):
            os.mkdir(comp, dir_fd=dir_fd)
            child = os.open(comp, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child
            cur = os.path.join(cur, comp)
        link = os.path.join(cur, link_name)
        os.symlink(os.path.join(outside, "PWNED"), link_name, dir_fd=dir_fd)
        os.close(dir_fd)
        self.assertGreaterEqual(len(link), 4096, len(link))
        return link

    def test_long_dot_chain_symlink_is_refused_before_eb(self) -> None:
        """Regression test."""
        for dry in (False, True):
            for backend in ("installpath", "userns", "auto", "bwrap"):
                with self.subTest(dry=dry, backend=backend):
                    root, software, modules, state, outside, env, eb, log_path = self._live_tree()
                    if backend == "bwrap":
                        env["PATH"] = os.path.join(root, "bin")
                        os.makedirs(env["PATH"])
                    path = self._plant_long_dot_symlink(state, outside)
                    env["EB_TOUCH"] = path
                    args = [
                        "run",
                        "--backend",
                        backend,
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
                        "demo-1.eb",
                    ]
                    if dry:
                        args.insert(1, "--dry-run")
                    done = run_cli(*args, env=env)
                    self._assert_kind_refused(done, outside, log_path, state, path, "is a symlink")

    def _fd_rmtree(self, root: str) -> None:
        def rec(fd: int) -> None:
            entries = [(ent.name, ent.is_dir(follow_symlinks=False)) for ent in os.scandir(fd)]
            for name, is_dir in entries:
                if is_dir:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                    try:
                        rec(child)
                    finally:
                        os.close(child)
                    os.rmdir(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)

        top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            rec(top)
        finally:
            os.close(top)
        os.rmdir(root)

    def _chain_exact(self, start: str, length: int) -> tuple[int, str]:
        fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY)
        path = start
        while len(path) != length:
            gap = length - len(path)
            name_len = gap - 1
            if name_len > 200:
                name_len = 200
                if gap - (1 + name_len) == 1:
                    name_len -= 1
            if name_len < 1:
                os.close(fd)
                raise AssertionError(f"cannot land on {length} from {len(path)}")
            name = "n" + ("b" * (name_len - 1))
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            path = path + "/" + name
        return fd, path

    def _chain_to(self, start: str, length: int) -> tuple[int, str]:
        fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY)
        path = start
        wide = "d" + ("b" * 199)
        while len(path) < length:
            need = length - len(path) - 1
            name = wide if need >= len(wide) else "d" + ("b" * max(0, need - 1))
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            path = path + "/" + name
        return fd, path

    def _extend(self, fd: int, path: str, names: tuple[str, ...]) -> tuple[int, str]:
        for name in names:
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            path = path + "/" + name
        return fd, path

    def _long_stage(self, *, modules: bool, software_link: bool):
        root = tempfile.mkdtemp(prefix="stage-judge-longroot-")
        self.addCleanup(self._fd_rmtree, root)
        software = os.path.join(root, "software")
        modules_live = os.path.join(root, "modules")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules_live, "all", "demo"))
        os.makedirs(outside)
        live = os.path.join(software, "demo", "1")
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(modules_live, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        log_path = os.path.join(root, "eb.log")
        eb = self._write_eb(root, _TOUCH_EB)
        env = dict(os.environ)
        env["EB_LOG"] = log_path
        env["OUTSIDE"] = outside
        fd, state = self._chain_to(root, 4096 - len("/generations/r1/modules"))
        fd, gen_software = self._extend(fd, state, ("generations", "r1", "software"))
        self.assertGreaterEqual(len(gen_software), 4096, len(gen_software))
        link = ""
        if software_link:
            os.symlink(os.path.join(outside, "PWNED"), "escape", dir_fd=fd)
            link = gen_software + "/escape"
        if modules:
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            try:
                os.mkdir("modules", dir_fd=parent)
            finally:
                os.close(parent)
            modules_path = gen_software[: -len("software")] + "modules"
            self.assertGreaterEqual(len(modules_path), 4096, len(modules_path))
        os.close(fd)
        return software, modules_live, state, outside, env, eb, log_path, link

    def test_long_software_root_symlink_is_refused_before_eb(self) -> None:
        for dry in (False, True):
            for backend in ("installpath", "userns", "auto", "bwrap"):
                with self.subTest(dry=dry, backend=backend):
                    software, modules, state, outside, env, eb, log_path, link = self._long_stage(
                        modules=False, software_link=True
                    )
                    root_holder = os.path.dirname(software)
                    if backend == "bwrap":
                        env["PATH"] = os.path.join(root_holder, "bin")
                        os.makedirs(env["PATH"])
                    env["EB_TOUCH"] = link
                    args = [
                        "run",
                        "--backend",
                        backend,
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
                        "demo-1.eb",
                    ]
                    if dry:
                        args.insert(1, "--dry-run")
                    done = run_cli(*args, env=env)
                    self._assert_kind_refused(done, outside, log_path, state, link, "is a symlink")

    def test_long_modules_directory_is_accepted(self) -> None:
        for backend in ("installpath", "userns", "auto", "bwrap"):
            with self.subTest(backend=backend):
                software, modules, state, _outside, env, eb, log_path, _link = self._long_stage(
                    modules=True, software_link=False
                )
                done = run_cli(
                    "run",
                    "--dry-run",
                    "--backend",
                    backend,
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
                    "demo-1.eb",
                    env=env,
                )
                detail = done.stdout + done.stderr
                self.assertNotIn("not a real directory", detail)
                self.assertNotIn("cannot be classified", detail)
                self._assert_proceeded_to_eb(done, log_path)

    def test_hidden_lock_file_under_software_does_not_block_a_rebuild(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-locks-")
        self.addCleanup(self._remove, root)
        site = Site(root)
        locks = os.path.join(site.state, "generations", "r1", "software", ".locks")
        os.makedirs(os.path.dirname(locks))
        with open(locks, "w", encoding="utf-8") as handle:
            handle.write("lock\n")
        done = site.run(verify=False)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertIn("published", detail)
        self.assertNotIn("escapes", detail)
        self.assertTrue(os.path.isfile(locks), detail)

    def test_easybuild_lock_under_software_locks_does_not_block_a_rebuild(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-eblock-")
        self.addCleanup(self._remove, root)
        site = Site(root)
        install = os.path.join(site.state, "generations", "r1", "software", "demo", "1")
        lock_dir = os.path.join(site.state, "generations", "r1", "software", ".locks", install.replace("/", "_") + ".lock")
        lock_file = os.path.join(os.path.dirname(lock_dir), "kept.lock")
        os.makedirs(lock_dir)
        with open(lock_file, "w", encoding="utf-8") as handle:
            handle.write("lock\n")
        done = site.run(verify=False)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertIn("published", detail)
        self.assertNotIn("escapes", detail)
        self.assertTrue(os.path.isdir(lock_dir), detail)
        self.assertTrue(os.path.isfile(lock_file), detail)

    def test_resume_rebuilds_a_real_install_that_holds_an_interior_symlink(self) -> None:
        """Regression test."""
        for state_name in ("failed", "created", "building"):
            with self.subTest(state=state_name):
                root, software, modules, state, outside, env, _eb, log_path = self._live_tree()
                eb = self._write_eb(root, _INTERIOR_EB)
                canary = os.path.join(outside, "CANARY")
                with open(canary, "w", encoding="utf-8") as handle:
                    handle.write("safe\n")
                done = run_cli(
                    "run",
                    "--backend",
                    "installpath",
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
                )
                detail = done.stdout + done.stderr
                self.assertEqual(done.returncode, 3, detail)
                journal_path = os.path.join(state, "runs", "r1", "journal.json")
                with open(journal_path, encoding="utf-8") as handle:
                    journal = json.load(handle)
                gen = journal["generation_root"]
                link = os.path.join(gen, "software", "demo", "1", "lib64")
                self.assertTrue(os.path.islink(link), detail)
                self.assertFalse(os.path.exists(os.path.join(outside, "PWNED")), detail)
                if state_name != "failed":
                    journal["state"] = state_name
                    with open(journal_path, "w", encoding="utf-8") as handle:
                        json.dump(journal, handle)
                        handle.write("\n")
                resumed = run_cli("resume", "--state-root", state, "r1", env=env)
                resumed_detail = resumed.stdout + resumed.stderr
                self.assertEqual(resumed.returncode, 0, resumed_detail)
                self.assertIn("published", resumed_detail)
                self.assertNotIn("escapes", resumed_detail)
                self.assertNotIn("Errno 24", resumed_detail)
                self.assertFalse(os.path.exists(os.path.join(outside, "PWNED")), resumed_detail)
                with open(canary, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "safe\n", resumed_detail)
                with open(os.path.join(software, "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "rebuilt\n", resumed_detail)
                self.assertFalse(os.path.exists(log_path) and "escapes" in open(log_path, encoding="utf-8").read())

    def test_sibling_symlink_is_refused_on_every_backend_and_dry_run(self) -> None:
        """Regression test."""
        cases = (
            ("installpath", False),
            ("userns", False),
            ("auto", False),
            ("bwrap", False),
            ("installpath", True),
        )
        for backend, dry in cases:
            with self.subTest(backend=backend, dry=dry):
                root, software, modules, state, outside, env, eb, log_path = self._live_tree()
                if backend == "bwrap":
                    env["PATH"] = os.path.join(root, "bin")
                    os.makedirs(env["PATH"])
                gen = os.path.join(state, "generations", "r1")
                os.makedirs(gen)
                secret = os.path.join(root, "secret")
                with open(secret, "w", encoding="utf-8") as handle:
                    handle.write("safe\n")
                link = os.path.join(gen, "sibling")
                os.symlink(secret, link)
                env["EB_TOUCH"] = link
                args = [
                    "run",
                    "--backend",
                    backend,
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
                    "demo-1.eb",
                ]
                if dry:
                    args.insert(1, "--dry-run")
                done = run_cli(*args, env=env)
                self._assert_stopped_before_eb(done, outside, log_path, state)
                with open(secret, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "safe\n")

    def test_resume_of_a_complete_run_reports_complete_with_a_module_symlink(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-complete-")
        self.addCleanup(self._remove, root)
        site = Site(root)
        done = site.run(verify=False)
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertEqual(site.journal()["state"], "complete", detail)
        gen = site.journal()["generation_root"]
        tools = os.path.join(gen, "modules", "tools", "demo", "1.lua")
        self.assertTrue(os.path.islink(tools), detail)
        os.symlink("demo.lua", os.path.join(gen, "modules", "all", "class.lua"))
        # Publication moved the module tree to the live modules directory.
        live_tools = os.path.join(site.modules, "tools", "demo", "1.lua")
        self.assertTrue(os.path.islink(live_tools), detail)
        resumed = site.cli("resume", "--state-root", site.state, "r1")
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 0, resumed_detail)
        self.assertIn("run r1 is already complete", resumed_detail)
        self.assertNotIn("escapes", resumed_detail)

    def test_resume_refuses_a_bwrap_info_symlink_before_rebuilding(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env, _eb, log_path = self._live_tree()
        eb = self._write_eb(root, _REAL_INSTALL_EB)
        secret = os.path.join(root, "secret")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("safe\n")
        done = run_cli(
            "run",
            "--backend",
            "installpath",
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
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 3, detail)
        journal_path = os.path.join(state, "runs", "r1", "journal.json")
        before = self._journal_bytes(state)
        with open(journal_path, encoding="utf-8") as handle:
            gen = json.load(handle)["generation_root"]
        link = os.path.join(gen, "bwrap_info.json")
        os.symlink(secret, link)
        env["EB_TOUCH"] = link
        log_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        resumed = run_cli("resume", "--state-root", state, "r1", env=env)
        resumed_detail = resumed.stdout + resumed.stderr
        self.assertEqual(resumed.returncode, 2, resumed_detail)
        self.assertIn("escapes", resumed_detail)
        self.assertNotIn("rebuilding", resumed_detail)
        self.assertNotIn("dry-run:", resumed_detail)
        with open(secret, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "safe\n", resumed_detail)
        self.assertEqual(self._journal_bytes(state), before, resumed_detail)
        grown = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        self.assertEqual(grown, log_size, resumed_detail)
        self.assertEqual(os.listdir(outside), [], resumed_detail)

    def test_dry_run_refuses_a_fifo_at_bwrap_info(self) -> None:
        """Regression test."""
        root, software, modules, state, outside, env, eb, log_path = self._live_tree()
        gen = os.path.join(state, "generations", "r1")
        os.makedirs(gen)
        os.mkfifo(os.path.join(gen, "bwrap_info.json"))
        done = run_cli(
            "run",
            "--dry-run",
            "--backend",
            "installpath",
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
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("special", detail)
        self.assertIn("escapes", detail)
        self.assertNotIn("dry-run:", detail)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertFalse(os.path.exists(os.path.join(state, "runs", "r1")), detail)
        self.assertEqual(os.listdir(outside), [], detail)
        _ = root

    def test_resume_under_a_small_nofile_does_not_report_emfile_as_an_escape(self) -> None:
        """Regression test."""
        site, _gen = self._built_site()
        staged = site.staged()
        for index in range(80):
            os.makedirs(os.path.join(staged, f"d{index:02d}"))
        done = site.cli("resume", "--state-root", site.state, "r1", prefix=["prlimit", "--nofile=64"])
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, detail)
        self.assertIn("published", detail)
        self.assertNotIn("Errno 24", detail)
        self.assertNotIn("Errno 23", detail)
        self.assertNotIn("escapes", detail)
        self.assertEqual(site.payload(), "from-eb\n", detail)

    def test_resume_after_journal_built_write_keeps_a_soname_link(self) -> None:
        """Regression test."""
        ops = os.environ.get("TOOL_BIN_TEST_OPS") or ""
        if not ops:
            sibling = os.path.join(os.path.dirname(tool_bin()), "eb-staged-rebuild-test-ops")
            if os.access(sibling, os.X_OK):
                ops = sibling
        if not ops or not os.access(ops, os.X_OK):
            self.fail("eb-staged-rebuild-test-ops is missing; zig build -Dtest-ops builds it")
        previous = os.environ.get("TOOL_BIN")
        os.environ["TOOL_BIN"] = ops
        try:
            for verify in (False, True):
                with self.subTest(verify=verify):
                    root = tempfile.mkdtemp(prefix="stage-judge-soname-")
                    self.addCleanup(self._remove, root)
                    site = Site(root)
                    failed = site.run(
                        verify=False,
                        extra={
                            "text": {"lib/libdemo.so.1": "soname\n"},
                            "links": {"lib/libdemo.so": "libdemo.so.1"},
                        },
                        EB_STAGED_REBUILD_FAIL_AT="journal.built.write",
                    )
                    detail = failed.stdout + failed.stderr
                    self.assertNotEqual(failed.returncode, 0, detail)
                    self.assertIn("journal.built.write", detail)
                    journal = site.journal()
                    self.assertNotEqual(journal["state"], "complete", detail)
                    self.assertIn(journal["state"], ("building", "created", "failed"), detail)
                    link = os.path.join(journal["generation_root"], "software", "demo", "1", "lib", "libdemo.so")
                    self.assertTrue(os.path.islink(link), detail)
                    self.assertEqual(os.readlink(link), "libdemo.so.1", detail)
                    args = ["resume", "--state-root", site.state, "r1"]
                    if verify:
                        args.insert(1, "--verify")
                    resumed = site.cli(*args)
                    resumed_detail = resumed.stdout + resumed.stderr
                    self.assertEqual(resumed.returncode, 0, resumed_detail)
                    self.assertIn("published", resumed_detail)
                    self.assertNotIn("escapes", resumed_detail)
                    self.assertNotIn("Errno 24", resumed_detail)
        finally:
            if previous is None:
                os.environ.pop("TOOL_BIN", None)
            else:
                os.environ["TOOL_BIN"] = previous

    def _long_install(self, length: int) -> tuple[str, str, str, str, str, dict[str, str], str, str]:
        root = tempfile.mkdtemp(prefix="stage-judge-clear-")
        self.addCleanup(self._fd_rmtree, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(os.path.join(modules, "all", "demo"))
        os.makedirs(outside)
        with open(os.path.join(software, "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        suffix = "/generations/r1/software/demo/1"
        fd, state = self._chain_exact(root, length - len(suffix))
        fd, install = self._extend(fd, state, ("generations", "r1", "software", "demo", "1"))
        self.assertEqual(len(install), length)
        os.symlink(outside, "lib64", dir_fd=fd)
        os.close(fd)
        with self.assertRaises(OSError) as raised:
            os.lstat(install)
        self.assertEqual(raised.exception.errno, errno.ENAMETOOLONG)
        log_path = os.path.join(root, "eb.log")
        eb = self._write_eb(root, _CLEAR_EB)
        env = dict(os.environ)
        env["EB_LOG"] = log_path
        env["OUTSIDE"] = outside
        return root, software, modules, state, outside, env, eb, log_path

    def test_rebuilt_stage_of_4096_bytes_is_removed_before_eb(self) -> None:
        """Regression test."""
        self._assert_rebuilt_stage_removed(4096, "installpath")

    def test_rebuilt_stage_of_4097_bytes_is_removed_before_eb(self) -> None:
        """Regression test."""
        self._assert_rebuilt_stage_removed(4097, "installpath")

    def test_bwrap_rebuilt_stage_of_4096_bytes_is_removed_before_eb(self) -> None:
        """Regression test."""
        self._assert_rebuilt_stage_removed(4096, "bwrap")

    def test_bwrap_rebuilt_stage_of_4097_bytes_is_removed_before_eb(self) -> None:
        """Regression test."""
        self._assert_rebuilt_stage_removed(4097, "bwrap")

    def _assert_rebuilt_stage_removed(self, length: int, backend: str) -> None:
        root, software, modules, state, outside, env, eb, log_path = self._long_install(length)
        done = run_cli(
            "run",
            "--backend",
            backend,
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
        )
        detail = done.stdout + done.stderr
        self.assertIn("running ", detail)
        with open(log_path, encoding="utf-8") as handle:
            builds = [
                line
                for line in handle
                if "demo-1.eb" in line and "--version" not in line and "--dry-run" not in line
            ]
        self.assertTrue(builds, detail)
        self.assertFalse(os.path.exists(os.path.join(outside, "PWNED")), detail)
        stage_software = state + "/generations/r1/software"
        self.assertLess(len(stage_software), 4096)
        try:
            fd = os.open(stage_software, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return
        try:
            try:
                demo = os.open("demo", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                return
        finally:
            os.close(fd)
        install = None
        try:
            try:
                install = os.open("1", os.O_RDONLY | os.O_DIRECTORY, dir_fd=demo)
            except FileNotFoundError:
                return
            try:
                mode = os.lstat("lib64", dir_fd=install).st_mode
            except FileNotFoundError:
                return
            self.assertFalse(stat.S_ISLNK(mode), detail)
        finally:
            os.close(demo)
            if install is not None:
                os.close(install)

    def test_state_root_symlink_of_4108_bytes_is_refused_before_eb(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-state-")
        self.addCleanup(self._fd_rmtree, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        outside = os.path.join(root, "outside")
        os.makedirs(os.path.join(software, "demo", "1"))
        os.makedirs(modules)
        os.makedirs(outside)
        real = os.path.join(root, "real-state")
        os.mkdir(real)
        fd, parent = self._chain_exact(root, 4102)
        os.symlink(real, "state", dir_fd=fd)
        os.close(fd)
        state = parent + "/state"
        self.assertEqual(len(state), 4108)
        with self.assertRaises(OSError) as raised:
            os.lstat(state)
        self.assertEqual(raised.exception.errno, errno.ENAMETOOLONG)
        log_path = os.path.join(root, "eb.log")
        eb = self._write_eb(root, _TOUCH_EB)
        env = dict(os.environ)
        env["EB_LOG"] = log_path
        env["OUTSIDE"] = outside
        done = run_cli(
            "run",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--module",
            "demo/1",
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("state root is unsafe", detail)
        self.assertIn("is a symlink", detail)
        self.assertIn(state, detail)
        self.assertFalse(os.path.exists(log_path), detail)
        self.assertEqual(os.listdir(outside), [], detail)

    def test_module_symlink_of_4136_bytes_is_refused_on_dry_run(self) -> None:
        """Regression test."""
        root = tempfile.mkdtemp(prefix="stage-judge-mod-")
        self.addCleanup(self._fd_rmtree, root)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        outside = os.path.join(root, "outside")
        os.mkdir(software)
        os.mkdir(modules)
        os.mkdir(state)
        os.mkdir(outside)
        fd, parent = self._chain_exact(software, 4131)
        os.symlink(modules, "link", dir_fd=fd)
        os.close(fd)
        full = parent + "/link"
        self.assertEqual(len(full), 4136)
        with self.assertRaises(OSError) as raised:
            os.lstat(full)
        self.assertEqual(raised.exception.errno, errno.ENAMETOOLONG)
        rel = os.path.relpath(full, software)
        log_path = os.path.join(root, "eb.log")
        eb = self._write_eb(root, _TOUCH_EB)
        env = dict(os.environ)
        env["EB_LOG"] = log_path
        done = run_cli(
            "run",
            "--dry-run",
            "--software",
            software,
            "--modules",
            modules,
            "--state-root",
            state,
            "--eb",
            eb,
            "--module",
            rel,
            "demo-1.eb",
            env=env,
        )
        detail = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, detail)
        self.assertIn("is a symlink", detail)
        self.assertIn("dry-run would refuse", detail)
        self.assertIn(full, detail)


_CLEAR_EB = """\
#!/usr/bin/env python3
import os, stat, sys
""" + _VERSION + """
with open(os.environ["EB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv) + "\\n")

def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

software = opt("--installpath-software")
if software is None:
    generation = opt("--bwrap-installpath")
    if generation is not None:
        software = generation + "/software"
if software is None:
    raise SystemExit(0)
root = os.open(software, os.O_RDONLY | os.O_DIRECTORY)
try:
    demo = os.open("demo", os.O_RDONLY | os.O_DIRECTORY, dir_fd=root)
finally:
    os.close(root)
install = None
try:
    try:
        install = os.open("1", os.O_RDONLY | os.O_DIRECTORY, dir_fd=demo)
    except FileNotFoundError:
        os.mkdir("1", dir_fd=demo)
        install = os.open("1", os.O_RDONLY | os.O_DIRECTORY, dir_fd=demo)
    info = os.lstat("lib64", dir_fd=install)
    if stat.S_ISLNK(info.st_mode):
        outside = os.open("lib64", os.O_RDONLY | os.O_DIRECTORY, dir_fd=install)
        try:
            created = os.open("PWNED", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644, dir_fd=outside)
            os.write(created, b"ran\\n")
            os.close(created)
        finally:
            os.close(outside)
except FileNotFoundError:
    created = os.open("PAYLOAD", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644, dir_fd=install)
    os.write(created, b"rebuilt\\n")
    os.close(created)
finally:
    if install is not None:
        os.close(install)
    os.close(demo)
raise SystemExit(0)
"""

_OTHER_EB = """\
#!/usr/bin/env python3
import os, sys
with open(os.environ["EB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv) + "\\n")
if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] other-9.eb (module: other/9)")
    raise SystemExit(0)
raise SystemExit(0)
"""

_TOUCH_EB = """\
#!/usr/bin/env python3
import os, sys
with open(os.environ["EB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv) + "\\n")
target = os.environ.get("EB_TOUCH")
if target:
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)
raise SystemExit(0)
"""

_PARTIAL_EB = """\
#!/usr/bin/env python3
import os, sys
with open(os.environ["EB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv) + "\\n")
if "--version" in sys.argv:
    print("This is EasyBuild 5.1.2 (framework: 5.1.2, easyblocks: 5.1.2)")
    raise SystemExit(0)
if "--dry-run" in sys.argv or "--extended-dry-run" in sys.argv:
    print(" * [R] demo-1.eb (module: demo/1)")
    raise SystemExit(0)

def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

software = opt("--installpath-software")
modules = opt("--installpath-modules")
if not software or not modules:
    print("missing installpath", file=sys.stderr)
    raise SystemExit(2)
os.makedirs(software, exist_ok=True)
os.makedirs(os.path.join(modules, "all"), exist_ok=True)
link = os.path.join(software, "other", "9")
os.makedirs(os.path.dirname(link), exist_ok=True)
if not os.path.lexists(link):
    os.symlink(os.environ["OUTSIDE"], link)
with open(os.path.join(modules, "all", "good.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % link)
bad = os.path.join(modules, "all", "bad.lua")
if os.environ.get("BAD_MODE") == "unread":
    with open(bad, "w", encoding="utf-8") as handle:
        handle.write('local root = "%s"\\n' % link)
    os.chmod(bad, 0)
else:
    with open(bad, "wb") as handle:
        handle.write(b'local root = "x"\\n\\xff\\n')
gen = os.path.dirname(software)
count_path = os.path.join(gen, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
if n >= 2:
    with open(os.path.join(link, "VIA_LINK"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
raise SystemExit(3)
"""

_INTERIOR_EB = """\
#!/usr/bin/env python3
import os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

software = opt("--installpath-software")
modules = opt("--installpath-modules")
if not software or not modules:
    print("missing installpath", file=sys.stderr)
    raise SystemExit(2)
gen = os.path.dirname(software)
count_path = os.path.join(gen, "build-count")
n = 0
if os.path.exists(count_path):
    with open(count_path, encoding="utf-8") as handle:
        n = int(handle.read() or "0")
n += 1
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % n)
install = os.path.join(software, "demo", "1")
link = os.path.join(install, "lib64")
if n == 1:
    os.makedirs(install, exist_ok=True)
    if not os.path.lexists(link):
        os.symlink(os.environ["OUTSIDE"], link)
    os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
    with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
        handle.write('local root = "%s"\\n' % install)
    raise SystemExit(3)
if os.path.islink(link):
    with open(os.path.join(link, "PWNED"), "w", encoding="utf-8") as handle:
        handle.write("ran\\n")
os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("rebuilt\\n")
with open(os.path.join(install, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
    handle.write("name = 'demo'\\n")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % install)
raise SystemExit(0)
"""

_REAL_INSTALL_EB = """\
#!/usr/bin/env python3
import os, sys
""" + _VERSION + """
def opt(name):
    prefix = name + "="
    for arg in sys.argv:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None

software = opt("--installpath-software")
modules = opt("--installpath-modules")
if not software or not modules:
    print("missing installpath", file=sys.stderr)
    raise SystemExit(2)
install = os.path.join(software, "demo", "1")
os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("staged\\n")
with open(os.path.join(install, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
    handle.write("name = 'demo'\\n")
os.makedirs(os.path.join(modules, "all", "demo"), exist_ok=True)
with open(os.path.join(modules, "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
    handle.write('local root = "%s"\\n' % install)
raise SystemExit(3)
"""


if __name__ == "__main__":
    unittest.main()
