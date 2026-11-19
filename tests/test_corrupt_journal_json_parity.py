"""Parity checks for invalid journal JSON handling."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CorruptJournalJsonParityTests(unittest.TestCase):
    def test_invalid_journal_json_has_matching_refusal_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="corrupt-journal-json-") as tmp:
            state = os.path.join(tmp, "state")
            run_dir = os.path.join(state, "runs", "broken")
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "journal.json"), "w", encoding="utf-8") as handle:
                handle.write('{"run_id":"broken"')

            for args in (
                ("status", "--state-root", state),
                ("status", "--state-root", state, "--json"),
                ("gc", "--state-root", state, "--dry-run"),
                ("gc", "--state-root", state, "--dry-run", "--json"),
                ("rollback", "--state-root", state, "broken"),
                ("resume", "--state-root", state, "broken"),
            ):
                py = run_cli(*args)
                self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
                self.assertEqual(py.stdout, "")
                self.assertIn("journal cannot be read:", py.stderr)
                self.assertIn("not valid JSON", py.stderr)
                self.assertNotIn("Traceback", py.stderr)

                zig = run_cli_optional_binary(*args)
                if zig is None:
                    continue
                self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                self.assertEqual(zig.stdout, "")
                self.assertEqual(py.stderr, zig.stderr)


if __name__ == "__main__":
    unittest.main()
