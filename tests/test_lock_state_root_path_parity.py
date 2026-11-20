"""CLI parity for state-root lock path normalization and error messages."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import hold_state_root_lock, run_cli, run_cli_optional_binary


class LockStateRootPathParityTests(unittest.TestCase):
    def _assert_both(
        self,
        args: tuple[str, ...],
        *,
        expected_rc: int,
        expected_stderr: str,
        cwd: str | None = None,
    ) -> None:
        py_done = run_cli(*args, cwd=cwd)
        self.assertEqual(py_done.returncode, expected_rc, py_done.stdout + py_done.stderr)
        self.assertEqual(py_done.stdout, "")
        self.assertEqual(py_done.stderr, expected_stderr)
        zig_done = run_cli_optional_binary(*args, cwd=cwd)
        if zig_done is not None:
            self.assertEqual(zig_done.returncode, expected_rc, zig_done.stdout + zig_done.stderr)
            self.assertEqual(zig_done.stdout, py_done.stdout)
            self.assertEqual(zig_done.stderr, py_done.stderr)

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
                    self._assert_both(
                        args,
                        expected_rc=2,
                        expected_stderr=f"eb-staged-rebuild: state root is unsafe: {offending} is a symlink\n",
                        cwd=cwd,
                    )

    def test_held_lock_error_joins_trailing_slash_state_root_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lock-busy-slash-") as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            with hold_state_root_lock(state, timeout=0):
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
                        self._assert_both(
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
            self._assert_both(
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
            self._assert_both(
                ("gc", "--state-root", os.path.join(link, "..", "real"), "--dry-run"),
                expected_rc=0,
                expected_stderr="",
            )

    def test_binary_parity_runs_when_tool_bin_is_set(self) -> None:
        if run_cli_optional_binary("--help") is None:
            self.skipTest("TOOL_BIN is unset")
        with tempfile.TemporaryDirectory(prefix="lock-binary-check-") as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            py_done = run_cli("gc", "--state-root", link + "/", "--dry-run")
            zig_done = run_cli_optional_binary("gc", "--state-root", link + "/", "--dry-run")
            assert zig_done is not None
            self.assertEqual(py_done.returncode, 2)
            self.assertEqual(zig_done.returncode, 2)
            self.assertEqual(py_done.stdout, zig_done.stdout)
            self.assertEqual(py_done.stderr, zig_done.stderr)


if __name__ == "__main__":
    unittest.main()
