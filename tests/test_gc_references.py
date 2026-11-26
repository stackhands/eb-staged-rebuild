"""gc keeps a generation that the usage log reports in use."""

import json
import os
import shutil
import sys
import unittest

from tests.support import site, write


def _keep_logs_stub(root):
    script = os.path.join(root, "eb-keep-logs")
    write(
        script,
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_eb_keep_logs.py")}" "$@"\n',
        0o755,
    )
    return script


class ReferenceTests(unittest.TestCase):
    def published(self):
        demo = site(self, "gc-references-")
        done = demo.run(verify=False, run_id="run1", FAKE_EB_PAYLOAD="v1\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return demo, demo.journal("run1")["modules_plan"][0]["retired"]

    def test_usage_log_keeps_a_generation(self):
        demo, retired = self.published()
        keep_logs = _keep_logs_stub(demo.root)
        usage = os.path.join(demo.root, "usage.log")
        write(usage, "{}\n")
        generation = os.path.join(demo.state, "generations", "run1")
        self.assertTrue(os.path.isdir(generation))
        shutil.rmtree(retired)
        done = demo.cli(
            "gc",
            "--state-root",
            demo.state,
            "--grace-hours",
            "0",
            "--usage-log",
            usage,
            "--eb-keep-logs",
            keep_logs,
            "--json",
            FAKE_KEEP_LOGS_MODE="in-use",
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        matched = [action for action in json.loads(done.stdout)["actions"] if action["path"] == generation]
        self.assertEqual([action["action"] for action in matched], ["keep"], done.stdout)
        self.assertTrue(os.path.isdir(generation))


if __name__ == "__main__":
    unittest.main()
