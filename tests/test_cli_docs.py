"""Diff argparse options in cli.py against the published docs.

Run as a script: missing names are printed, and a clean tree prints nothing.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

from eb_staged_rebuild.cli import _parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERNAL_ID = re.compile(r"\b(?:open|ns)-\d{3}\b")


def internal_ids():
    """Lines of the docs and sources that cite a design-note id such as ns-051."""
    paths = [os.path.join(ROOT, name) for name in ("README.md", "DESIGN.md", "COMPAT.md", "CHANGELOG.md", "TESTMAP.md")]
    for sub, suffixes in (
        ("changelog.d", (".md",)),
        ("docs", (".md", ".org")),
        ("eb_staged_rebuild", (".py",)),
        ("zig", (".zig",)),
    ):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, sub)):
            dirnames[:] = [name for name in dirnames if name not in {"build", "__pycache__"}]
            paths.extend(os.path.join(dirpath, name) for name in filenames if name.endswith(suffixes))
    found = []
    for path in sorted(paths):
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if INTERNAL_ID.search(line):
                    found.append(f"{os.path.relpath(path, ROOT)}:{number}: {line.strip()}")
    return found


def documentation_files():
    """README plus the Org pages the Sphinx site is built from."""
    found = [os.path.join(ROOT, "README.md")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "docs", "orgmode")):
        dirnames.sort()
        found.extend(os.path.join(dirpath, name) for name in sorted(filenames) if name.endswith(".org"))
    return found


def argparse_names():
    """Long options, subcommands, and choice values exposed by the parser."""
    names = []
    parser = _parser()
    for action in parser._actions:
        if action.option_strings:
            names.extend(opt for opt in action.option_strings if opt.startswith("--"))
        sub = getattr(action, "choices", None)
        if not isinstance(sub, dict):
            continue
        for command, subparser in sub.items():
            names.append(command)
            for sub_action in subparser._actions:
                names.extend(opt for opt in sub_action.option_strings if opt.startswith("--"))
                if sub_action.choices:
                    names.extend(str(choice) for choice in sub_action.choices)
    with open(os.path.join(ROOT, "eb_staged_rebuild", "cli.py"), encoding="utf-8") as handle:
        source = handle.read()
    names.extend(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
    return names


def missing_from_docs():
    text = []
    for path in documentation_files():
        with open(path, encoding="utf-8") as handle:
            text.append(handle.read())
    combined = "\n".join(text)
    missing = []
    seen = set()
    for name in argparse_names():
        if name in seen or name in combined:
            seen.add(name)
            continue
        seen.add(name)
        missing.append(name)
    return missing


def main():
    missing = missing_from_docs()
    for name in missing:
        print(name)
    return 1 if missing else 0


class CliDocTests(unittest.TestCase):
    def test_docs_name_every_cli_option(self):
        self.assertEqual(missing_from_docs(), [])

    def test_first_tutorial_starts_on_clean_ubuntu(self):
        path = os.path.join(ROOT, "docs", "orgmode", "tutorials", "first-staged-rebuild.org")
        with open(path, encoding="utf-8") as handle:
            tutorial = handle.read()
        self.assertIn("python3-venv", tutorial)

    def test_transcripts_do_not_record_a_checkout_path(self):
        """Recorded output must not name the machine that recorded it.

        The pages show paths under the example site and the reader's home
        directory, never the checkout the output was recorded in.
        """
        bad = []
        absolute = re.compile(r"/[^\s\"'`]*eb-staged-rebuild/docs/examples/eb-standin")
        for path in documentation_files():
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if "/agent/" in line or absolute.search(line):
                        bad.append(f"{os.path.relpath(path, ROOT)}:{number}: {line.strip()}")
        self.assertEqual(bad, [])

    def test_no_design_note_ids_in_docs_or_output(self):
        """The docs, the sources and probe's lock line say what is unmeasured
        in words; a reader has no copy of the notes that numbered it.
        """
        self.assertEqual(internal_ids(), [])
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.run(
                [sys.executable, "-m", "eb_staged_rebuild", "probe", "--path", directory, "--eb", "missing-eb"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
        self.assertIn("lock: local flock lock plus POSIX record lock", proc.stdout)
        self.assertIsNone(INTERNAL_ID.search(proc.stdout + proc.stderr))


if __name__ == "__main__":
    raise SystemExit(main())
