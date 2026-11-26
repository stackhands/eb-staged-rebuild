"""The ledger append, recovery and idempotence checks against a real eb-stack.

The checks run through ``run``, ``resume`` and ``rollback`` of the program
TOOL_BIN names: the run is killed with SIGKILL after its first append returned
and before the receipt was saved (EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*"),
then resumed twice and rolled back twice. A run that ``run --verify`` refuses is
then published with ``resume --force``, and its records carry ``build.verify``.
Set ``EB_STACK_BIN`` to the executable built from eb-stack 0.6.2 (tag
``v0.6.2``, 416a68f). Skipped unless EB_STACK_BIN is set. The filesystem under
test is whatever the temporary directory uses; this is not a General Parallel
File System acceptance test.

    EB_STACK_BIN=.../eb-stack TOOL_BIN=zig-out/bin/eb-staged-rebuild \\
        python3 -m unittest -v tests.test_ledger_eb_stack_cli
"""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest

from tests.support import classic_install, pause_fifo, run_cli, tool_bin, wait_for_pause

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")
VERIFY_ENV = ("LMOD_CMD", "MODULEPATH", "LOADEDMODULES", "_LMFILES_", "EESSI_CVMFS_REPO", "EESSI_EPREFIX")
BINARY = os.environ.get("EB_STACK_BIN")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _keys(directory):
    private = os.path.join(directory, "ledger-key.pem")
    public = os.path.join(directory, "ledger-key.pub")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", private], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", private, "-pubout", "-out", public], check=True, capture_output=True)
    return private, public


class CliEbStackLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY:
            raise unittest.SkipTest("set EB_STACK_BIN to run the real ledger protocol")
        completed = subprocess.run([BINARY, "--version"], check=True, capture_output=True, text=True)
        cls.version = completed.stdout.strip() or completed.stderr.strip()
        print(f"eb-stack binary {_sha256(BINARY)} version {cls.version}", flush=True)

    def entries(self, log):
        with open(log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def assert_protocol(self, log, head, install):
        with open(log, encoding="utf-8") as handle:
            raw_lines = [line for line in handle.read().splitlines() if line]
        for line in raw_lines:
            canonical = json.dumps(json.loads(line), separators=(",", ":"), sort_keys=True)
            self.assertEqual(canonical, line)
        parsed = [json.loads(line) for line in raw_lines]
        ids = [entry["build"]["event_id"] for entry in parsed]
        self.assertEqual(len(ids), len(set(ids)))
        with open(head, encoding="utf-8") as handle:
            signed = json.load(handle)
        self.assertEqual(signed["domain"], "eb-stack-ledger-v1")
        self.assertEqual(signed["size"], len(parsed))
        self.assertEqual(len(signed["root"]), 64)
        self.assertEqual(len(signed["signature"]), 128)
        for entry in parsed:
            self.assertEqual(entry["location"], install)
            self.assertNotIn("generations", entry["location"])
            self.assertEqual(entry["module"], "demo/1")
        return parsed

    def cli_fixture(self, root):
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        install, module, _text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
        private, public = _keys(root)
        log = os.path.join(root, "ledger", "stack.jsonl")
        head = os.path.join(root, "out", "head.json")
        os.makedirs(os.path.dirname(log))
        os.makedirs(os.path.dirname(head))
        config = {
            "eb_stack": os.path.abspath(BINARY), "log": log, "key": private, "head": head,
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

    def tool(self, root, env, *words, rc=0, clear=()):
        done = run_cli(*words, env=env, cwd=root, clear=clear)
        self.assertEqual(done.returncode, rc, done.stdout + done.stderr)

    def journal(self, state):
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def payload(self, install):
        with open(os.path.join(install, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()

    def test_append_recovery_and_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            args, state, install, log, head = self.cli_fixture(root)
            env = {"FAKE_EB_PAYLOAD": "v1\n"}
            ready, fifo = pause_fifo(root)
            paused = dict(os.environ, **env, EB_STAGED_REBUILD_PAUSE_AT="ledger.acked:*",
                          EB_STAGED_REBUILD_PAUSE_READY=ready, EB_STAGED_REBUILD_PAUSE_SECONDS="60")
            with subprocess.Popen([tool_bin(), *args], env=paused, cwd=root, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
                reached = wait_for_pause(child, fifo)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            self.assertTrue(reached, "the run exited before, or did not reach, ledger.acked within 60 seconds")
            self.assertEqual(self.payload(install), "v0\n")
            self.assertEqual([entry["stage"] for entry in self.entries(log)], ["started"])
            self.assertEqual(self.entries(log)[0]["action"], "rebuild-inplace")

            self.tool(root, env, "resume", "--state-root", state, "run1")
            self.assertEqual(self.payload(install), "v1\n")
            published = self.assert_protocol(log, head, install)
            self.assertEqual([entry["stage"] for entry in published], ["started", "finished"])
            journal = self.journal(state)
            self.assertEqual(journal["schema"], 4)
            self.assertEqual(journal["publication"], "directory-swap")

            self.tool(root, env, "resume", "--state-root", state, "run1")
            self.assertEqual(len(self.entries(log)), 2)

            self.tool(root, env, "rollback", "--state-root", state, "run1")
            self.assertEqual(self.payload(install), "v0\n")
            rolled = self.assert_protocol(log, head, install)
            self.assertEqual(
                [entry["stage"] for entry in rolled if entry["action"] == "correction"],
                ["started", "finished"],
            )
            self.tool(root, env, "rollback", "--state-root", state, "run1")
            self.assertEqual(len(self.entries(log)), 4)
            self.assert_protocol(log, head, install)

    def test_a_forced_publication_says_so_in_its_records(self):
        extra = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
        with tempfile.TemporaryDirectory() as root:
            args, state, install, log, head = self.cli_fixture(root)
            args = ["run", "--verify", *args[1:]]
            cleared = [key for key in os.environ if key in VERIFY_ENV or key.startswith("EASYBUILD_")]
            env = {"FAKE_EB_PAYLOAD": "v1\n", "FAKE_EB_EXTRA": json.dumps(extra)}

            self.tool(root, env, *args, rc=2, clear=cleared)
            self.assertEqual(self.payload(install), "v0\n")
            self.assertEqual(self.entries(log) if os.path.exists(log) else [], [])

            self.tool(root, env, "resume", "--state-root", state, "--force", "run1", clear=cleared)
            self.assertEqual(self.payload(install), "v1\n")
            verify = self.journal(state)["verify"]
            self.assertEqual((verify["result"], verify["forced"], verify["checks"]["elf"]), ("fail", True, 1))
            forced = {"result": "fail", "forced": True, "checks": verify["checks"]}
            published = self.assert_protocol(log, head, install)
            self.assertEqual(
                [(entry["stage"], entry["build"].get("verify")) for entry in published],
                [("started", forced), ("finished", forced)],
            )

            self.tool(root, env, "rollback", "--state-root", state, "run1", clear=cleared)
            corrections = self.assert_protocol(log, head, install)[2:]
            self.assertEqual([entry["action"] for entry in corrections], ["correction", "correction"])
            self.assertEqual([entry["build"].get("verify") for entry in corrections], [None, None])

            del env["FAKE_EB_EXTRA"]
            self.tool(root, env, *["run2" if word == "run1" else word for word in args], clear=cleared)
            clean = self.assert_protocol(log, head, install)[4:]
            self.assertEqual([(entry["stage"], entry["build"].get("verify")) for entry in clean],
                             [("started", None), ("finished", None)])


if __name__ == "__main__":
    unittest.main()
