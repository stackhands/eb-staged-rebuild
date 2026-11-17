"""Tests for the opt-in --build-dir option and its refusal rules."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from eb_staged_rebuild import cli
from eb_staged_rebuild.builders import bwrap_command, child_env, installpath_command
from eb_staged_rebuild.engine import Spec, resume, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.probe import probe_userns_sandbox
from tests.support import PROBES, classic_install

FAKE_EB = os.path.join(os.path.dirname(__file__), "fake_eb.py")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _probes():
    probes = json.loads(json.dumps(PROBES))
    probes["easybuild"]["framework_version"] = [5, 4, 2]
    probes["easybuild"]["stage_path_keys"] = True
    return probes


class BuildDirTests(unittest.TestCase):
    def setUp(self):
        os.chmod(FAKE_EB, os.stat(FAKE_EB).st_mode | stat.S_IEXEC)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.software = os.path.join(self.root, "software")
        self.modules = os.path.join(self.root, "modules")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.software)
        os.makedirs(self.modules)
        self.install, self.module, self.v0 = classic_install(
            self.software, self.modules, "demo/1", "v0\n", "all/demo/1.lua"
        )
        self.argv = os.path.join(self.root, "argv.log")
        self._env = dict(os.environ)
        os.environ["FAKE_EB_ARGV"] = self.argv
        os.environ["FAKE_EB_PAYLOAD"] = "v1\n"
        for key in ("FAKE_EB_FAIL", "EASYBUILD_BUILDPATH"):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _spec(self, backend="installpath", build_dir=None, eb_args=None, run_id="run1"):
        return Spec(
            software=self.software,
            modules=self.modules,
            state_root=self.state,
            eb_args=list(eb_args or [os.path.join(self.root, "demo-1.eb")]),
            backend=backend,
            module_rels=["demo/1"],
            eb_executable=FAKE_EB,
            probes=_probes(),
            run_id=run_id,
            build_dir=build_dir,
        )

    def test_without_build_dir_eb_args_and_env_are_unchanged(self):
        env = child_env(
            {"PATH": "/bin", "EASYBUILD_BUILDPATH": "/keep/me"},
            "/gen/software",
            "/gen/modules",
            "/live/modules",
            ["demo-1.eb", "--buildpath=/from/eb-args"],
        )
        self.assertEqual(env.get("EASYBUILD_BUILDPATH"), "/keep/me")
        result = run(
            self._spec(
                backend="installpath",
                build_dir=None,
                eb_args=[os.path.join(self.root, "demo-1.eb"), "--buildpath=/from/eb-args"],
            )
        )
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        build_cmd = commands[-1]
        self.assertIn("--buildpath=/from/eb-args", build_cmd)
        self.assertEqual(sum(1 for arg in build_cmd if arg.startswith("--buildpath")), 1)
        journal = json.loads(_read(os.path.join(self.state, "runs", "run1", "journal.json")))
        self.assertNotIn("build_dir", journal)

    def test_build_dir_passes_buildpath_in_installpath_and_overrides_eb_args(self):
        build_dir = os.path.join(self.root, "scratch-build")
        os.environ["EASYBUILD_BUILDPATH"] = "/ignored/env"
        env = child_env(
            {"PATH": "/bin", "EASYBUILD_BUILDPATH": "/ignored/env"},
            "/gen/software",
            "/gen/modules",
            "/live/modules",
            ["demo-1.eb", "--buildpath", "/ignored/arg"],
            drop_buildpath=True,
        )
        self.assertNotIn("EASYBUILD_BUILDPATH", env)
        result = run(
            self._spec(
                backend="installpath",
                build_dir=build_dir,
                eb_args=[os.path.join(self.root, "demo-1.eb"), "--buildpath", "/ignored/arg"],
            )
        )
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        build_cmd = commands[-1]
        self.assertIn(f"--buildpath={build_dir}", build_cmd)
        self.assertNotIn("/ignored/arg", build_cmd)

    def test_build_dir_passes_buildpath_in_bwrap(self):
        build_dir = os.path.join(self.root, "scratch-bwrap")
        result = run(self._spec(backend="bwrap", build_dir=build_dir))
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        build_cmd = commands[-1]
        self.assertIn("--bwrap", build_cmd)
        self.assertIn(f"--buildpath={build_dir}", build_cmd)

    def test_build_dir_passes_buildpath_in_userns(self):
        probe = probe_userns_sandbox()
        if not probe.get("usable"):
            self.skipTest(probe.get("reason"))
        build_dir = os.path.join(self.root, "scratch-userns")
        result = run(self._spec(backend="userns", build_dir=build_dir))
        self.assertEqual(result.returncode, 0, result.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        build_cmd = commands[-1]
        self.assertIn(f"--buildpath={build_dir}", build_cmd)

    def test_relative_build_dir_is_made_absolute_and_saved_in_journal(self):
        with contextlib.chdir(self.root):
            result = run(self._spec(backend="installpath", build_dir="rel/build"))
        self.assertEqual(result.returncode, 0, result.message)
        expected = os.path.join(self.root, "rel", "build")
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        self.assertIn(f"--buildpath={expected}", commands[-1])
        journal = json.loads(_read(os.path.join(self.state, "runs", "run1", "journal.json")))
        self.assertEqual(journal["build_dir"], expected)

    def test_resume_reuses_recorded_build_dir(self):
        build_dir = os.path.join(self.root, "scratch-resume")
        os.environ["FAKE_EB_FAIL"] = "1"
        failed = run(self._spec(backend="installpath", build_dir=build_dir))
        self.assertEqual(failed.returncode, 3)
        os.environ.pop("FAKE_EB_FAIL", None)
        resumed = resume(self.state, "run1")
        self.assertEqual(resumed.returncode, 0, resumed.message)
        commands = [json.loads(line) for line in _read(self.argv).splitlines()]
        self.assertIn(f"--buildpath={build_dir}", commands[-1])

    def test_build_dir_refuses_empty_string(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                [
                    "run",
                    "--software",
                    self.software,
                    "--modules",
                    self.modules,
                    "--state-root",
                    self.state,
                    "--build-dir",
                    "",
                    "--dry-run",
                    "--",
                    "demo-1.eb",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--build-dir must name a directory; it is empty", err.getvalue())

    def test_build_dir_refuses_inside_generation(self):
        for candidate in (
            os.path.join(self.state, "generations"),
            os.path.join(self.state, "generations", "run1"),
            os.path.join(self.state, "generations", "run1", "build"),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(Refused) as caught:
                    run(self._spec(backend="installpath", build_dir=candidate))
                self.assertIn("generation", str(caught.exception))

    def test_build_dir_refuses_inside_live_prefix(self):
        for candidate, label in (
            (self.software, "software"),
            (os.path.join(self.software, "demo", "build"), "software"),
            (self.modules, "modules"),
            (os.path.join(self.modules, "all", "build"), "modules"),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(Refused) as caught:
                    run(self._spec(backend="installpath", build_dir=candidate))
                self.assertIn(label, str(caught.exception))

    def test_build_dir_refuses_symlink_resolving_inside_generation_or_live_prefix(self):
        targets = (
            (os.path.join(self.software, "demo", "via-link"), "software"),
            (os.path.join(self.modules, "all", "via-link"), "modules"),
            (os.path.join(self.state, "generations", "via-link"), "generation"),
        )
        for index, (target, expected) in enumerate(targets):
            link = os.path.join(self.root, f"link-{index}")
            os.symlink(target, link)
            with self.subTest(link=link):
                with self.assertRaises(Refused) as caught:
                    run(self._spec(backend="installpath", build_dir=link))
                self.assertIn(expected, str(caught.exception))

    def test_build_dir_refuses_non_directory_file(self):
        file_path = os.path.join(self.root, "not-a-dir")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(Refused) as caught:
            run(self._spec(backend="installpath", build_dir=file_path))
        self.assertIn("not a directory", str(caught.exception))

    def test_build_dir_allows_sibling_of_live_prefix_and_symlink_outside(self):
        real_scratch = os.path.join(self.root, "real-scratch")
        os.makedirs(real_scratch)
        link_scratch = os.path.join(self.root, "scratch-link")
        os.symlink(real_scratch, link_scratch)
        dry = run(
            Spec(
                software=self.software,
                modules=self.modules,
                state_root=self.state,
                eb_args=[os.path.join(self.root, "demo-1.eb")],
                backend="installpath",
                dry_run=True,
                module_rels=["demo/1"],
                eb_executable=FAKE_EB,
                probes=_probes(),
                build_dir=link_scratch,
            )
        )
        self.assertEqual(dry.returncode, 0, dry.message)
        cmd, _env, _sw, _mod = installpath_command(
            type("Ctx", (), {"generation_root": "/g", "eb_executable": "eb", "eb_args": ["x.eb"], "modules": "/m", "build_dir": link_scratch})()
        )
        self.assertIn(f"--buildpath={link_scratch}", cmd)
        bcmd, _benv = bwrap_command(
            type("Ctx", (), {"generation_root": "/g", "eb_executable": "eb", "eb_args": ["x.eb"], "software": "/s", "modules": "/m", "build_dir": link_scratch})()
        )
        self.assertIn(f"--buildpath={link_scratch}", bcmd)


if __name__ == "__main__":
    unittest.main()
