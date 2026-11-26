from __future__ import annotations

import os
import signal
import tempfile
import unittest
from contextlib import contextmanager

from tests.support import golden, run_cli, scrub


@contextmanager
def _deadline(seconds: float):
    def _raise_timeout(_signum, _frame):
        raise TimeoutError("ledger-config invocation timed out")

    old = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


class CliRunLedgerConfigHostilePathsParityTests(unittest.TestCase):
    def test_dangling_config_escapes_nbsp_and_soft_hyphen(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            config = os.path.join(root, "ledger-\u00a0-\u00ad.json")
            os.makedirs(software, exist_ok=True)
            os.makedirs(modules, exist_ok=True)

            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--dry-run",
                "--ledger-config",
                config,
                "--",
                "demo.eb",
            )
            zig = run_cli(*args)
            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
            self.assertEqual(zig.stdout, "")
            self.assertIn(r"\xa0", zig.stderr)
            self.assertIn(r"\xad", zig.stderr)
            golden(self, "stderr", scrub(zig.stderr, ROOT=root))

    def test_run_dry_refuses_hostile_ledger_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state_root = os.path.join(root, "state")
            os.makedirs(software, exist_ok=True)
            os.makedirs(modules, exist_ok=True)
            os.makedirs(state_root, exist_ok=True)

            directory_path = os.path.join(root, "ledger-dir")
            os.makedirs(directory_path, exist_ok=True)
            fifo_path = os.path.join(root, "ledger-fifo")
            os.mkfifo(fifo_path)
            symlink_dir_path = os.path.join(root, "ledger-dir-link")
            os.symlink(directory_path, symlink_dir_path)
            symlink_loop_path = os.path.join(root, "ledger-loop")
            os.symlink(symlink_loop_path, symlink_loop_path)
            dangling_path = os.path.join(root, "ledger-dangling.json")
            os.symlink(os.path.join(root, "absent-ledger.json"), dangling_path)

            for label, path, needle in (
                ("directory", directory_path, "Invalid argument"),
                ("fifo", fifo_path, "Invalid argument"),
                ("symlink-directory", symlink_dir_path, "Invalid argument"),
                (
                    "symlink-loop",
                    symlink_loop_path,
                    "Too many levels of symbolic links",
                ),
                ("dangling", dangling_path, "No such file or directory"),
            ):
                with self.subTest(path_kind=label):
                    args = (
                        "run",
                        "--software",
                        software,
                        "--modules",
                        modules,
                        "--state-root",
                        state_root,
                        "--backend",
                        "installpath",
                        "--dry-run",
                        "--ledger-config",
                        path,
                        "--module",
                        "demo/1",
                        "--",
                        "demo-1.eb",
                    )

                    with _deadline(5):
                        zig = run_cli(*args)
                    self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                    self.assertEqual(zig.stdout, "")
                    self.assertIn("cannot read ledger config", zig.stderr)
                    self.assertIn(needle, zig.stderr)
                    golden(self, f"stderr.{label}", scrub(zig.stderr, ROOT=root))

    def test_run_dry_missing_ledger_config_refuses_before_installpath_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            os.makedirs(software, exist_ok=True)
            os.makedirs(modules, exist_ok=True)
            missing = os.path.join(root, "missing-ledger.json")
            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--dry-run",
                "--ledger-config",
                missing,
                "--",
                "demo.eb",
            )
            env = {"PATH": ""}
            zig = run_cli(*args, env=env)
            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
            self.assertEqual(zig.stdout, "")
            self.assertEqual(
                zig.stderr,
                f"eb-staged-rebuild: cannot read ledger config {missing}: [Errno 2] No such file or directory: '{missing}'\n",
            )


if __name__ == "__main__":
    unittest.main()
