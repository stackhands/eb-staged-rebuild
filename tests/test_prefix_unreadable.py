import os
import tempfile
import unittest
from pathlib import Path

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.prefix import rewrite_tree

OLD = b"/state/generations/run1"
NEW = b"/stable"


class PrefixUnreadableTests(unittest.TestCase):
    def test_rewrite_refuses_an_unreadable_subdirectory(self):
        if os.geteuid() == 0:
            self.skipTest("root can still read mode-000 directories")
        with tempfile.TemporaryDirectory(prefix="prefix-unreadable-") as root:
            path = Path(root, "bin", "demo")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"#!" + OLD + b"/bin/python\n")
            denied = Path(root, "denied")
            denied.mkdir()
            denied.chmod(0)
            try:
                with self.assertRaisesRegex(Refused, f"rewrite tree is unreadable: {root}"):
                    rewrite_tree(root, OLD, NEW)
            finally:
                denied.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
