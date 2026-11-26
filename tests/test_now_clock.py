"""EB_STAGED_REBUILD_NOW is a test clock.

Only the -Dtest-ops artifact reads it. A value the run id cannot record
is refused before the command. The installed binary leaves it unread.
"""

from __future__ import annotations

import os
import unittest

from tests.support import _run, _skip, golden


class TestClockTests(unittest.TestCase):
    def test_a_bad_value_is_refused_before_the_command(self) -> None:
        tool = os.environ.get("TOOL_BIN_TEST_OPS")
        if not tool:
            _skip(self, "TOOL_BIN_TEST_OPS is unset")
        cases = (
            ("bogus", "is not a number"),
            ("nan", "is not a finite number"),
            ("inf", "is not a finite number"),
            ("1e308", "is not a representable timestamp"),
            ("-1", "is not a representable timestamp"),
        )
        for value, phrase in cases:
            env = {"EB_STAGED_REBUILD_NOW": value}
            binary = _run([tool], env)
            detail = f"{value}\nbinary {binary.returncode} {binary.stderr!r}"
            self.assertEqual(binary.returncode, 2, detail)
            self.assertEqual(binary.stdout, "", detail)
            golden(self, f"stderr-{value}", binary.stderr)
            self.assertIn(phrase, binary.stderr, detail)
            self.assertIn(repr(value), binary.stderr, detail)
            self.assertNotIn("Traceback", binary.stderr)
            self.assertNotIn("OverflowError", binary.stderr)
            self.assertNotIn("panic", binary.stderr)

    def test_installed_frontends_ignore_the_clock(self) -> None:
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            _skip(self, "TOOL_BIN is unset")
        commands = ([tool, "--version"],)
        for argv in commands:
            plain = _run(argv)
            poisoned = _run(argv, {"EB_STAGED_REBUILD_NOW": "1e308"})
            detail = f"{argv}\nplain {plain.returncode} {plain.stdout!r} {plain.stderr!r}\npoisoned {poisoned.returncode} {poisoned.stdout!r} {poisoned.stderr!r}"
            self.assertEqual(plain.returncode, poisoned.returncode, detail)
            self.assertEqual(plain.stdout, poisoned.stdout, detail)
            self.assertEqual(plain.stderr, poisoned.stderr, detail)
            self.assertNotIn("OverflowError", poisoned.stderr)
            self.assertNotIn("Traceback", poisoned.stderr)
            self.assertNotIn("EB_STAGED_REBUILD_NOW", poisoned.stdout + poisoned.stderr)
