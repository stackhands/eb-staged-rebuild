import json
import os
import tempfile
import unittest

from tests.support import run_cli


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ProbeTests(unittest.TestCase):
    def test_probe_no_create_refuses_a_missing_path(self):
        with tempfile.TemporaryDirectory() as root:
            missing = os.path.join(root, "missing")
            probe = run_cli(
                "probe", "--path", missing, "--no-create", "--eb", "missing-eb"
            )
            self.assertEqual(probe.returncode, 1, probe.stdout + probe.stderr)
            self.assertIn(f"probe path does not exist: {missing}", probe.stderr)
            self.assertFalse(os.path.exists(missing))

    def test_unreadable_resolve_exits_2_without_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode 000 file")
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "mod.lua")
            _write(path, 'local root = "/opt/software/demo/1"\n')
            os.chmod(path, 0)
            try:
                done = run_cli("resolve", path)
            finally:
                os.chmod(path, 0o644)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("eb-staged-rebuild:", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_probe_require_eb_fails_when_eb_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            probe = run_cli(
                "probe", "--path", root, "--require-eb", "--eb", "missing-eb"
            )
            self.assertEqual(probe.returncode, 1, probe.stdout + probe.stderr)
            self.assertIn("EasyBuild:", probe.stdout)
            self.assertIn("probe requires EasyBuild, but it was not found: missing-eb", probe.stderr)


class CliTests(unittest.TestCase):
    def test_dry_run_and_probe_and_resolve(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            os.makedirs(os.path.join(software, "demo", "1", "easybuild"))
            _write(os.path.join(software, "demo", "1", "PAYLOAD"), "v0\n")
            _write(
                os.path.join(software, "demo", "1", "easybuild", "demo-1.eb"),
                "name = 'demo'\n",
            )
            module = os.path.join(modules, "all", "demo")
            os.makedirs(module)
            module_file = os.path.join(module, "1.lua")
            install = os.path.join(software, "demo", "1")
            _write(
                module_file,
                f'local root = "{install}"\nsetenv("EBROOTDEMO", root)\n',
            )
            completed = run_cli(
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                os.path.join(root, "state"),
                "--dry-run",
                "--module",
                "demo/1",
                "--",
                "demo-1.eb",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("dry-run", completed.stdout)
            self.assertFalse(os.path.isdir(os.path.join(root, "state", "runs")))
            self.assertIn("v0\n", _read(os.path.join(install, "PAYLOAD")))

            probe = run_cli("probe", "--path", root, "--json", "--eb", "missing-eb")
            self.assertEqual(probe.returncode, 0, probe.stderr)
            report = json.loads(probe.stdout)
            self.assertIn("rename_exchange", report)
            self.assertEqual(report["flock"]["cross_node"], "unmeasured")

            resolved = run_cli("resolve", module_file)
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            body = json.loads(resolved.stdout)
            self.assertEqual(body["root"], install)
            self.assertTrue(body["complete"])


class StatusJsonTests(unittest.TestCase):
    def test_status_json_is_one_document(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            runs = os.path.join(state, "runs")
            os.makedirs(os.path.join(runs, "r1"))
            os.makedirs(os.path.join(runs, "r2"))
            _write(
                os.path.join(runs, "r1", "journal.json"),
                json.dumps(
                    {
                        "schema": 2,
                        "run_id": "r1",
                        "state": "done",
                        "backend": "bwrap",
                        "message": "all published",
                        "modules_plan": [
                            {
                                "rel": "demo/1",
                                "status": "published",
                                "live_software": "/sw/demo/1",
                                "retired": "/sw/demo/.1.old",
                            }
                        ],
                    }
                ),
            )
            _write(
                os.path.join(runs, "r2", "journal.json"),
                json.dumps(
                    {
                        "schema": 2,
                        "kind": "retire",
                        "run_id": "r2",
                        "state": "retired",
                        "module_path": "/m/x/1.lua",
                        "software_dir": "/sw/x/1",
                    }
                ),
            )
            completed = run_cli("status", "--state-root", state, "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertEqual(
            document,
            {
                "runs": [
                    {
                        "backend": "bwrap",
                        "kind": "rebuild",
                        "message": "all published",
                        "modules": [
                            {
                                "live_software": "/sw/demo/1",
                                "rel": "demo/1",
                                "retired": "/sw/demo/.1.old",
                                "status": "published",
                            }
                        ],
                        "modules_published": 1,
                        "modules_total": 1,
                        "run_id": "r1",
                        "state": "done",
                    },
                    {
                        "kind": "retire",
                        "module_path": "/m/x/1.lua",
                        "run_id": "r2",
                        "software_dir": "/sw/x/1",
                        "state": "retired",
                    },
                ]
            },
        )
        self.assertNotIn("no runs", completed.stdout)

    def test_status_json_of_an_empty_root_is_an_empty_document(self):
        with tempfile.TemporaryDirectory() as root:
            completed = run_cli("status", "--state-root", root, "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"runs": []})


if __name__ == "__main__":
    unittest.main()
