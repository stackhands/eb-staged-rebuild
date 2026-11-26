"""Refusal text for invalid journal JSON."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CorruptJournalJsonParityTests(unittest.TestCase):
    def test_invalid_journal_json_has_matching_refusal_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="corrupt-journal-json-") as tmp:
            state = os.path.join(tmp, "state")
            run_dir = os.path.join(state, "runs", "broken")
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "journal.json"), "w", encoding="utf-8") as handle:
                handle.write('{"run_id":"broken"')

            for index, args in enumerate(
                (
                    ("status", "--state-root", state),
                    ("status", "--state-root", state, "--json"),
                    ("gc", "--state-root", state, "--dry-run"),
                    ("gc", "--state-root", state, "--dry-run", "--json"),
                    ("rollback", "--state-root", state, "broken"),
                    ("resume", "--state-root", state, "broken"),
                )
            ):
                with self.subTest(args=args):
                    zig = run_cli(*args)
                    self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                    self.assertEqual(zig.stdout, "")
                    self.assertIn("journal cannot be read:", zig.stderr)
                    self.assertIn("not valid JSON", zig.stderr)
                    self.assertNotIn("Traceback", zig.stderr)
                    golden(self, f"stderr-{index}", scrub(zig.stderr, STATE=state))


if __name__ == "__main__":
    unittest.main()
