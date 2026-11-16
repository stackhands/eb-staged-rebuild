"""A staged tree that records the generation directory is not published."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from eb_staged_rebuild import genscan
from eb_staged_rebuild.builders import userns_command
from eb_staged_rebuild.engine import Spec, run
from eb_staged_rebuild.journal import journal_path, load_journal
from eb_staged_rebuild.probe import probe_userns_sandbox
from tests.elf_fixture import build_elf
from tests.support import PROBES, classic_install

FAKE_EB = os.path.join(os.path.dirname(__file__), "fake_eb.py")
GENERATION = "/state/generations/r1"
NEEDLE = GENERATION.encode()


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, data):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _scan(self):
        return genscan.sort_hits(genscan.scan_tree(self.root, self.root, GENERATION))

    def test_text_needs_the_whole_directory_name(self):
        self._file("a.pc", b"prefix=" + NEEDLE + b"/software/demo/1\n")
        self._file("b.pc", b"prefix=" + NEEDLE + b"\n")
        self._file("c.pc", b"prefix=" + NEEDLE + b"0/software\n")
        self._file("d.pc", b"prefix=/state/generations/r\n")
        self.assertEqual(self._scan(), [("a.pc", "text"), ("b.pc", "text")])

    def test_text_match_across_a_read_boundary(self):
        for offset in (genscan.CHUNK - len(NEEDLE), genscan.CHUNK - 3, genscan.CHUNK - len(NEEDLE) - 1):
            with self.subTest(offset=offset):
                self._file("big.bin", b"x" * offset + NEEDLE + b"/lib" + b"y" * 100)
                self._file("end.bin", b"x" * offset + NEEDLE)
                self.assertEqual(self._scan(), [("big.bin", "text"), ("end.bin", "text")])
        self._file("long.bin", b"x" * (genscan.CHUNK - len(NEEDLE)) + NEEDLE + b"0")
        os.unlink(os.path.join(self.root, "big.bin"))
        os.unlink(os.path.join(self.root, "end.bin"))
        self.assertEqual(self._scan(), [])

    def test_elf_kinds_for_every_layout(self):
        for bits in (32, 64):
            for big in (False, True):
                cases = {
                    "rpath": {"rpath": f"$ORIGIN/../lib:{GENERATION}/lib"},
                    "runpath": {"runpath": f"{GENERATION}/lib"},
                    "interp": {"interp": f"{GENERATION}/lib/ld.so"},
                }
                for kind, kwargs in cases.items():
                    with self.subTest(bits=bits, big=big, kind=kind):
                        self._file("f.so", build_elf(bits=bits, big=big, **kwargs))
                        self.assertEqual(self._scan(), [("f.so", kind)])
                with self.subTest(bits=bits, big=big, kind="all"):
                    self._file("f.so", build_elf(bits=bits, big=big, rpath=GENERATION, runpath=GENERATION, interp=GENERATION))
                    self.assertEqual(self._scan(), [("f.so", "rpath"), ("f.so", "runpath"), ("f.so", "interp")])
                with self.subTest(bits=bits, big=big, kind="clean"):
                    self._file("f.so", build_elf(bits=bits, big=big, rpath="$ORIGIN/../lib", runpath="/opt/x", interp="/lib64/ld.so"))
                    self.assertEqual(self._scan(), [])

    def test_elf_is_read_by_its_headers_not_its_bytes(self):
        data = bytearray(build_elf(rpath="$ORIGIN"))
        data += NEEDLE
        self._file("f.so", bytes(data))
        self.assertEqual(self._scan(), [])

    def test_damaged_elf_headers_give_no_hit(self):
        good = build_elf(rpath=GENERATION)
        for name, data in {
            "short": good[:40],
            "bad_class": good[:4] + b"\x07" + good[5:],
            "bad_order": good[:5] + b"\x09" + good[6:],
            "cut_table": good[:100],
        }.items():
            with self.subTest(name=name):
                self._file("f.so", data)
                self.assertEqual(self._scan(), [])

    def test_only_the_top_easybuild_directory_is_a_build_record(self):
        self._file("easybuild/easybuild-x.log", NEEDLE)
        self._file("easybuild/reprod/r.sh", NEEDLE)
        self._file("share/easybuild/y.txt", NEEDLE)
        self._file("lib/easybuild", NEEDLE)
        self.assertEqual(self._scan(), [("lib/easybuild", "text"), ("share/easybuild/y.txt", "text")])

    def test_symlinks_are_not_followed_and_the_stamp_is_skipped(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, outside.name)
        outside.write(NEEDLE)
        outside.close()
        os.symlink(outside.name, os.path.join(self.root, "link"))
        os.symlink(self.root, os.path.join(self.root, "loop"))
        for name in genscan.OWN_FILES:
            self._file(name, NEEDLE)
            self._file("sub/" + name, NEEDLE)
        self.assertEqual(self._scan(), [("sub/" + name, "text") for name in sorted(genscan.OWN_FILES)])

    def test_an_unreadable_file_refuses(self):
        path = self._file("secret", b"x")
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("the process reads files regardless of mode")
            with self.assertRaises(genscan.Refused) as caught:
                self._scan()
            self.assertEqual(str(caught.exception), "cannot scan secret: not readable")
        finally:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _probes():
    return json.loads(json.dumps(PROBES))


class RefusalTests(unittest.TestCase):
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
        self.inode = os.lstat(self.install).st_ino
        self._env = dict(os.environ)
        os.environ["FAKE_EB_PAYLOAD"] = "v1\n"
        for key in ("FAKE_EB_FAIL", "FAKE_EB_KEYS", "FAKE_EB_OVERLAY", "FAKE_EB_RECORD", "FAKE_EB_PREFIX_FILE"):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(self, backend):
        spec = Spec(
            software=self.software,
            modules=self.modules,
            state_root=self.state,
            eb_args=[os.path.join(self.root, "demo-1.eb")],
            backend=backend,
            module_rels=["demo/1"],
            eb_executable=FAKE_EB,
            probes=_probes(),
            run_id="run1",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = run(spec)
        return result, out.getvalue()

    def _generation(self):
        return os.path.join(self.state, "generations", "run1")

    def _assert_live_untouched(self):
        self.assertEqual(os.lstat(self.install).st_ino, self.inode)
        self.assertEqual(_read(os.path.join(self.install, "PAYLOAD")), "v0\n")
        self.assertEqual(_read(self.module), self.v0)
        retired = os.path.join(os.path.dirname(self.install), ".1.run1")
        self.assertFalse(os.path.lexists(retired))

    def _assert_published(self):
        self.assertNotEqual(os.lstat(self.install).st_ino, self.inode)
        self.assertEqual(_read(os.path.join(self.install, "PAYLOAD")), "v1\n")

    def test_a_text_file_with_the_generation_path_is_refused(self):
        os.environ["FAKE_EB_RECORD"] = "text"
        result, out = self._run("installpath")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.state, "failed")
        self._assert_live_untouched()
        self.assertIn(
            "eb-staged-rebuild: staged file records the generation directory: "
            "software/demo/1/lib/pkgconfig/demo.pc (text)\n",
            out,
        )
        self.assertIn("1 staged file(s) name the generation directory " + self._generation(), result.message)
        self.assertIn("use the bwrap or userns backend", result.message)
        journal = load_journal(journal_path(self.state, "run1"))
        self.assertEqual(journal["state"], "failed")
        self.assertEqual(journal["modules_plan"], [])
        self.assertFalse(os.path.exists(os.path.join(self._generation(), "software", "demo", "1", ".eb-staged-rebuild-complete")))

    def test_elf_records_are_refused_with_their_kind(self):
        for kind in ("rpath", "runpath", "interp"):
            for variant in ("", ":32be", ":32le", ":64be"):
                with self.subTest(kind=kind, variant=variant):
                    os.environ["FAKE_EB_RECORD"] = kind + variant
                    self.tearDown_state()
                    result, out = self._run("installpath")
                    self.assertEqual(result.returncode, 2, result.message)
                    self._assert_live_untouched()
                    self.assertIn(f"({kind})\n", out)

    def tearDown_state(self):
        # A fresh state root for the next subtest.
        import shutil

        shutil.rmtree(self.state, ignore_errors=True)

    def test_the_report_is_capped_with_a_total(self):
        os.environ["FAKE_EB_RECORD"] = "text*25,rpath"
        result, out = self._run("installpath")
        self.assertEqual(result.returncode, 2)
        hits = [line for line in out.splitlines() if "records the generation directory" in line]
        self.assertEqual(len(hits), genscan_limit())
        self.assertIn("eb-staged-rebuild: ... and 6 more\n", out)
        self.assertIn("26 staged file(s) name the generation directory", result.message)

    def test_a_clean_tree_publishes(self):
        os.environ["FAKE_EB_RECORD"] = "clean,near"
        result, out = self._run("installpath")
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_published()
        self.assertNotIn("records the generation directory", out)

    def test_the_easybuild_build_record_is_not_scanned(self):
        os.environ["FAKE_EB_RECORD"] = "ebrecord"
        result, out = self._run("installpath")
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_published()
        self.assertNotIn("records the generation directory", out)
        log = os.path.join(self.install, "easybuild", "easybuild-demo-1-20260101.123456.log")
        self.assertIn("generations", _read(log))

    def test_bwrap_records_the_live_path_and_publishes(self):
        os.environ["FAKE_EB_RECORD"] = "text,rpath,runpath,interp"
        result, _out = self._run("bwrap")
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_published()

    def test_the_userns_command_mounts_the_generation_over_the_stable_path(self):
        context = SimpleNamespace(
            generation_root=os.path.join(self.root, "generation"),
            eb_executable=FAKE_EB,
            eb_args=[os.path.join(self.root, "demo-1.eb")],
            software=self.software,
            modules=self.modules,
            rels=["demo/1", "dep/2"],
            probes={"userns_sandbox": {"path": "/usr/bin/unshare"}},
        )
        command, env, software, _modules = userns_command(context)
        self.assertEqual(software, self.software)
        binds = [command[i + 1 : i + 3] for i, word in enumerate(command) if word == "--bind"]
        self.assertEqual(
            binds,
            [
                [os.path.join(self.root, "generation", "software", "demo/1"), os.path.join(self.software, "demo/1")],
                [os.path.join(self.root, "generation", "software", "dep/2"), os.path.join(self.software, "dep/2")],
            ],
        )
        self.assertIn(f"--installpath-software={self.software}", command)
        self.assertEqual(env["EASYBUILD_INSTALLPATH_SOFTWARE"], self.software)
        self.assertEqual(env["EASYBUILD_INSTALLPATH_MODULES"], os.path.join(self.root, "generation", "modules"))
        self.assertEqual(env["EASYBUILD_LOCKS_DIR"], os.path.join(self.root, "generation", "locks"))
        self.assertLess(command.index("--read-only"), command.index("--bind"))
        self.assertLess(command.index("--bind"), command.index("--"))

    def test_the_userns_command_passes_the_invoking_users_ids(self):
        context = SimpleNamespace(
            generation_root=os.path.join(self.root, "generation"),
            eb_executable=FAKE_EB,
            eb_args=[],
            software=self.software,
            modules=self.modules,
            rels=[],
            probes={"userns_sandbox": {"path": "/usr/bin/unshare"}},
        )
        command, _env, _software, _modules = userns_command(context)
        index = command.index("--as-user")
        self.assertEqual(command[index + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertLess(index, command.index("--read-only"))

    def _sandbox(self, *arguments):
        command = [
            "unshare", "--user", "--map-root-user", "--mount", "--fork", "--kill-child",
            sys.executable, "-m", "eb_staged_rebuild.userns_sandbox", *arguments,
        ]
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def test_the_sandbox_drops_to_the_invoking_user_after_its_mounts(self):
        if not probe_userns_sandbox().get("usable"):
            self.skipTest("user namespaces are not usable here")
        ids = f"{os.getuid()}:{os.getgid()}"
        done = self._sandbox("--as-user", ids, "--read-only", self.software, "--", "sh", "-c", "id -u; id -g; touch " + self.software + "/x")
        self.assertEqual(done.stdout.split(), [str(os.getuid()), str(os.getgid())])
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Read-only", done.stderr)

    def test_the_sandbox_refuses_a_malformed_user(self):
        if not probe_userns_sandbox().get("usable"):
            self.skipTest("user namespaces are not usable here")
        done = self._sandbox("--as-user", "x", "--", "true")
        self.assertEqual(done.returncode, 125)
        self.assertIn("--as-user needs UID:GID, got 'x'", done.stderr)

    def test_userns_records_the_stable_path(self):
        probe = probe_userns_sandbox()
        if not probe.get("usable"):
            self.skipTest(probe.get("reason"))
        os.environ["FAKE_EB_PREFIX_FILE"] = "1"
        os.environ["FAKE_EB_RECORD"] = "text,rpath,runpath,interp"
        result, out = self._run("userns")
        self.assertEqual(result.returncode, 0, result.message)
        self._assert_published()
        self.assertEqual(_read(os.path.join(self.install, "PREFIX")), self.install + "\n")
        self.assertNotIn("records the generation directory", out)


def genscan_limit():
    from eb_staged_rebuild.engine import MAX_REPORTED_HITS

    return MAX_REPORTED_HITS


if __name__ == "__main__":
    unittest.main()
