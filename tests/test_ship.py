"""A checksum list of an install tree, for a node that does not share the disk."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from eb_staged_rebuild.ship import main, snapshot, verify, write_manifest


class ShipTests(unittest.TestCase):
    def test_a_copy_matches_until_a_byte_or_a_link_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            (root / "software" / "Foo").mkdir(parents=True)
            (root / "software" / "Foo" / "a.txt").write_bytes(b"hello\n")
            os.symlink("a.txt", root / "software" / "Foo" / "b.link")
            manifest = Path(tmp) / "MANIFEST"
            write_manifest(root, manifest)
            text = manifest.read_text()
            self.assertIn(hashlib.sha256(b"hello\n").hexdigest(), text)
            self.assertEqual(verify(root, manifest), [])
            (root / "software" / "Foo" / "a.txt").write_bytes(b"hello!\n")
            problems = verify(root, manifest)
            self.assertTrue(any("sha256" in item and "a.txt" in item for item in problems), problems)
            (root / "software" / "Foo" / "a.txt").write_bytes(b"hello\n")
            os.remove(root / "software" / "Foo" / "b.link")
            os.symlink("missing", root / "software" / "Foo" / "b.link")
            problems = verify(root, manifest)
            self.assertTrue(any("link" in item and "b.link" in item for item in problems), problems)

    def test_the_command_is_quiet_when_the_tree_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            root.mkdir()
            (root / "only.txt").write_text("x")
            manifest = Path(tmp) / "MANIFEST"
            self.assertEqual(main(["write", str(root), str(manifest)]), 0)
            self.assertEqual(main(["verify", str(root), str(manifest)]), 0)
            (root / "extra.txt").write_text("y")
            self.assertEqual(main(["verify", str(root), str(manifest)]), 1)

    def test_paths_do_not_escape_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            root.mkdir()
            (root / "ok.txt").write_text("z")
            rows = snapshot(root)
            self.assertEqual([row["path"] for row in rows], ["ok.txt"])
            self.assertTrue(all(".." not in row["path"] for row in rows))

    def test_the_how_to_names_the_command(self):
        page = Path(__file__).resolve().parents[1] / "docs" / "orgmode" / "howto" / "check-a-copied-install.org"
        text = page.read_text()
        self.assertIn("eb_staged_rebuild.ship", text)
        self.assertIn("verify", text)


if __name__ == "__main__":
    unittest.main()
