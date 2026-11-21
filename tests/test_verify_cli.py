"""verify and the publish gate from the command line, for both implementations.

Each case stages demo/1 with tests/fake_eb.py, whose FAKE_EB_EXTRA adds ELF
objects, text files, links and modes to the installation, then runs
``run --verify``, ``verify`` and ``resume`` on it; a few run without
``--verify``, which publishes as a run did before the gate. Runs the Python
tool and the program TOOL_BIN names. LMOD_CMD and the EESSI
variables are unset unless a case sets them; LMOD_CMD is then
tests/fake_lmod.py or a script.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_verify_cli
"""

import os
import unittest

from tests.fake_eb import elf_bytes
from tests.support import FAKE_LMOD, Site, sites, write


class VerifyCliTests(unittest.TestCase):
    def setUp(self):
        self.previous_umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self.previous_umask)

    def sites(self):
        return sites(self, Site, "verify-cli-")

    def problems(self, site):
        return [f"{p['check']}: {p['message']}" for p in site.report()["problems"]]

    def test_a_clean_stage_passes_and_is_published(self):
        extra = {"elf": {"bin/demo": {"needed": ["libdemo.so.1"], "runpath": "$ORIGIN/../lib"}, "lib/libdemo.so.1": {}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(
                    f"verify r1: pass (elf 0, references 0, modules skipped (LMOD_CMD is not set), access 0); "
                    f"2 ELF object(s); report {site.report_path()}\n",
                    done.stdout,
                )
                self.assertEqual(site.payload(), "from-eb\n")
                journal = site.journal()
                self.assertEqual(journal["state"], "complete")
                self.assertEqual(journal["verify"]["result"], "pass")
                self.assertFalse(journal["verify"]["forced"])
                report = site.report()
                self.assertEqual(report["checks"], {"elf": 0, "references": 0, "modules": None, "access": 0})
                self.assertEqual(report["skipped"], {"modules": "LMOD_CMD is not set"})
                self.assertEqual((report["elf_objects"], report["problems"]), (2, []))

    def test_a_missing_library_is_refused_until_it_is_there(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn("verify: elf: demo/1/bin/demo: libgone.so.1 not found\n", done.stdout)
                self.assertIn(
                    "verify failed; the live directory was not swapped. Fix the stage and resume r1, "
                    "or resume it with --force to publish it anyway\n",
                    done.stdout,
                )
                self.assertEqual(site.payload(), "old\n")
                journal = site.journal()
                self.assertEqual((journal["state"], journal["verify"]["result"]), ("built", "fail"))
                self.assertFalse(journal["verify"]["forced"])
                self.assertEqual(self.problems(site), ["elf: demo/1/bin/demo: libgone.so.1 not found"])
                again = site.cli("verify", "--state-root", site.state, "r1")
                self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
                self.assertIn("verify r1: fail (elf 1, ", again.stdout)
                write(os.path.join(site.staged(), "lib", "libgone.so.1"), elf_bytes())
                fixed = site.cli("verify", "--state-root", site.state, "r1")
                self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
                self.assertEqual(site.journal()["verify"]["result"], "pass")
                published = site.cli("resume", "--state-root", site.state, "r1")
                self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
                self.assertEqual(site.payload(), "from-eb\n")
                self.assertEqual(site.journal()["state"], "complete")

    def test_force_publishes_a_failing_stage_and_records_it(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                refused = site.run(extra=extra)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                forced = site.cli("resume", "--state-root", site.state, "--force", "r1")
                self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
                self.assertIn("verify failed; publishing run r1 anyway (--force)\n", forced.stdout)
                self.assertEqual(site.payload(), "from-eb\n")
                journal = site.journal()
                self.assertEqual((journal["state"], journal["verify"]["result"], journal["verify"]["forced"]), ("complete", "fail", True))
                self.assertEqual(site.report()["result"], "fail")
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run("--force", extra=extra)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertTrue(site.journal()["verify"]["forced"])

    def test_without_verify_a_run_publishes_as_before(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra, verify=False)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertNotIn("eb-staged-rebuild: verify", done.stdout + done.stderr)
                self.assertEqual(site.payload(), "from-eb\n")
                journal = site.journal()
                self.assertEqual(journal["state"], "complete")
                self.assertEqual([key for key in journal if key.startswith("verify")], [])
                self.assertFalse(os.path.exists(site.report_path()))

    def test_force_needs_verify(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                refused = site.run("--force", verify=False)
                self.assertEqual(
                    (refused.returncode, refused.stdout, refused.stderr),
                    (2, "", "eb-staged-rebuild: --force needs --verify; without it, nothing checks the stage before the swap\n"),
                )
                self.assertFalse(os.path.exists(os.path.join(site.state, "runs", "r1")))
                self.assertEqual(site.run(verify=False).returncode, 0)
                again = site.cli("resume", "--state-root", site.state, "--force", "r1")
                self.assertEqual(
                    (again.returncode, again.stderr),
                    (2, "eb-staged-rebuild: --force needs --verify; run r1 does not check its stage before the swap\n"),
                )

    def test_resume_verify_checks_a_stage_the_run_did_not(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                prepare = os.path.join(site.root, "prepare")
                write(prepare, "#!/bin/sh\nexit 42\n", 0o755)
                stopped = site.run("--prepare", prepare, extra=extra, verify=False)
                self.assertEqual(stopped.returncode, 42, stopped.stdout + stopped.stderr)
                self.assertEqual(site.journal()["state"], "built")
                self.assertNotIn("verify_before_swap", site.journal())
                write(prepare, "#!/bin/sh\nexit 0\n", 0o755)
                build = os.path.join(site.root, "resume-build")
                checked = site.cli("resume", "--state-root", site.state, "--verify", "r1", EASYBUILD_BUILDPATH=build)
                self.assertEqual(checked.returncode, 2, checked.stdout + checked.stderr)
                self.assertIn("verify: elf: demo/1/bin/demo: libgone.so.1 not found\n", checked.stdout)
                journal = site.journal()
                self.assertEqual((journal["state"], journal["verify_before_swap"], journal["verify"]["result"]), ("built", True, "fail"))
                self.assertIn(build, journal["verify_context"]["build_dirs"])
                again = site.cli("resume", "--state-root", site.state, "r1")
                self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
                self.assertEqual(site.payload(), "old\n")
                self.assertEqual(site.journal()["verify_context"], journal["verify_context"])
                forced = site.cli("resume", "--state-root", site.state, "--force", "r1")
                self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
                self.assertEqual(site.payload(), "from-eb\n")
                self.assertTrue(site.journal()["verify"]["forced"])

    def test_a_stripped_rpath_is_named(self):
        extra = {"elf": {"bin/demo": {"needed": ["libdemo.so.1"]}, "lib/libdemo.so.1": {}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(self.problems(site), ["elf: demo/1/bin/demo: libdemo.so.1 not found (no RPATH or RUNPATH)"])

    def test_a_library_outside_the_stage_is_named(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                elsewhere = os.path.join(site.root, "elsewhere", "lib")
                write(os.path.join(elsewhere, "libx.so.1"), elf_bytes())
                extra = {"elf": {"bin/demo": {"needed": ["libx.so.1"], "runpath": elsewhere}, "lib/libin.so": {"needed": ["libx.so.1"], "rpath": "@ROOT@/lib:" + elsewhere, "wide": False, "machine": 3}}}
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(
                    f"elf: demo/1/bin/demo: libx.so.1 resolves to {elsewhere}/libx.so.1, outside the stage, the site prefix and EESSI",
                    self.problems(site),
                )
                self.assertIn(
                    f"elf: demo/1/lib/libin.so: libx.so.1 resolves to {elsewhere}/libx.so.1, outside the stage, the site prefix and EESSI",
                    self.problems(site),
                )

    def loader(self, site):
        system = os.path.join(site.root, "sys", "lib")
        loader = os.path.join(site.root, "sys", "ld.so")
        write(loader, f"#!/bin/sh\nprintf '\\t%s (system search path)\\n' '{system}'\n", 0o755)
        write(os.path.join(system, "libfoo.so.1"), elf_bytes())
        return loader, system, {"elf": {"bin/demo": {"needed": ["libfoo.so.1"], "interp": loader, "little": False}}}

    def test_the_loader_and_its_search_path(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                _loader, _system, extra = self.loader(site)
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(site.report()["problems"], [])
        for site in self.sites():
            with self.subTest(tool=site.name):
                loader, system, extra = self.loader(site)
                outside = site.run(extra=extra, EESSI_CVMFS_REPO=os.path.join(site.root, "eessi"))
                self.assertEqual(outside.returncode, 2, outside.stdout + outside.stderr)
                self.assertEqual(
                    self.problems(site),
                    [
                        f"elf: demo/1/bin/demo: loader {loader} is outside the stage, the site prefix and EESSI",
                        f"elf: demo/1/bin/demo: libfoo.so.1 resolves to {system}/libfoo.so.1, outside the stage, the site prefix and EESSI",
                    ],
                )

    def test_a_shared_object_is_loaded_by_the_eessi_loader(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                prefix = os.path.join(site.root, "eessi", "compat")
                lib64 = os.path.join(prefix, "lib64")
                write(os.path.join(lib64, "ld-linux-x86-64.so.2"), f"#!/bin/sh\necho '  {lib64} (system search path)'\n", 0o755)
                write(os.path.join(lib64, "libc.so.6"), elf_bytes())
                extra = {"elf": {"lib/libdemo.so.1": {"needed": ["libc.so.6", "libnot.so.1"]}, "bin/lost": {"interp": "/no/such/ld.so"}}}
                env = {"EESSI_CVMFS_REPO": os.path.join(site.root, "eessi"), "EESSI_EPREFIX": prefix}
                done = site.run(extra=extra, **env)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(
                    self.problems(site),
                    [
                        "elf: demo/1/bin/lost: loader /no/such/ld.so not found",
                        "elf: demo/1/lib/libdemo.so.1: libnot.so.1 not found (no RPATH or RUNPATH)",
                    ],
                )

    def test_references_to_the_build_and_temporary_directories(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                build, tmp = os.path.join(site.root, "build"), site.tmp
                extra = {
                    "text": {
                        "share/demo.pc": f"prefix={build}/demo/1/src\n",
                        "easybuild/demo-1.log": f"built in {build}/demo/1\n",
                        "share/runtime.txt": "a socket in /tmp/ or /tmp/tmux-1000/default\n",
                        "share/log.txt": f"see {tmp}/eb-ab12cd_9/easybuild.log\n",
                    },
                    "elf": {"lib/libr.so": {"runpath": f"/tmp/eb-build-x/lib:{build}/demo/1/lib"}},
                    "links": {"lib/libt.so": f"{tmp}/x/libt.so", "lib/libu.so": "libr.so"},
                }
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                problems = self.problems(site)
                self.assertEqual(
                    problems,
                    [
                        "references: demo/1/lib/libr.so: RUNPATH entry /tmp/eb-build-x/lib is under /tmp",
                        f"references: demo/1/lib/libr.so: names {build}/demo/1/lib, in the build directory {build}",
                        f"references: demo/1/lib/libt.so: symbolic link to {tmp}/x/libt.so, under {tmp}",
                        f"references: demo/1/share/demo.pc: names {build}/demo/1/src, in the build directory {build}",
                        f"references: demo/1/share/log.txt: names {tmp}/eb-ab12cd_9/easybuild.log, an EasyBuild temporary directory",
                    ],
                )

    def test_modules_load_from_the_stage(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(LMOD_CMD=FAKE_LMOD)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("verify r1: pass (elf 0, references 0, modules 0, access 0); 0 ELF object(s)", done.stdout)
        for site in self.sites():
            with self.subTest(tool=site.name):
                missing = os.path.join(site.root, "missing")
                done = site.run(LMOD_CMD=FAKE_LMOD, FAKE_LMOD_EBROOT=missing)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(self.problems(site), [f"modules: module file all/demo/1.lua: EBROOTDEMO names {missing}, which is not a directory"])
                os.unlink(os.path.join(site.journal()["stage_modules"], "all", "demo", "1.lua"))
                again = site.cli("verify", "--state-root", site.state, "r1", LMOD_CMD=FAKE_LMOD)
                self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
                staged = os.path.join(site.journal()["stage_modules"], "all", "demo", "1.lua")
                live = os.path.join(site.modules, "all", "demo", "1.lua")
                self.assertEqual(self.problems(site), [f"modules: module file all/demo/1.lua: Lmod loaded {live}, not {staged}"])

    def test_lmod_that_fails_or_is_missing_is_reported(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                failing = os.path.join(site.root, "lmod-fails")
                write(failing, "#!/bin/sh\necho false\nprintf 'Lmod has detected the following error:\\n  boom\\n\\nmore\\n' >&2\nexit 1\n", 0o755)
                done = site.run(LMOD_CMD=failing)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(self.problems(site), ["modules: module file all/demo/1.lua: Lmod did not load demo/1: Lmod has detected the following error: boom"])
                absent = os.path.join(site.root, "no-lmod")
                again = site.cli("verify", "--state-root", site.state, "r1", LMOD_CMD=absent)
                self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
                self.assertEqual(self.problems(site), [f"modules: module file all/demo/1.lua: cannot run {absent}: No such file or directory"])

    def test_access_that_publishing_would_change(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                os.chmod(site.live, 0o700)
                done = site.run()
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(
                    self.problems(site),
                    ["access: demo/1: the staged tree has mode 0755 and the live tree 0700; publishing would change what group and others may do"],
                )
        extra = {"text": {"share/secret": "x\n"}, "modes": {"share/secret": 0o600}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(
                    self.problems(site),
                    ["access: demo/1: 1 entries are less readable than the tree's top directory, the first demo/1/share/secret"],
                )
        for site in self.sites():
            with self.subTest(tool=site.name):
                prepare = os.path.join(site.root, "prepare")
                write(prepare, "#!/bin/sh\nexit 0\n", 0o755)
                os.chmod(site.live, 0o700)
                done = site.run("--prepare", prepare, extra=extra)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_parent_the_publisher_cannot_write(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                if os.geteuid() == 0:
                    done = site.run()
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    self.assertEqual(site.payload(), "from-eb\n")
                    continue
                parent = os.path.dirname(site.live)
                os.chmod(parent, 0o555)
                try:
                    done = site.run()
                finally:
                    os.chmod(parent, 0o755)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(self.problems(site), [f"access: demo/1: uid {os.geteuid()} cannot write {parent}"])
                self.assertEqual(site.payload(), "old\n")

    def test_more_problems_than_are_shown(self):
        extra = {"elf": {"bin/demo": {"needed": [f"lib{n:02d}.so" for n in range(60)], "runpath": "$ORIGIN"}}}
        for site in self.sites():
            with self.subTest(tool=site.name):
                done = site.run(extra=extra)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                shown = [line for line in done.stdout.splitlines() if line.startswith("eb-staged-rebuild: verify: elf: ")]
                self.assertEqual(len(shown), 50)
                self.assertIn(f"eb-staged-rebuild: verify: 10 more problem(s) in {site.report_path()}\n", done.stdout)
                self.assertEqual(len(site.report()["problems"]), 60)

    def test_verify_needs_a_built_run(self):
        for site in self.sites():
            with self.subTest(tool=site.name):
                self.assertEqual(site.run().returncode, 0)
                nothing = site.cli("verify", "--state-root", site.state, "nope")
                self.assertEqual(nothing.returncode, 2, nothing.stdout + nothing.stderr)
                self.assertIn(f"no journal at {site.state}/runs/nope/journal.json", nothing.stderr)
                complete = site.cli("verify", "--state-root", site.state, "r1")
                self.assertEqual(complete.returncode, 2, complete.stdout + complete.stderr)
                self.assertIn("run r1 is complete; only a built run has a stage to verify", complete.stderr)
                module = os.path.join(site.modules, "all", "demo", "1.lua")
                retired = site.cli("retire", "--state-root", site.state, "--module-file", module)
                self.assertEqual(retired.returncode, 0, retired.stdout + retired.stderr)
                (record,) = [name for name in os.listdir(os.path.join(site.state, "runs")) if name != "r1"]
                refused = site.cli("verify", "--state-root", site.state, record)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn(f"run {record} is a retire record; it has no stage to verify", refused.stderr)


if __name__ == "__main__":
    unittest.main()
