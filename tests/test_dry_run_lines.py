"""The module preflight reads the module list from eb --dry-run.

The recorded text is EasyBuild 5.4.2 on EESSI 2026.06 for
``eb --dry-run [--robot] --rebuild pigz-2.8-GCCcore-15.2.0.eb``. With stdout
not a terminal, its rich output wraps every line at 80 columns.
"""

import dataclasses
import json
import os
import stat
import tempfile
import unittest

from eb_staged_rebuild.engine import Spec, run, software_rels_from_dry_run
from eb_staged_rebuild.errors import Refused
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
# EasyBuild 5.4.2, eb --extended-dry-run Top-1.0-GCCcore-15.2.0.eb
# --robot=$HOME/easyconfigs: --rebuild --stop=fetch --ignore-locks, where Top
# needs Dep and neither is installed: the output of each easyconfig up to its
# first step, and the summary.
NOTE = (
    "Important note: the actual build & install procedure that will be performed may diverge",
    "(slightly) from what is outlined below, due to conditions in the easyblock which are",
    "incorrectly handled in a dry run.",
    "Any errors that may occur are ignored and reported as warnings, on a per-step basis.",
    "Please be aware of this, and only use the information below for quick debugging purposes.",
    "",
    "*** DRY RUN using 'Binary' easyblock (easybuild.easyblocks.generic.binary @ "
    "/opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/binary.py) ***",
    "",
)
EXTENDED = (
    "== resolving dependencies ...",
    "",
    "== processing EasyBuild easyconfig /home/alice/easyconfigs/Dep-1.0-GCCcore-15.2.0.eb",
    "",
    *NOTE,
    "== building and installing Dep/1.0-GCCcore-15.2.0...",
    "  >> installation prefix: /tmp/eb-0yw4vdhi/__ROOT__/opt/site/software/Dep/1.0-GCCcore-15.2.0",
    "fetching files and verifying checksums... [DRY RUN]",
    "",
    "== processing EasyBuild easyconfig /home/alice/easyconfigs/Top-1.0-GCCcore-15.2.0.eb",
    "",
    *NOTE,
    "== building and installing Top/1.0-GCCcore-15.2.0...",
    "  >> installation prefix: /tmp/eb-0yw4vdhi/__ROOT__/opt/site/software/Top/1.0-GCCcore-15.2.0",
    "fetching files and verifying checksums... [DRY RUN]",
    "",
    "== Build succeeded for 2 out of 2 (total: 0 secs)",
    "== Summary:",
    "   * [SUCCESS] Dep/1.0-GCCcore-15.2.0",
    "   * [SUCCESS] Top/1.0-GCCcore-15.2.0",
)
# EasyBuild 5.4.2 with rich 15.0.0 on the example site, as alice on login1,
# with FORCE_COLOR=1 and TERM=xterm-256color: eb --dry-run [--robot]
# --rebuild Example-1.0-GCCcore-15.2.0.eb, and of eb --extended-dry-run
# --robot --rebuild --stop=fetch --ignore-locks the start, the blocks of the
# first and the last of the eight easyconfigs up to their first step, and the
# end. Once FORCE_COLOR is set and not empty, rich writes ANSI escape
# sequences to a pipe too, and still wraps the lines at 80 columns.
COLOR_REBUILD = (
    "== Temporary log file in case of crash /tmp/eb-0uh_10us/easybuild-vkclmi_q.log",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "Dry run: printing build status of easyconfigs",
    " * \x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m \x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m",
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-0uh_10us/\x1b[0m\x1b[95measybuild-vkclmi_q.log\x1b[0m* have been ",
    "removed.",
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-0uh_10us\x1b[0m has been removed.",
)
COLOR_ROBOT = (
    "== Temporary log file in case of crash /tmp/eb-x1zw5cqf/easybuild-4u5bzs64.log",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "Dry run: printing build status of easyconfigs and dependencies",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.20.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/z/zlib/\x1b[0m\x1b[95mzlib-1.3.1.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "zlib/\x1b[1;36m1.3\x1b[0m.\x1b[1;36m1\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.19.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m19\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/b/Bison/\x1b[0m\x1b[95mBison-3.8.2.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "Bison/\x1b[1;36m3.8\x1b[0m.\x1b[1;36m2\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/f/flex/\x1b[0m\x1b[95mflex-2.6.4.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "flex/\x1b[1;36m2.6\x1b[0m.\x1b[1;36m4\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/b/binutils/\x1b[0m\x1b[95mbinutils-2.45.eb\x1b[0m \x1b[1m(\x1b[0mmodule:",
    "binutils/\x1b[1;36m2.45\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/g/GCCcore/\x1b[0m\x1b[95mGCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule:",
    "GCCcore/\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m",
    " * \x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m \x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
    "Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m",
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-x1zw5cqf/\x1b[0m\x1b[95measybuild-4u5bzs64.log\x1b[0m* have been ",
    "removed.",
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-x1zw5cqf\x1b[0m has been removed.",
)
COLOR_EXTENDED = (
    "== Temporary log file in case of crash /tmp/eb-tz2pzp7y/easybuild-_v17l6i5.log",
    "file \x1b[35m/tmp/eb-tz2pzp7y/\x1b[0m\x1b[95mfake_vsc_mleiqnwz\x1b[0m removed",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
    "== resolving dependencies \x1b[33m...\x1b[0m",
    "",
    "== processing EasyBuild easyconfig ",
    "\x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.20.eb\x1b[0m",
    "",
    "Important note: the actual build & install procedure that will be performed may ",
    "diverge",
    "\x1b[1m(\x1b[0mslightly\x1b[1m)\x1b[0m from what is outlined below, due to conditions in the easyblock which",
    "are",
    "incorrectly handled in a dry run.",
    "Any errors that may occur are ignored and reported as warnings, on a per-step ",
    "basis.",
    "Please be aware of this, and only use the information below for quick debugging ",
    "purposes.",
    "",
    "*** DRY RUN using \x1b[32m'ConfigureMake'\x1b[0m easyblock ",
    "\x1b[1m(\x1b[0measybuild.easyblocks.generic.configuremake @ ",
    "\x1b[35m/opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/\x1b[0m\x1b[95mconfigu\x1b[0m",
    "\x1b[95mremake.py\x1b[0m\x1b[1m)\x1b[0m ***",
    "",
    "== building and installing M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m\x1b[33m...\x1b[0m",
    "  >> installation prefix: \x1b[35m/tmp/eb-tz2pzp7y/__ROOT__/opt/site/software/M4/\x1b[0m\x1b[95m1.4.20\x1b[0m",
    "fetching files and verifying checksums\x1b[33m...\x1b[0m \x1b[1m[\x1b[0mDRY RUN\x1b[1m]\x1b[0m",
    "== processing EasyBuild easyconfig ",
    "\x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m",
    "",
    "Important note: the actual build & install procedure that will be performed may ",
    "diverge",
    "\x1b[1m(\x1b[0mslightly\x1b[1m)\x1b[0m from what is outlined below, due to conditions in the easyblock which",
    "are",
    "incorrectly handled in a dry run.",
    "Any errors that may occur are ignored and reported as warnings, on a per-step ",
    "basis.",
    "Please be aware of this, and only use the information below for quick debugging ",
    "purposes.",
    "",
    "*** DRY RUN using \x1b[32m'Binary'\x1b[0m easyblock \x1b[1m(\x1b[0measybuild.easyblocks.generic.binary @ ",
    "\x1b[35m/opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/\x1b[0m\x1b[95mbinary.\x1b[0m",
    "\x1b[95mpy\x1b[0m\x1b[1m)\x1b[0m ***",
    "",
    "== building and installing Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[33m...\x1b[0m",
    "  >> installation prefix: ",
    "\x1b[35m/tmp/eb-tz2pzp7y/__ROOT__/opt/site/software/Example/\x1b[0m\x1b[95m1.0-GCCcore-15.2.0\x1b[0m",
    "fetching files and verifying checksums\x1b[33m...\x1b[0m \x1b[1m[\x1b[0mDRY RUN\x1b[1m]\x1b[0m",
    "== Build succeeded for \x1b[1;36m8\x1b[0m out of \x1b[1;36m8\x1b[0m \x1b[1m(\x1b[0mtotal: \x1b[1;36m7\x1b[0m mins \x1b[1;36m0\x1b[0m secs\x1b[1m)\x1b[0m",
    "== Summary:",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m zlib/\x1b[1;36m1.3\x1b[0m.\x1b[1;36m1\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m19\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m Bison/\x1b[1;36m3.8\x1b[0m.\x1b[1;36m2\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m flex/\x1b[1;36m2.6\x1b[0m.\x1b[1;36m4\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m binutils/\x1b[1;36m2.45\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m GCCcore/\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m",
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m",
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-tz2pzp7y/\x1b[0m\x1b[95measybuild-_v17l6i5.log\x1b[0m* have been ",
    "removed.",
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-tz2pzp7y\x1b[0m has been removed.",
)
COLOR_ROBOT_RELS = [
    "M4/1.4.20",
    "zlib/1.3.1",
    "M4/1.4.19",
    "Bison/3.8.2",
    "flex/2.6.4",
    "binutils/2.45",
    "GCCcore/15.2.0",
    "Example/1.0-GCCcore-15.2.0",
]
# Text, and the text without its whole CSI and OSC sequences; the Zig test of
# stripEscapes has the same cases.
ESCAPES = (
    ("", ""),
    ("plain", "plain"),
    ("\x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m", "[R]"),
    ("\x1b[4;94mhttps://x\x1b[0m", "https://x"),
    ("a\x1b[ qb", "ab"),
    ("\x1b]8;;file:///a\x1b\\a\x1b]8;;\x1b\\", "a"),
    ("\x1b]0;title\x07x", "x"),
    ("a\x1b[12", "a\x1b[12"),
    ("a\x1b[1\nm", "a\x1b[1\nm"),
    ("a\x1b]8;;x", "a\x1b]8;;x"),
    ("\x1b]8;;x\ny\x07", "\x1b]8;;x\ny\x07"),
    ("\x1b(B", "\x1b(B"),
    ("\x1b", "\x1b"),
    ("\x1b\x1b[1mx", "\x1bx"),
    ("\x1b]x\x1b[1my\x07", "\x1b]xy\x07"),
    ("\x1b[1m\u00e9\x1b[0m", "\u00e9"),
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

    def test_the_extended_dry_run_names_the_modules_it_builds(self):
        from eb_staged_rebuild.engine import modules_built_by_extended_dry_run

        built = ["Dep/1.0-GCCcore-15.2.0", "Top/1.0-GCCcore-15.2.0"]
        self.assertEqual(modules_built_by_extended_dry_run(_text(EXTENDED)), built)
        for width in (24, 30, 40, 80):
            wrapped = [piece for line in EXTENDED for piece in wrap(line, width)]
            self.assertEqual(modules_built_by_extended_dry_run(_text(wrapped)), built, width)
        self.assertEqual(modules_built_by_extended_dry_run("== building and installing a/1\n== done\n"), [])

    def test_escape_sequences_in_rich_output_are_left_out(self):
        from eb_staged_rebuild.engine import modules_built_by_extended_dry_run

        self.assertEqual(software_rels_from_dry_run(_text(COLOR_REBUILD)), ["Example/1.0-GCCcore-15.2.0"])
        self.assertEqual(software_rels_from_dry_run(_text(COLOR_ROBOT)), COLOR_ROBOT_RELS)
        self.assertEqual(
            modules_built_by_extended_dry_run(_text(COLOR_EXTENDED)),
            ["M4/1.4.20", "Example/1.0-GCCcore-15.2.0"],
        )

    def test_only_whole_csi_and_osc_sequences_are_left_out(self):
        from eb_staged_rebuild.engine import strip_escapes

        for text, plain in ESCAPES:
            with self.subTest(text=text):
                self.assertEqual(strip_escapes(text), plain)
        linked = " * [R] \x1b]8;;file:///x/a.eb\x1b\\/x/a.eb\x1b]8;;\x1b\\ (module: \x1b]8;;x\x07a/1\x1b]8;;\x07)\n"
        self.assertEqual(software_rels_from_dry_run(linked), ["a/1"])


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
        for key in (
            "FAKE_EB_FAIL",
            "FAKE_EB_RELS",
            "FAKE_EB_DEPS",
            "FAKE_EB_WRAP",
            "FAKE_EB_UNBUILT",
            "FAKE_EB_MISSING",
            "FAKE_EB_XFAIL",
            "FAKE_EB_RICH",
            "FAKE_EB_STYLE",
        ):
            os.environ.pop(key, None)
        os.environ["FAKE_EB_ARGV"] = self.argv
        os.environ["FAKE_EB_PAYLOAD"] = "v1\n"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _spec(self, *eb_args):
        return Spec(
            software=self.software,
            modules=self.modules,
            state_root=os.path.join(self.root, "state"),
            eb_args=["demo-1.eb", *eb_args],
            backend="installpath",
            eb_executable=FAKE_EB,
            probes=json.loads(json.dumps(PROBES)),
            run_id="run1",
        )

    def _refused(self, *eb_args):
        with self.assertRaises(Refused) as caught:
            run(self._spec(*eb_args))
        with open(os.path.join(self.install, "PAYLOAD"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "v0\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "runs", "run1")))
        return str(caught.exception)

    def _run(self, *eb_args):
        result = run(self._spec(*eb_args))
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

    def test_robot_leaves_out_dependencies_eb_would_not_build(self):
        os.environ["FAKE_EB_DEPS"] = "GCCcore/15.2.0"
        os.environ["FAKE_EB_UNBUILT"] = "M4/1.4.20,zlib/1.3.1"
        commands = self._run("--robot")
        journal = os.path.join(self.root, "state", "runs", "run1", "journal.json")
        with open(journal, encoding="utf-8") as handle:
            plan = json.load(handle)["modules_plan"]
        self.assertEqual([item["rel"] for item in plan], ["all/demo/1.lua"])
        self.assertEqual(
            commands[1],
            ["--extended-dry-run", "demo-1.eb", "--robot", "--rebuild", "--stop=fetch", "--ignore-locks"],
        )
        self.assertEqual(sorted(os.listdir(self.software)), ["demo"])

    def test_bwrap_mount_points_of_unbuilt_dependencies_are_removed(self):
        os.makedirs(os.path.join(self.software, "M4", "1.4.19"))
        os.environ["FAKE_EB_UNBUILT"] = "M4/1.4.20,zlib/1.3.1"
        spec = self._spec("--robot")
        spec.backend = "bwrap"
        result = run(spec)
        self.assertEqual(result.returncode, 0, result.message)
        self.assertEqual(sorted(os.listdir(self.software)), ["M4", "demo"])
        self.assertEqual(os.listdir(os.path.join(self.software, "M4")), ["1.4.19"])
        with open(os.path.join(self.root, "state", "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["mount_points"], ["M4/1.4.20", "zlib/1.3.1", "zlib"])

    def test_wrapped_extended_dry_run_lines_name_the_modules(self):
        os.environ["FAKE_EB_UNBUILT"] = "M4/1.4.20"
        os.environ["FAKE_EB_WRAP"] = "24"
        self._run("--robot")

    def test_robot_refuses_a_missing_dependency_that_eb_would_build(self):
        os.environ["FAKE_EB_UNBUILT"] = "M4/1.4.20"
        os.environ["FAKE_EB_MISSING"] = "dep/2"
        message = self._refused("--robot")
        self.assertIn(os.path.join(self.software, "dep", "2"), message)

    def test_force_color_does_not_hide_the_module_lines(self):
        styles = os.path.join(self.root, "styles.log")
        os.environ.update(
            {
                "FAKE_EB_RICH": "1",
                "FORCE_COLOR": "0",
                "TERM": "xterm-256color",
                "EASYBUILD_OUTPUT_STYLE": "rich",
                "FAKE_EB_UNBUILT": "M4/1.4.20",
                "FAKE_EB_STYLE": styles,
            }
        )
        self._run("--robot")
        with open(styles, encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), ["basic", "basic", "rich"])

    def test_escape_sequences_that_reach_the_module_preflight_are_left_out(self):
        os.environ.update(
            {
                "FAKE_EB_RICH": "1",
                "FORCE_COLOR": "1",
                "TERM": "xterm-256color",
                "FAKE_EB_UNBUILT": "M4/1.4.20",
                "FAKE_EB_WRAP": "24",
            }
        )
        self._run("--output-style=rich", "--robot")

    def test_force_color_does_not_hide_a_dependency_eb_would_build(self):
        os.environ.update(
            {"FAKE_EB_RICH": "1", "FORCE_COLOR": "1", "TERM": "xterm-256color", "FAKE_EB_MISSING": "dep/2"}
        )
        with self.assertRaises(Refused) as caught:
            run(dataclasses.replace(self._spec("--robot"), module_rels=["demo/1"]))
        self.assertIn("dep/2 is missing, not an existing rebuild directory", str(caught.exception))

    def test_an_extended_dry_run_that_fails_refuses(self):
        os.environ["FAKE_EB_UNBUILT"] = "M4/1.4.20"
        os.environ["FAKE_EB_XFAIL"] = "31"
        message = self._refused("--robot")
        self.assertIn("eb --extended-dry-run exited 31", message)
        self.assertIn("Missing dependencies: dep/9", message)


if __name__ == "__main__":
    unittest.main()
