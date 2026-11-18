"""Metrics output from garbage collection."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import eb_staged_rebuild.gc as gc_module
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.metrics import render_metrics, write_metrics


class MetricsTests(unittest.TestCase):
    def test_metrics_report_only_known_values(self):
        with tempfile.TemporaryDirectory() as root:
            generations = os.path.join(root, "generations")
            os.makedirs(os.path.join(generations, "current"))
            os.makedirs(os.path.join(generations, "failed"))
            os.makedirs(os.path.join(generations, "building"))
            os.symlink("current", os.path.join(generations, "alias"))
            live = os.path.join(root, "software", "demo", "1")
            os.makedirs(live)
            with open(os.path.join(live, ".eb-staged-rebuild-complete"), "w", encoding="utf-8") as handle:
                handle.write('{"run_id": "current"}\n')
            module = os.path.join(root, "modules", "all", "demo", "1.lua")
            os.makedirs(os.path.dirname(module))
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(f'local root = "{live}"\n')
            metrics = render_metrics(
                root,
                [
                    {
                        "run_id": "current",
                        "state": "complete",
                        "created_at": 700.0,
                        "modules_plan": [
                            {"status": "published", "live_software": live, "live_module": module}
                        ],
                    },
                    {"run_id": "failed", "state": "failed", "created_at": 900.0, "modules_plan": []},
                    {"run_id": "building", "state": "building", "created_at": 950.0, "modules_plan": []},
                ],
                1_000.0,
            )
        self.assertIn("eb_staged_rebuild_generations_present 3\n", metrics)
        self.assertIn("eb_staged_rebuild_current_generation_age_seconds 300\n", metrics)
        self.assertIn("eb_staged_rebuild_last_gc_timestamp_seconds 1000\n", metrics)
        self.assertNotIn("bytes", metrics)

    def test_retired_publication_with_stamped_tree_is_not_current(self):
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, "software", "demo", "1")
            os.makedirs(live)
            with open(os.path.join(live, ".eb-staged-rebuild-complete"), "w", encoding="utf-8") as handle:
                handle.write('{"run_id": "retired"}\n')
            missing_module = os.path.join(root, "modules", "all", "demo", "1.lua")
            metrics = render_metrics(
                root,
                [
                    {
                        "run_id": "retired",
                        "state": "complete",
                        "created_at": 700.0,
                        "modules_plan": [
                            {
                                "status": "published",
                                "live_software": live,
                                "live_module": missing_module,
                            }
                        ],
                    }
                ],
                1_000.0,
            )
        samples = [line for line in metrics.splitlines() if line and not line.startswith("#")]
        self.assertFalse(
            any(line.startswith("eb_staged_rebuild_current_generation_age_seconds ") for line in samples)
        )

    def test_metrics_are_an_atomic_prometheus_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "textfiles", "eb-staged-rebuild.prom")
            write_metrics(path, root, [], 1_000.0)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(path), ".eb-staged-rebuild.prom.new")))
            with open(path, encoding="utf-8") as handle:
                metrics = handle.read()
            samples = [line for line in metrics.splitlines() if line and not line.startswith("#")]
            self.assertFalse(any(line.startswith("eb_staged_rebuild_current_generation_age_seconds ") for line in samples))
            promtool = shutil.which("promtool")
            if promtool:
                checked = subprocess.run(
                    [promtool, "check", "metrics"],
                    input=metrics,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_gc_timestamp_is_taken_after_locked_work(self):
        events = []

        def completed():
            events.append("clock")
            return 2_000.0

        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(gc_module.FileLock, "acquire", side_effect=lambda _timeout: events.append("acquire")),
                mock.patch.object(gc_module.FileLock, "release", side_effect=lambda: events.append("release")),
                mock.patch.object(gc_module, "write_metrics", side_effect=lambda *_args: events.append("write")) as write,
            ):
                collect(root, 0, 1_000.0, metrics_file=os.path.join(root, "metrics.prom"), completion_clock=completed)
        self.assertEqual(events, ["acquire", "clock", "write", "release"])
        self.assertEqual(write.call_args.args[-1], 2_000.0)

    def test_dry_run_refuses_to_write_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "metrics.prom")
            with self.assertRaisesRegex(Refused, "--metrics-file cannot be used with --dry-run"):
                collect(root, 0, 1_000.0, dry_run=True, metrics_file=path)
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
