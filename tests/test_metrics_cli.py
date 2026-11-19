"""Prometheus output from the Python and Zig command lines."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import run_cli, run_cli_optional_binary

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fixture(root):
    state = os.path.join(root, "state")
    live = os.path.join(root, "software", "demo", "1")
    os.makedirs(live)
    now = time.time()
    with open(os.path.join(live, ".eb-staged-rebuild-complete"), "w", encoding="utf-8") as handle:
        json.dump({"run_id": "current"}, handle)
    live_module = os.path.join(root, "modules", "all", "demo", "1.lua")
    os.makedirs(os.path.dirname(live_module))
    with open(live_module, "w", encoding="utf-8") as handle:
        handle.write(f'local root = "{live}"\n')
    journals = (
        {
            "schema": 2,
            "publication": "directory-swap",
            "run_id": "current",
            "state": "complete",
            "created_at": now - 300,
            "modules_plan": [
                {
                    "status": "published",
                    "live_software": live,
                    "live_module": live_module,
                    "retired": os.path.join(root, "software", "demo", ".1.current"),
                    "published_at": now,
                }
            ],
        },
        {
            "schema": 2,
            "publication": "directory-swap",
            "run_id": "failed",
            "state": "failed",
            "created_at": now - 100,
            "modules_plan": [],
        },
        {
            "schema": 2,
            "publication": "directory-swap",
            "run_id": "building",
            "state": "building",
            "created_at": now - 50,
            "modules_plan": [],
        },
    )
    for journal in journals:
        run_id = journal["run_id"]
        os.makedirs(os.path.join(state, "generations", run_id))
        run = os.path.join(state, "runs", run_id)
        os.makedirs(run)
        with open(os.path.join(run, "journal.json"), "w", encoding="utf-8") as handle:
            json.dump(journal, handle)
    return state


def _samples(text):
    found = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split()
            found[name] = float(value)
    return found


class MetricsCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = os.environ.get("TOOL_BIN")
        cls.test_tool = os.environ.get("TOOL_BIN_TEST_OPS")
        cls.promtool = shutil.which("promtool")
        if not cls.tool or not cls.test_tool:
            raise unittest.SkipTest("TOOL_BIN and TOOL_BIN_TEST_OPS are required")
        if not cls.promtool:
            raise RuntimeError("promtool is required for the metrics command-line suite")

    def _command(self, implementation):
        if implementation == "python":
            return [sys.executable, "-m", "eb_staged_rebuild"]
        return [self.tool]

    def test_python_and_zig_emit_matching_atomic_metrics(self):
        rendered = {}
        for implementation in ("python", "zig"):
            with self.subTest(implementation=implementation), tempfile.TemporaryDirectory() as root:
                state = _fixture(root)
                metrics = os.path.join(root, "textfiles", "metrics.prom")
                os.makedirs(os.path.dirname(metrics))
                with open(metrics, "w", encoding="utf-8") as handle:
                    handle.write("old\n")
                os.chmod(metrics, 0o640)
                before = time.time()
                proc = subprocess.run(
                    self._command(implementation) + ["gc", "--state-root", state, "--metrics-file", metrics],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                after = time.time()
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(stat.S_IMODE(os.stat(metrics).st_mode), 0o640)
                self.assertFalse(os.path.exists(os.path.join(os.path.dirname(metrics), ".metrics.prom.new")))
                with open(metrics, encoding="utf-8") as handle:
                    text = handle.read()
                checked = subprocess.run(
                    [self.promtool, "check", "metrics"],
                    input=text,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                samples = _samples(text)
                self.assertEqual(samples["eb_staged_rebuild_generations_present"], 3)
                self.assertGreater(samples["eb_staged_rebuild_current_generation_age_seconds"], 250)
                self.assertLess(samples["eb_staged_rebuild_current_generation_age_seconds"], 400)
                self.assertGreaterEqual(samples["eb_staged_rebuild_last_gc_timestamp_seconds"], before)
                self.assertLessEqual(samples["eb_staged_rebuild_last_gc_timestamp_seconds"], after)
                os.unlink(os.path.join(root, "modules", "all", "demo", "1.lua"))
                retired = subprocess.run(
                    self._command(implementation)
                    + ["gc", "--state-root", state, "--metrics-file", metrics],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(retired.returncode, 0, retired.stderr)
                with open(metrics, encoding="utf-8") as handle:
                    retired_text = handle.read()
                checked = subprocess.run(
                    [self.promtool, "check", "metrics"],
                    input=retired_text,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                self.assertNotIn(
                    "eb_staged_rebuild_current_generation_age_seconds",
                    _samples(retired_text),
                )
                rendered[implementation] = {line for line in text.splitlines() if line.startswith("#")}
        self.assertEqual(rendered["python"], rendered["zig"])

    def test_atomic_failures_keep_the_previous_file(self):
        for implementation in ("python", "zig"):
            for step in ("metrics.write", "metrics.fsync", "metrics.rename"):
                with self.subTest(implementation=implementation, step=step), tempfile.TemporaryDirectory() as root:
                    state = _fixture(root)
                    metrics = os.path.join(root, "metrics.prom")
                    with open(metrics, "w", encoding="utf-8") as handle:
                        handle.write("previous\n")
                    if implementation == "python":
                        command = [sys.executable, os.path.join(ROOT, "tests", "fail_cli.py")]
                    else:
                        command = [self.test_tool]
                    env = dict(os.environ, EB_STAGED_REBUILD_FAIL_AT=step)
                    proc = subprocess.run(
                        command + ["gc", "--state-root", state, "--metrics-file", metrics],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    with open(metrics, encoding="utf-8") as handle:
                        self.assertEqual(handle.read(), "previous\n")
                    self.assertFalse(os.path.exists(os.path.join(root, ".metrics.prom.new")))

    def test_symlink_destination_and_dry_run_are_refused(self):
        for implementation in ("python", "zig"):
            with self.subTest(implementation=implementation), tempfile.TemporaryDirectory() as root:
                state = _fixture(root)
                target = os.path.join(root, "target")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("target\n")
                metrics = os.path.join(root, "metrics.prom")
                os.symlink(target, metrics)
                command = self._command(implementation)
                refused = subprocess.run(
                    command + ["gc", "--state-root", state, "--metrics-file", metrics],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(refused.returncode, 2)
                with open(target, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "target\n")
                dry_path = os.path.join(root, "dry.prom")
                dry = subprocess.run(
                    command + ["gc", "--state-root", state, "--dry-run", "--metrics-file", dry_path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(dry.returncode, 2)
                self.assertFalse(os.path.exists(dry_path))

    def test_directory_and_fifo_destinations_are_refused(self):
        for shape in ("directory", "fifo"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as root:
                state = _fixture(root)
                metrics = os.path.join(root, "metrics.target")
                if shape == "directory":
                    os.makedirs(metrics)
                else:
                    os.mkfifo(metrics)

                py = run_cli("gc", "--state-root", state, "--metrics-file", metrics)
                self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
                self.assertEqual(py.stdout, "")
                self.assertIn("metadata source is not a regular file", py.stderr)
                self.assertIn(metrics, py.stderr)

                zig = run_cli_optional_binary("gc", "--state-root", state, "--metrics-file", metrics)
                if zig is not None:
                    self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                    self.assertEqual(zig.stdout, "")
                    self.assertEqual(zig.stderr, py.stderr)


if __name__ == "__main__":
    unittest.main()
