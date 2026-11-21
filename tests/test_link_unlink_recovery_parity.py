"""CLI parity when removing a created module link fails during recovery or rollback."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import run_cli, run_cli_optional_binary

_FAKE_EB = """#!/usr/bin/env python3
import os
import sys

if "--dry-run" in sys.argv:
    print(" * [R] /tmp/demo-1.eb (module: demo/1)")
    sys.exit(0)

software = next(
    arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--installpath-software=")
)
modules = next(
    arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--installpath-modules=")
)
install = os.path.join(software, "demo", "1")
os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
with open(os.path.join(install, "PAYLOAD"), "w", encoding="utf-8") as handle:
    handle.write("new\\n")
with open(os.path.join(install, "easybuild", "demo-1.eb"), "w", encoding="utf-8") as handle:
    handle.write("name = 'demo'\\n")
module = os.path.join(modules, "all", "demo", "1.lua")
os.makedirs(os.path.dirname(module), exist_ok=True)
with open(module, "w", encoding="utf-8") as handle:
    handle.write(f'local root = "{install}"\\n')
for rel in ("alpha/demo/1.lua", "tools/demo/1.lua"):
    link = os.path.join(modules, rel)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(module, link)
"""


def _build_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    software = root / "software"
    modules = root / "modules"
    state_root = root / "state"
    (software / "demo" / "1" / "easybuild").mkdir(parents=True)
    (software / "demo" / "1" / "PAYLOAD").write_text("old\n", encoding="utf-8")
    (software / "demo" / "1" / "easybuild" / "demo-1.eb").write_text(
        "name = 'demo'\n", encoding="utf-8"
    )
    (modules / "all" / "demo").mkdir(parents=True)
    (modules / "all" / "demo" / "1.lua").write_text(
        f'local root = "{software / "demo" / "1"}"\n', encoding="utf-8"
    )
    fake_eb = root / "fake-eb"
    fake_eb.write_text(_FAKE_EB, encoding="utf-8")
    fake_eb.chmod(0o755)
    return software, modules, state_root, fake_eb


def _publish_run(runner, software: Path, modules: Path, state_root: Path, fake_eb: Path):
    return runner(
        "run",
        "--backend",
        "installpath",
        "--software",
        str(software),
        "--modules",
        str(modules),
        "--state-root",
        str(state_root),
        "--eb",
        str(fake_eb),
        "--run-id",
        "r1",
        "--skip-live-check",
        "--",
        "demo-1.eb",
    )


class LinkUnlinkRecoveryParityTests(unittest.TestCase):
    def _assert_rollback_restores_on_unlink_error(self, runner) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission checks are not meaningful as root")
        with tempfile.TemporaryDirectory(prefix="rollback-link-unlink-") as tmp:
            root = Path(tmp)
            software, modules, state_root, fake_eb = _build_fixture(root)
            published = _publish_run(runner, software, modules, state_root, fake_eb)
            if published is None:
                self.skipTest("TOOL_BIN is unset")
            self.assertEqual(published.returncode, 0, published.stdout + published.stderr)

            first_link = modules / "alpha" / "demo" / "1.lua"
            locked_link = modules / "tools" / "demo" / "1.lua"
            self.assertTrue(first_link.is_symlink())
            self.assertTrue(locked_link.is_symlink())

            locked_link.parent.chmod(0o555)
            try:
                result = runner("rollback", "--state-root", str(state_root), "r1")
            finally:
                locked_link.parent.chmod(0o755)

            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(
                result.stderr,
                f"eb-staged-rebuild: [Errno 13] Permission denied: '{locked_link}'\n",
            )
            journal = json.loads(
                (state_root / "runs" / "r1" / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["state"], "complete")
            self.assertEqual(journal["message"], "rollback interrupted; published tree restored")
            self.assertEqual(
                (software / "demo" / "1" / "PAYLOAD").read_text(encoding="utf-8"),
                "new\n",
            )
            self.assertTrue(first_link.is_symlink())
            self.assertTrue(locked_link.is_symlink())
            self.assertTrue(all(link["created"] is True for link in journal["links_plan"]))

    def _assert_resume_refuses_on_unlink_error(self, runner) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission checks are not meaningful as root")
        with tempfile.TemporaryDirectory(prefix="resume-link-unlink-") as tmp:
            root = Path(tmp)
            software, modules, state_root, fake_eb = _build_fixture(root)
            published = _publish_run(runner, software, modules, state_root, fake_eb)
            if published is None:
                self.skipTest("TOOL_BIN is unset")
            self.assertEqual(published.returncode, 0, published.stdout + published.stderr)

            journal_file = state_root / "runs" / "r1" / "journal.json"
            journal = json.loads(journal_file.read_text(encoding="utf-8"))
            journal["state"] = "publishing"
            journal["message"] = ""
            journal.pop("completed_at", None)
            journal_file.write_text(
                json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            locked_link = modules / "tools" / "demo" / "1.lua"
            self.assertTrue(locked_link.is_symlink())
            locked_link.parent.chmod(0o555)
            try:
                result = runner("resume", "--state-root", str(state_root), "r1")
            finally:
                locked_link.parent.chmod(0o755)

            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(
                result.stderr,
                "eb-staged-rebuild: publish failed and the old live tree could not be restored: "
                f"[Errno 13] Permission denied: '{locked_link}'\n",
            )
            after = json.loads(journal_file.read_text(encoding="utf-8"))
            self.assertEqual(after["state"], "publishing")
            by_rel = {link["rel"]: link for link in after["links_plan"]}
            self.assertIs(by_rel["tools/demo/1.lua"]["created"], True)
            self.assertTrue(locked_link.is_symlink())
            self.assertEqual(
                (software / "demo" / "1" / "PAYLOAD").read_text(encoding="utf-8"),
                "old\n",
            )

    def test_rollback_restores_published_tree_when_link_unlink_fails_python(self) -> None:
        self._assert_rollback_restores_on_unlink_error(run_cli)

    def test_rollback_restores_published_tree_when_link_unlink_fails_zig(self) -> None:
        self._assert_rollback_restores_on_unlink_error(run_cli_optional_binary)

    def test_resume_refuses_when_created_link_unlink_fails_python(self) -> None:
        self._assert_resume_refuses_on_unlink_error(run_cli)

    def test_resume_refuses_when_created_link_unlink_fails_zig(self) -> None:
        self._assert_resume_refuses_on_unlink_error(run_cli_optional_binary)


if __name__ == "__main__":
    unittest.main()
