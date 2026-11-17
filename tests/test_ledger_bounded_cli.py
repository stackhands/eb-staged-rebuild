"""test_ledger_bounded's two receipt tests through ``run --ledger-config``.

The output limit is the same 64 KiB in both implementations. A wrapper around
``fake_eb_stack.py`` pads each append's stdout to an exact size and floods
stderr. Runs the Python tool and, when TOOL_BIN is set, the program it names.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_ledger_bounded_cli
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from eb_staged_rebuild.ledger_adapter import _OUTPUT_LIMIT
from eb_staged_rebuild.outbox import PENDING_EXIT
from tests.support import classic_install

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(REPO, "tests", "fake_eb_stack.py")
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")


def implementations():
    found = [("python", [sys.executable, "-m", "eb_staged_rebuild"])]
    if os.environ.get("TOOL_BIN"):
        found.append(("binary", [os.environ["TOOL_BIN"]]))
    return found


class Case:
    """A rebuild of demo/1 whose required ledger is the padded fake eb-stack."""

    def __init__(self, root, stdout_bytes, receipt_first=True):
        """``receipt_first`` False puts the padding before the receipt, as a
        helper that chatters before it would. Each append's stdout size goes
        to ``sizes``."""
        self.root = root
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        self.state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        self.install, _module, _text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
        self.sizes_path = os.path.join(root, "sizes")
        self.plan = os.path.join(root, "plan")
        self.log = os.path.join(root, "stack.jsonl")
        wrapper = os.path.join(root, "eb-stack")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(f"""\
                #!{sys.executable}
                import os, subprocess, sys
                done = subprocess.run([sys.executable, {FAKE!r}, *sys.argv[1:]], capture_output=True)
                if sys.argv[1:3] == ["ledger", "append"]:
                    pad = {stdout_bytes} - len(done.stdout)
                    padding = b"p" * (pad - 1) + b"\\n" if pad > 0 else b""
                    body = done.stdout + padding if {receipt_first!r} else padding + done.stdout
                    with open({self.sizes_path!r}, "a") as handle:
                        handle.write(str(len(body)) + "\\n")
                else:
                    body = done.stdout
                for start in range(0, len(body), 4096):
                    os.write(1, body[start:start + 4096])
                chunk = b"w" * 65536
                for _ in range(128):
                    os.write(2, chunk)
                sys.exit(done.returncode)
            """))
        os.chmod(wrapper, 0o755)
        config = {
            "eb_stack": wrapper, "log": self.log, "key": os.path.join(root, "unused.pem"),
            "head": os.path.join(root, "head.json"), "public_key": os.path.join(root, "unused.pub"),
            "actor": "bounded-test", "stack": "2026.06", "target": "x86_64/generic",
            "action": "rebuild-inplace", "required": True, "timeout": 30,
        }
        config_path = os.path.join(root, "ledger.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
        os.chmod(eb, 0o755)
        self.run_args = [
            "run", "--software", software, "--modules", modules, "--state-root", self.state,
            "--backend", "installpath", "--eb", eb, "--run-id", "run1", "--skip-live-check",
            "--ledger-config", config_path, "--module", "demo/1", "--", "demo-1.eb",
        ]

    def plan_modes(self, *modes):
        with open(self.plan, "w", encoding="utf-8") as handle:
            handle.write("".join(mode + "\n" for mode in modes))

    def tool(self, command, args):
        env = dict(os.environ, PYTHONPATH=REPO, FAKE_EB_PAYLOAD="v1\n", FAKE_LEDGER_PLAN=self.plan)
        return subprocess.run(command + args, env=env, cwd=self.root, capture_output=True, text=True, timeout=300)

    def payload(self):
        with open(os.path.join(self.install, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def stages(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line)["stage"] for line in handle]

    def sizes(self):
        with open(self.sizes_path, encoding="utf-8") as handle:
            return [int(line) for line in handle.read().split()]


class ReceiptLimitTests(unittest.TestCase):
    def test_receipt_that_fills_the_output_limit_is_accepted(self):
        for name, command in implementations():
            with self.subTest(implementation=name), tempfile.TemporaryDirectory() as root:
                case = Case(root, _OUTPUT_LIMIT)
                case.plan_modes("fail-after-append")
                first = case.tool(command, case.run_args)
                self.assertEqual(first.returncode, PENDING_EXIT, first.stdout + first.stderr[-2000:])
                self.assertEqual(case.stages(), ["started"])
                self.assertEqual(case.payload(), "v0\n")
                again = case.tool(command, ["resume", "--state-root", case.state, "run1"])
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr[-2000:])
                self.assertEqual(case.payload(), "v1\n")
                self.assertEqual(case.stages(), ["started", "finished"])
                self.assertEqual(case.sizes(), [_OUTPUT_LIMIT] * 3)

    def test_receipt_beyond_the_output_limit_is_not_read(self):
        for name, command in implementations():
            with self.subTest(implementation=name), tempfile.TemporaryDirectory() as root:
                case = Case(root, 2 * _OUTPUT_LIMIT, receipt_first=False)
                result = case.tool(command, case.run_args)
                self.assertEqual(result.returncode, PENDING_EXIT, result.stdout + result.stderr[-2000:])
                self.assertIn("printed no receipt", result.stdout + result.stderr)
                self.assertEqual(case.payload(), "v0\n")
                self.assertEqual(case.sizes(), [2 * _OUTPUT_LIMIT])


if __name__ == "__main__":
    unittest.main()
