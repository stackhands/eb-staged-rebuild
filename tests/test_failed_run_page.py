"""The rebuild how-to says what to do after a build that did not swap."""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "docs", "orgmode", "howto", "rebuild.org")


class FailedRunPageTests(unittest.TestCase):
    def test_the_page_says_what_follows_a_build_that_did_not_swap(self):
        with open(PAGE, encoding="utf-8") as handle:
            text = handle.read()
        for needle in (
            "eb exited N; live modules were not changed",
            "There is no =swap= command",
            "status --state-root DIR RUN",
            "YYYYMMDD_HHMMSS",
            "keep_failed",
            "invalid choice: 'swap'",
        ):
            self.assertIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
