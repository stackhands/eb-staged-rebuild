from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE_MAIN = "9dede81ed18869e2587c9fb93660ed83d4a683bb"


def _run_cli(repo: Path, *args: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    cp = subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return cp.returncode, cp.stdout, cp.stderr


def _with_added_verify(result: tuple[int, str, str]) -> tuple[int, str, str]:
    rc, out, err = result
    old_choices = "{probe,run,resume,rollback,gc,retire,status,resolve}"
    new_choices = "{probe,run,resume,verify,rollback,gc,retire,status,list,resolve}"
    out = (
        out.replace(old_choices, new_choices)
        .replace(
            "    resume              continue a run after a crash\n",
            "    resume              continue a run after a crash\n"
            "    verify              check a built run's stage before it is published\n",
        )
        .replace(
            "    status              show run journals\n",
            "    status              show run journals\n"
            "    list                show each install path's live tree and the generations\n"
            "                        kept beside it\n",
        )
    )
    err = err.replace(old_choices, new_choices)
    return rc, out, err


class CliOutputCompatTests(unittest.TestCase):
    @classmethod
    def _ensure_base_commit_available(cls) -> None:
        have_commit = subprocess.run(
            ["git", "cat-file", "-e", f"{BASE_MAIN}^{{commit}}"],
            cwd=cls.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if have_commit.returncode == 0:
            return

        has_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cls.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if has_origin.returncode != 0:
            raise RuntimeError(
                f"base commit {BASE_MAIN} is missing and remote 'origin' is unavailable; "
                "cannot fetch commit for compatibility test"
            )

        fetch = subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", BASE_MAIN],
            cwd=cls.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if fetch.returncode != 0:
            raise RuntimeError(
                f"base commit {BASE_MAIN} is missing and fetch failed (exit {fetch.returncode}):\n"
                f"{fetch.stdout}{fetch.stderr}"
            )

        have_after_fetch = subprocess.run(
            ["git", "cat-file", "-e", f"{BASE_MAIN}^{{commit}}"],
            cwd=cls.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if have_after_fetch.returncode != 0:
            raise RuntimeError(f"fetched {BASE_MAIN} but commit is still unavailable for worktree add")

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[1]
        cls._ensure_base_commit_available()
        cls._worktree_dir = Path(tempfile.mkdtemp(prefix="esr-main-cli-"))
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(cls._worktree_dir), BASE_MAIN],
            cwd=cls.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(cls._worktree_dir)],
            cwd=cls.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def test_default_help_output_matches_main(self) -> None:
        self.assertEqual(_run_cli(self.repo, "--help"), _with_added_verify(_run_cli(self._worktree_dir, "--help")))

    def test_default_run_output_matches_main(self) -> None:
        self.assertEqual(_run_cli(self.repo), _with_added_verify(_run_cli(self._worktree_dir)))
