"""The module preflight reads the module list from eb --dry-run.

The recorded text is EasyBuild 5.4.2 on EESSI 2026.06 for
``eb --dry-run [--robot] --rebuild pigz-2.8-GCCcore-15.2.0.eb``. With stdout
not a terminal, its rich output wraps every line at 80 columns.
"""

import json
import os
import stat
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run, software_rels_from_dry_run
from tests.fake_eb import wrap
from tests.support import PROBES, classic_install

FAKE_EB = os.path.join(os.path.dirname(__file__), "fake_eb.py")

REBUILD_80 = (
    "Dry run: printing build status of easyconfigs",
    " * [R] ",
    "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirera",
    "pids/software/EasyBuild/5.4.2/easybuild/easyconfigs/p/pigz/pigz-2.8-GCCcore-15.2",
    ".0.eb (module: pigz/2.8-GCCcore-15.2.0)",
)
ROBOT = (
    "Dry run: printing build status of easyconfigs and dependencies",
    " * [x] /cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirerapids/software/EasyBuild/5.4.2/easybuild/easyconfigs/m/M4/M4-1.4.20.eb (module: M4/1.4.20)",
    " * [x] /cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirerapids/software/EasyBuild/5.4.2/easybuild/easyconfigs/g/GCCcore/GCCcore-15.2.0.eb (module: GCCcore/15.2.0)",
    " * [R] /cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirerapids/software/EasyBuild/5.4.2/easybuild/easyconfigs/p/pigz/pigz-2.8-GCCcore-15.2.0.eb (module: pigz/2.8-GCCcore-15.2.0)",
)
ROBOT_80 = (
    "Dry run: printing build status of easyconfigs and dependencies",
    " * [x] ",
    "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirera",
    "pids/software/EasyBuild/5.4.2/easybuild/easyconfigs/m/M4/M4-1.4.20.eb (module: ",
    "M4/1.4.20)",
    " * [x] ",
    "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirera",
    "pids/software/EasyBuild/5.4.2/easybuild/easyconfigs/g/GCCcore/GCCcore-15.2.0.eb ",
    "(module: GCCcore/15.2.0)",
    " * [R] ",
    "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirera",
    "pids/software/EasyBuild/5.4.2/easybuild/easyconfigs/p/pigz/pigz-2.8-GCCcore-15.2",
    ".0.eb (module: pigz/2.8-GCCcore-15.2.0)",
)
TAIL = (
    "== Temporary log file(s) /tmp/eb-o62b5_19/easybuild-q30ibs1n.log* have been ",
    "removed.",
    "== Temporary directory /tmp/eb-o62b5_19 has been removed.",
)


def _text(*parts):
    return "\n".join(line for part in parts for line in part) + "\n"


class ParseTests(unittest.TestCase):
    def test_a_wrapped_entry_is_read_whole(self):
        self.assertEqual(software_rels_from_dry_run(_text(REBUILD_80, TAIL)), ["pigz/2.8-GCCcore-15.2.0"])

    def test_installed_dependencies_are_left_out(self):
        self.assertEqual(software_rels_from_dry_run(_text(ROBOT, TAIL)), ["pigz/2.8-GCCcore-15.2.0"])
        self.assertEqual(software_rels_from_dry_run(_text(ROBOT_80, TAIL)), ["pigz/2.8-GCCcore-15.2.0"])

    def test_an_entry_ends_at_the_next_entry_or_log_line(self):
        text = " * [R] /x/a.eb (module: a/1\n * [ ] /x/b.eb (module: \nb/1)\n * [F] /x/c.eb\n== done\n(module: c/1)\n"
        self.assertEqual(software_rels_from_dry_run(text), ["b/1"])

    def test_the_fake_wraps_as_easybuild_does(self):
        wrapped = [piece for line in ROBOT for piece in wrap(line, 80)]
        self.assertEqual(tuple(wrapped), ROBOT_80)


class RunTests(unittest.TestCase):
    def setUp(self):
        os.chmod(FAKE_EB, os.stat(FAKE_EB).st_mode | stat.S_IEXEC)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.software = os.path.join(self.root, "software")
        self.modules = os.path.join(self.root, "modules")
        os.makedirs(self.software)
        os.makedirs(self.modules)
        self.install, self.module, _ = classic_install(self.software, self.modules, "demo/1", "v0\n", "all/demo/1.lua")
        self.argv = os.path.join(self.root, "argv.log")
        self._env = dict(os.environ)
        for key in ("FAKE_EB_FAIL", "FAKE_EB_RELS", "FAKE_EB_DEPS", "FAKE_EB_WRAP"):
            os.environ.pop(key, None)
        os.environ["FAKE_EB_ARGV"] = self.argv
        os.environ["FAKE_EB_PAYLOAD"] = "v1\n"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(self, *eb_args):
        spec = Spec(
            software=self.software,
            modules=self.modules,
            state_root=os.path.join(self.root, "state"),
            eb_args=["demo-1.eb", *eb_args],
            backend="installpath",
            eb_executable=FAKE_EB,
            probes=json.loads(json.dumps(PROBES)),
            run_id="run1",
        )
        result = run(spec)
        self.assertEqual(result.returncode, 0, result.message)
        with open(os.path.join(self.install, "PAYLOAD"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "v1\n")
        with open(self.argv, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_a_wrapped_module_line_names_the_module(self):
        os.environ["FAKE_EB_WRAP"] = "24"
        self._run()

    def test_robot_rebuilds_only_the_listed_easyconfig(self):
        os.environ["FAKE_EB_DEPS"] = "M4/1.4.20,GCCcore/15.2.0"
        commands = self._run("--robot")
        journal = os.path.join(self.root, "state", "runs", "run1", "journal.json")
        with open(journal, encoding="utf-8") as handle:
            plan = json.load(handle)["modules_plan"]
        self.assertEqual([item["rel"] for item in plan], ["all/demo/1.lua"])
        self.assertEqual(commands[0], ["--dry-run", "demo-1.eb", "--robot", "--rebuild"])


if __name__ == "__main__":
    unittest.main()
