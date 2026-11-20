from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from tests.support import implementations, run_cli, run_cli_optional_binary


class CliRollbackOutboxInvalidJsonParityTests(unittest.TestCase):
    def _write_fixture(self, root: str, outbox_bytes: bytes, recorded: str = "{}") -> tuple[str, str]:
        state_root = os.path.join(root, "state")
        run_id = "run1"
        run_dir = os.path.join(state_root, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)

        software_root = os.path.join(root, "software")
        modules_root = os.path.join(root, "modules", "all")
        live = os.path.join(software_root, "pkg", "1")
        retired = os.path.join(software_root, "pkg", ".1.run1")
        os.makedirs(live, exist_ok=True)
        os.makedirs(retired, exist_ok=True)
        with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("live\n")
        with open(os.path.join(retired, "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("retired\n")

        module_file = os.path.join(modules_root, "pkg", "1.lua")
        os.makedirs(os.path.dirname(module_file), exist_ok=True)
        with open(module_file, "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')

        fake_ledger = os.path.join(root, "fake-eb-stack")
        with open(fake_ledger, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                'exec python3 "' + os.path.abspath("tests/fake_eb_stack.py") + '" "$@"\n'
            )
        os.chmod(fake_ledger, 0o755)

        ledger_path = os.path.join(root, "ledger.json")
        ledger = {
            "eb_stack": fake_ledger,
            "log": os.path.join(root, "ledger.log"),
            "key": os.path.join(root, "ledger.key"),
            "head": os.path.join(root, "ledger.head"),
            "actor": "ci",
            "stack": "stack",
            "target": "target",
            "required": True,
            "timeout": 5,
            "action": "rebuild-inplace",
        }
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle)
        with open(ledger["key"], "w", encoding="utf-8") as handle:
            handle.write("placeholder\n")
        with open(ledger["head"], "w", encoding="utf-8") as handle:
            handle.write('{"size": 0, "root": "' + ("0" * 64) + '"}\n')

        journal = {
            "schema": 2,
            "run_id": run_id,
            "state": "complete",
            "backend": "installpath",
            "software": software_root,
            "modules": modules_root,
            "state_root": recorded.format(state_root),
            "generation_root": os.path.join(state_root, "generations", run_id),
            "module_rels": ["pkg/1.lua"],
            "prepare_complete": False,
            "prepare_returncode": None,
            "ledger_config": ledger_path,
            "ledger_policy": {**ledger, "path": ledger_path},
            "publication": "directory-swap",
            "created_at": time.time(),
            "updated_at": time.time(),
            "modules_plan": [
                {
                    "rel": "pkg/1.lua",
                    "status": "published",
                    "live_software": live,
                    "retired": retired,
                    "live_module": module_file,
                    "prepared": os.path.join(state_root, "generations", run_id, "modules", "pkg", "1.lua"),
                    "staged": os.path.join(state_root, "generations", run_id, "software", "pkg", "1"),
                    "new_hash": "a" * 64,
                    "old_hash": "b" * 64,
                    "published_at": time.time(),
                }
            ],
            "links_plan": [],
            "message": "",
            "eb_returncode": None,
            "eb_command": None,
            "retired_at": None,
            "generation_removed_at": None,
        }
        with open(os.path.join(run_dir, "journal.json"), "w", encoding="utf-8") as handle:
            json.dump(journal, handle)

        with open(os.path.join(run_dir, "outbox.json"), "wb") as handle:
            handle.write(outbox_bytes)
        return state_root, run_id

    def test_rollback_rejects_invalid_outbox_json_with_same_message(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            for outbox_bytes in (b"", b"{not json\n"):
                state_root, run_id = self._write_fixture(root, outbox_bytes)

                proc = run_cli("rollback", "--state-root", state_root, run_id)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("outbox cannot be read:", proc.stderr)
                self.assertIn("not valid JSON", proc.stderr)

                binary = run_cli_optional_binary("rollback", "--state-root", state_root, run_id)
                if binary is not None:
                    self.assertEqual(binary.returncode, 2)
                    self.assertIn("outbox cannot be read:", binary.stderr)
                    self.assertIn("not valid JSON", binary.stderr)

    def test_rollback_reads_the_outbox_under_the_state_root_it_was_given(self) -> None:
        """A journal state root of STATE/. is STATE, and the outbox is read under the spelling the caller gave."""
        for implementation in implementations():
            with self.subTest(tool=implementation.name), tempfile.TemporaryDirectory() as root:
                state_root, run_id = self._write_fixture(root, b"{not json\n", recorded="{}/.")
                outbox = os.path.join(state_root, "runs", run_id, "outbox.json")
                proc = implementation.run("rollback", "--state-root", state_root, run_id)
                self.assertEqual(
                    (proc.returncode, proc.stdout, proc.stderr),
                    (2, "", f"eb-staged-rebuild: outbox cannot be read: {outbox}: not valid JSON\n"),
                )


if __name__ == "__main__":
    unittest.main()
