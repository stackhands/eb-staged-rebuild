"""The generation prefix is published by a same-directory replacement.

An in-place edit follows a symbolic link, tears a file that is killed
mid-write, fills a sparse hole, and chmod's a file that is not
owner-writable. chmod replaces an NFSv4 ACL with a mode ACL, so the file
itself must not be chmod'd. These tests exercise that publication.
"""

import errno
import os
import stat
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.prefix import rewrite_tree


OLD = b"/state/generations/run1"
NEW = b"/stable"


def _acl():
    entries = [(0x01, 4, 0xffffffff), (0x02, 4, 65534),
               (0x04, 4, 0xffffffff), (0x10, 4, 0xffffffff),
               (0x20, 4, 0xffffffff)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


class PrefixPublishTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"prefix-{os.getpid()}")
        os.makedirs(self.root)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for directory, dirs, files in os.walk(self.root, topdown=False):
            for name in files:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o644)
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
            if not os.path.islink(directory):
                os.chmod(directory, 0o755)
        for directory, dirs, files in os.walk(self.root, topdown=False):
            for name in files:
                os.unlink(os.path.join(directory, name))
            for name in dirs:
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    os.rmdir(path)
        os.rmdir(self.root)

    def _file(self, name, data, mode=0o644):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
        return path

    def test_a_failed_publish_leaves_the_original_bytes(self):
        path = self._file("lib/demo.pc", b"prefix=" + OLD + b"/software/demo\n")
        before = Path(path).read_bytes()
        real = os.replace

        def boom(src, dst):
            raise OSError("injected publish failure")

        with patch("eb_staged_rebuild.prefix.os.replace", side_effect=boom):
            with self.assertRaises(OSError):
                rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertFalse(os.path.islink(path))

    def test_rewrite_does_not_chmod_a_sealed_file(self):
        path = self._file("bin/demo", b"#!" + OLD + b"/bin/python\n", 0o644)
        os.setxattr(path, "user.ebsr", b"kept")
        os.setxattr(path, "system.posix_acl_access", _acl())
        acl = os.getxattr(path, "system.posix_acl_access")
        os.chmod(path, 0o444)
        os.chmod(self.root, 0o555)
        os.chmod(os.path.dirname(path), 0o555)
        seen = []
        real = os.chmod

        def spy(target, mode):
            seen.append(os.path.abspath(target))
            return real(target, mode)

        with patch("eb_staged_rebuild.prefix.os.chmod", side_effect=spy):
            rewrite_tree(self.root, OLD, NEW)
        self.assertNotIn(os.path.abspath(path), seen)
        self.assertEqual(Path(path).read_bytes(), b"#!" + NEW + b"/bin/python\n")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o444)
        self.assertEqual(os.getxattr(path, "system.posix_acl_access"), acl)
        self.assertEqual(os.getxattr(path, "user.ebsr"), b"kept")
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.dirname(path)).st_mode), 0o555)

    def test_a_sparse_file_stays_sparse(self):
        path = self._file("lib/libdemo.so", b"")
        blob = OLD + b"/lib\x00next\x00"
        fd = os.open(path, os.O_RDWR)
        try:
            os.write(fd, blob)
            os.lseek(fd, 1024 * 1024, os.SEEK_SET)
            os.write(fd, b"END")
        finally:
            os.close(fd)
        rewrite_tree(self.root, OLD, NEW)
        info = os.stat(path)
        self.assertLess(info.st_blocks, 128)
        with open(path, "rb") as handle:
            head = handle.read(len(blob) + 64)
            handle.seek(-3, os.SEEK_END)
            tail = handle.read()
        self.assertTrue(head.startswith(NEW + b"/lib\x00"))
        self.assertNotIn(OLD, head)
        self.assertEqual(tail, b"END")
        # Shortening pads the old C string with NULs. The next string stays
        # at the offset it had, which is what an ELF string table needs.
        next_at = len(OLD + b"/lib") + 1
        self.assertEqual(head[next_at:next_at + 4], b"next")

    def test_hard_links_stay_one_inode(self):
        path = self._file("bin/python", b"#!" + OLD + b"/bin/python\n")
        alias = os.path.join(self.root, "bin/python3")
        os.link(path, alias)
        before = os.lstat(path).st_ino
        rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(os.lstat(path).st_ino, os.lstat(alias).st_ino)
        self.assertNotEqual(os.lstat(path).st_ino, before)
        text = b"#!" + NEW + b"/bin/python\n"
        self.assertEqual(Path(path).read_bytes(), text)
        self.assertEqual(Path(alias).read_bytes(), text)

    def test_a_symlink_is_replaced_without_unlinking_first(self):
        os.makedirs(os.path.join(self.root, "bin"), exist_ok=True)
        path = os.path.join(self.root, "bin/alias")
        os.symlink(OLD.decode() + "/bin/python", path)
        original = os.readlink(path)

        def boom(target, link):
            raise OSError("injected symlink failure")

        with patch("eb_staged_rebuild.prefix.os.symlink", side_effect=boom):
            with self.assertRaises(OSError):
                rewrite_tree(self.root, OLD, NEW)
        self.assertTrue(os.path.islink(path))
        self.assertEqual(os.readlink(path), original)
        rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(os.readlink(path), NEW.decode() + "/bin/python")

    def test_non_utf8_bytes_keep_their_neighbors(self):
        path = self._file("share/odd", bytes([0xff]) + OLD + b"/x" + bytes([0xfe, 0x00]) + b"tail")
        rewrite_tree(self.root, OLD, NEW)
        data = Path(path).read_bytes()
        self.assertTrue(data.startswith(bytes([0xff]) + NEW + b"/x" + bytes([0xfe])))
        self.assertIn(b"tail", data)
        self.assertNotIn(OLD, data)

    def test_a_binary_without_room_to_shorten_is_refused(self):
        self._file("lib/tight.so", b"/short/lib\x00")
        with self.assertRaises(Refused):
            rewrite_tree(self.root, b"/short", b"/much/longer/stable")
        self.assertEqual(Path(self.root, "lib/tight.so").read_bytes(), b"/short/lib\x00")

    def test_the_completion_stamp_is_not_rewritten(self):
        path = self._file(".eb-staged-rebuild-complete", b'{"run_id": "x", "path": "' + OLD + b'"}\n')
        rewrite_tree(self.root, OLD, NEW)
        self.assertIn(OLD, Path(path).read_bytes())

    def test_a_symlink_is_not_followed_into_another_file(self):
        outside = self._file("outside-target", OLD + b"/secret\n")
        link = os.path.join(self.root, "looks-like-a-file")
        os.symlink(outside, link)
        # The link text is an absolute path that does not contain OLD as a
        # bounded prefix (it contains it, because the target path is under
        # root which does not include OLD). Point the link at a path that
        # does not contain OLD, while the target file does.
        os.unlink(link)
        innocent = self._file("innocent", b"untouched\n")
        os.symlink(innocent, link)
        # And a second link whose text contains the generation prefix.
        planted = os.path.join(self.root, "planted")
        os.symlink(OLD.decode() + "/bin/tool", planted)
        rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(Path(innocent).read_bytes(), b"untouched\n")
        self.assertEqual(os.readlink(planted), NEW.decode() + "/bin/tool")

    def test_a_full_filesystem_names_the_temporary_file(self):
        path = self._file("bin/tool", b"prefix " + OLD + b" suffix")
        before = Path(path).read_bytes()

        def refuse(_fd, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("eb_staged_rebuild.prefix.os.write", side_effect=refuse):
            with self.assertRaises(OSError) as caught:
                rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertTrue(str(caught.exception.filename).endswith(".ebsr-partial"))
        self.assertIn(".ebsr-partial", str(caught.exception))
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertFalse(any(name.endswith(".ebsr-partial") for name in os.listdir(os.path.dirname(path))))

    def test_a_read_only_directory_names_the_directory(self):
        path = self._file("bin/tool", b"prefix " + OLD + b" suffix")
        before = Path(path).read_bytes()
        parent = os.path.dirname(path)
        os.chmod(parent, 0o555)

        def refuse(_fd, _mode):
            raise OSError(errno.EROFS, "Read-only file system")

        with patch("eb_staged_rebuild.prefix.os.fchmod", side_effect=refuse):
            with self.assertRaises(OSError) as caught:
                rewrite_tree(self.root, OLD, NEW)
        self.assertEqual(caught.exception.errno, errno.EROFS)
        self.assertEqual(caught.exception.filename, parent)
        self.assertIn(parent, str(caught.exception))
        self.assertEqual(Path(path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
