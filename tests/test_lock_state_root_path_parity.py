"""State-root lock path normalization and error messages."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import hold_state_root_lock, run_cli


class LockStateRootPathParityTests(unittest.TestCase):
    def _assert_run(
        self,
        args: tuple[str, ...],
        *,
        expected_rc: int,
        expected_stderr: str,
        cwd: str | None = None,
    ) -> None:
        done = run_cli(*args, cwd=cwd)
        self.assertEqual(done.returncode, expected_rc, done.stdout + done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, expected_stderr)

    def test_symlink_state_root_with_trailing_slash_or_relative_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lock-symlink-") as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(os.path.join(real, "child"))
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            dangling = os.path.join(tmp, "dangling")
            os.symlink(os.path.join(tmp, "missing"), dangling)

            cases = (
                ("abs-trailing-slash-gc", ("gc", "--state-root", link + "/", "--dry-run"), None, link),
                ("abs-trailing-slash-rollback", ("rollback", "--state-root", link + "/", "r1"), None, link),
                ("abs-double-slash-gc", ("gc", "--state-root", link + "//", "--dry-run"), None, link),
                ("dangling-trailing-slash-gc", ("gc", "--state-root", dangling + "/", "--dry-run"), None, dangling),
                ("rel-trailing-slash-gc", ("gc", "--state-root", "link/", "--dry-run"), tmp, link),
                ("rel-child-gc", ("gc", "--state-root", "link/child", "--dry-run"), tmp, link),
                ("rel-child-rollback", ("rollback", "--state-root", "link/child", "r1"), tmp, link),
            )
            for label, args, cwd, offending in cases:
                with self.subTest(case=label):
                    self._assert_run(
                        args,
                        expected_rc=2,
                        expected_stderr=f"eb-staged-rebuild: state root is unsafe: {offending} is a symlink\n",
                        cwd=cwd,
                    )

    def test_held_lock_error_joins_trailing_slash_state_root_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lock-busy-slash-") as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            with hold_state_root_lock(state):
                cases = (
                    ("gc-abs-slash", ("gc", "--state-root", state + "/", "--lock-timeout", "0"), None, f"{state}/.lock"),
                    (
                        "rollback-abs-slash",
                        ("rollback", "--state-root", state + "/", "--lock-timeout", "0", "r1"),
                        None,
                        f"{state}/.lock",
                    ),
                    ("gc-rel-slash", ("gc", "--state-root", "state/", "--lock-timeout", "0"), tmp, "state/.lock"),
                    (
                        "rollback-rel-slash",
                        ("rollback", "--state-root", "state/", "--lock-timeout", "0", "r1"),
                        tmp,
                        "state/.lock",
                    ),
                )
                for label, args, cwd, lock_path in cases:
                    with self.subTest(case=label):
                        self._assert_run(
                            args,
                            expected_rc=2,
                            expected_stderr=f"eb-staged-rebuild: another eb-staged-rebuild holds the lock at {lock_path}\n",
                            cwd=cwd,
                        )

    def test_non_private_gate_with_trailing_slash_state_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lock-fifo-slash-") as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            os.mkfifo(os.path.join(state, ".lock"))
            self._assert_run(
                ("gc", "--state-root", state + "/", "--dry-run"),
                expected_rc=2,
                expected_stderr=f"eb-staged-rebuild: lock file must be a private regular file: {state}/.lock\n",
            )

    def test_normalized_dotdot_canceling_symlink_is_accepted_by_both(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lock-dotdot-") as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            self._assert_run(
                ("gc", "--state-root", os.path.join(link, "..", "real"), "--dry-run"),
                expected_rc=0,
                expected_stderr="",
            )


if __name__ == "__main__":
    unittest.main()
