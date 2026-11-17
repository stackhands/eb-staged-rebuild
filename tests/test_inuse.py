"""The mount table and maps parsing the in-use check of gc relies on."""

import unittest

from eb_staged_rebuild.inuse import mapped, mounts, source


class ParsingTests(unittest.TestCase):
    def test_mountinfo_fields_are_unescaped_and_a_trees_path_follows_the_mount_that_holds_it(self):
        table = mounts(
            b"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            b"40 22 0:52 /data /apps rw - nfs4 server:/export rw\n"
            b"41 22 0:52 /data/software/my\\040tree /mnt/a\\134b rw - nfs4 server:/export rw\n"
            b"short line\n"
        )
        self.assertEqual(len(table), 3)
        self.assertEqual(table[2], (b"0:52", b"/data/software/my tree", b"/mnt/a\\b"))
        self.assertEqual(source(b"/apps/software/my tree", table), (b"0:52", b"/data/software/my tree"))
        self.assertEqual(source(b"/apps", table), (b"0:52", b"/data"))
        self.assertEqual(source(b"/appsx", table), (b"8:1", b"/appsx"))
        self.assertEqual(source(b"/etc", table), (b"8:1", b"/etc"))
        self.assertIsNone(source(b"/etc", []))

    def test_a_maps_line_names_the_file_after_its_first_five_fields(self):
        maps = (
            b"7f00-7f01 r--p 00000000 08:01 1234    /lib/libz.so.1 (deleted)\n"
            b"5600-5601 rw-p 00000000 00:00 0  [heap]\n"
            b"7f00-7f01 r--p 00000000 00:00 0   \n"
        )
        self.assertEqual(list(mapped(maps)), [b"/lib/libz.so.1 (deleted)", b"[heap]"])
        self.assertEqual(list(mapped(b"")), [])
        self.assertEqual(list(mapped(None)), [])


if __name__ == "__main__":
    unittest.main()
