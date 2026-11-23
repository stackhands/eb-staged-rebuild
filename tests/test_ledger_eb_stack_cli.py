"""tests/test_ledger_eb_stack.py from the command line, for both implementations.

The same append, recovery and idempotence checks against a real eb-stack,
driven through ``run``, ``resume`` and ``rollback``: the run is killed
with SIGKILL after its first append returned and before the receipt was
saved (EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*"), then resumed twice
and rolled back twice. A run that ``run --verify`` refuses is then
published with ``resume --force``, and its records carry ``build.verify``.
Runs the Python tool and the program TOOL_BIN names.
Skipped unless EB_STACK_BIN is set.

    EB_STACK_BIN=.../eb-stack TOOL_BIN=zig-out/bin/eb-staged-rebuild \\
        python3 -m unittest -v tests.test_ledger_eb_stack_cli
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from eb_staged_rebuild.journal import load_journal
from tests import test_ledger_eb_stack as base
from tests.support import classic_install, implementations

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")
VERIFY_ENV = ("LMOD_CMD", "MODULEPATH", "LOADEDMODULES", "_LMFILES_", "EESSI_CVMFS_REPO", "EESSI_EPREFIX")


class CliEbStackLedgerTests(base.RealEbStackLedgerTests):
    def cli_fixture(self, root):
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        install, module, _text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
        private, public = base._keys(root)
        log = os.path.join(root, "ledger", "stack.jsonl")
        head = os.path.join(root, "out", "head.json")
        os.makedirs(os.path.dirname(log))
        os.makedirs(os.path.dirname(head))
        config = {
            "eb_stack": os.path.abspath(base.BINARY), "log": log, "key": private, "head": head,
            "public_key": public, "actor": "swap-test", "stack": "2026.06", "target": "x86_64/generic",
            "action": "rebuild-inplace", "required": True, "timeout": 30,
        }
        config_path = os.path.join(root, "ledger.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
        os.chmod(eb, 0o755)
        args = [
            "run", "--software", software, "--modules", modules, "--state-root", state,
            "--backend", "installpath", "--eb", eb, "--run-id", "run1", "--skip-live-check",
            "--ledger-config", config_path, "--module", "demo/1", "--", "demo-1.eb",
        ]
        return args, state, install, log, head

    def tool(self, implementation, root, env, *words, rc=0, clear=()):
        done = implementation.run(*words, env=env, cwd=root, clear=clear)
        self.assertEqual(done.returncode, rc, done.stdout + done.stderr)

    def payload(self, install):
        with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def test_append_recovery_and_idempotence(self):
        for implementation in implementations():
            with self.subTest(implementation=implementation.name), tempfile.TemporaryDirectory() as root:
                args, state, install, log, head = self.cli_fixture(root)
                env = {"PYTHONPATH": REPO, "FAKE_EB_PAYLOAD": "v1\n"}
                ready = os.path.join(root, "ready")
                paused = dict(os.environ, **env, EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*",
                              EB_STAGED_REBUILD_PAUSE_READY=ready, EB_STAGED_REBUILD_PAUSE_SECONDS="60")
                child = subprocess.Popen([*implementation.command, *args], env=paused, cwd=root, start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 60
                while not os.path.exists(ready):
                    self.assertIsNone(child.poll(), "the run exited before the first receipt")
                    self.assertLess(time.monotonic(), deadline, "the run never reached ledger.acked")
                    time.sleep(0.02)
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                self.assertEqual(self.payload(install), "v0\n")
                self.assertEqual([entry["stage"] for entry in self.entries(log)], ["started"])
                self.assertEqual(self.entries(log)[0]["action"], "rebuild-inplace")

                self.tool(implementation, root, env, "resume", "--state-root", state, "run1")
                self.assertEqual(self.payload(install), "v1\n")
                published = self.assert_protocol(log, head, install)
                self.assertEqual([entry["stage"] for entry in published], ["started", "finished"])
                journal = load_journal(os.path.join(state, "runs", "run1", "journal.json"))
                self.assertEqual(journal["schema"], 4)
                self.assertEqual(journal["publication"], "directory-swap")

                self.tool(implementation, root, env, "resume", "--state-root", state, "run1")
                self.assertEqual(len(self.entries(log)), 2)

                self.tool(implementation, root, env, "rollback", "--state-root", state, "run1")
                self.assertEqual(self.payload(install), "v0\n")
                rolled = self.assert_protocol(log, head, install)
                self.assertEqual(
                    [entry["stage"] for entry in rolled if entry["action"] == "correction"],
                    ["started", "finished"],
                )
                self.tool(implementation, root, env, "rollback", "--state-root", state, "run1")
                self.assertEqual(len(self.entries(log)), 4)
                self.assert_protocol(log, head, install)

    def test_a_forced_publication_says_so_in_its_records(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        for implementation in implementations():
            with self.subTest(implementation=implementation.name), tempfile.TemporaryDirectory() as root:
                args, state, install, log, head = self.cli_fixture(root)
                args = ["run", "--verify", *args[1:]]
                cleared = [key for key in os.environ if key in VERIFY_ENV or key.startswith("EASYBUILD_")]
                env = {"PYTHONPATH": REPO, "FAKE_EB_PAYLOAD": "v1\n", "FAKE_EB_EXTRA": json.dumps(extra)}

                self.tool(implementation, root, env, *args, rc=2, clear=cleared)
                self.assertEqual(self.payload(install), "v0\n")
                self.assertEqual(self.entries(log) if os.path.exists(log) else [], [])

                self.tool(implementation, root, env, "resume", "--state-root", state, "--force", "run1", clear=cleared)
                self.assertEqual(self.payload(install), "v1\n")
                verify = load_journal(os.path.join(state, "runs", "run1", "journal.json"))["verify"]
                self.assertEqual((verify["result"], verify["forced"], verify["checks"]["elf"]), ("fail", True, 1))
                forced = {"result": "fail", "forced": True, "checks": verify["checks"]}
                published = self.assert_protocol(log, head, install)
                self.assertEqual(
                    [(entry["stage"], entry["build"].get("verify")) for entry in published],
                    [("started", forced), ("finished", forced)],
                )

                self.tool(implementation, root, env, "rollback", "--state-root", state, "run1", clear=cleared)
                corrections = self.assert_protocol(log, head, install)[2:]
                self.assertEqual([entry["action"] for entry in corrections], ["correction", "correction"])
                self.assertEqual([entry["build"].get("verify") for entry in corrections], [None, None])

                del env["FAKE_EB_EXTRA"]
                self.tool(implementation, root, env, *["run2" if word == "run1" else word for word in args], clear=cleared)
                clean = self.assert_protocol(log, head, install)[4:]
                self.assertEqual([(entry["stage"], entry["build"].get("verify")) for entry in clean],
                                 [("started", None), ("finished", None)])


if __name__ == "__main__":
    unittest.main()
