"""A trailing slash must not hide a symlinked software or modules root."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import abspath, run_cli

_CLEAR = (
    "EB_STAGED_REBUILD_STATE_ROOT",
    "EASYBUILD_INSTALLPATH_SOFTWARE",
    "EASYBUILD_INSTALLPATH_MODULES",
    "EB_STAGED_REBUILD_MIN_FREE_BYTES",
)


class SoftwareSymlinkSlashTests(unittest.TestCase):
    def _run(self, *args: str, cwd: str | None = None):
        return run_cli(*args, cwd=cwd, clear=_CLEAR)

    def test_trailing_slash_still_refuses_a_symlinked_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="root-slash-") as tmp:
            real_software = os.path.join(tmp, "software-real")
            real_modules = os.path.join(tmp, "modules-real")
            os.makedirs(real_software)
            os.makedirs(real_modules)
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            os.symlink(real_software, software)
            os.symlink(real_modules, modules)
            state = os.path.join(tmp, "state")

            cases = (
                (
                    "software-no-slash",
                    (software, real_modules),
                    None,
                    f"software path is a symlink ({abspath(software)}); Name/version must stay a real directory",
                ),
                (
                    "software-slash",
                    (software + "/", real_modules),
                    None,
                    f"software path is a symlink ({abspath(software)}); Name/version must stay a real directory",
                ),
                (
                    "software-double-slash",
                    (software + "//", real_modules),
                    None,
                    f"software path is a symlink ({abspath(software)}); Name/version must stay a real directory",
                ),
                (
                    "modules-slash",
                    (real_software, modules + "/"),
                    None,
                    f"modules path is a symlink: {abspath(modules)}",
                ),
                (
                    "relative-software-slash",
                    ("software/", real_modules),
                    tmp,
                    f"software path is a symlink ({abspath(software)}); Name/version must stay a real directory",
                ),
            )
            for label, roots, cwd, message in cases:
                with self.subTest(case=label):
                    done = self._run(
                        "run",
                        "--software",
                        roots[0],
                        "--modules",
                        roots[1],
                        "--state-root",
                        state,
                        "--dry-run",
                        "--",
                        "demo.eb",
                        cwd=cwd,
                    )
                    self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                    self.assertEqual(done.stdout, "")
                    self.assertEqual(done.stderr, f"eb-staged-rebuild: {message}\n")

    def test_a_normalized_real_directory_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="root-real-slash-") as tmp:
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            os.makedirs(software)
            os.makedirs(modules)
            link = os.path.join(tmp, "link")
            os.symlink(os.path.join(tmp, "elsewhere"), link)
            state = os.path.join(tmp, "state")
            done = self._run(
                "run",
                "--software",
                os.path.join(link, "..", "software") + "/",
                "--modules",
                modules + "/",
                "--state-root",
                state,
                "--dry-run",
                "--",
                "demo.eb",
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertNotIn("is a symlink", done.stdout + done.stderr)
            self.assertIn(f"software: {software}", done.stdout)
            self.assertIn(f"modules: {modules}", done.stdout)


if __name__ == "__main__":
    unittest.main()
