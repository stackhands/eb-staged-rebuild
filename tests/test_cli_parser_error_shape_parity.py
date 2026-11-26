from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub

COMMANDS = "{probe,run,resume,verify,rollback,gc,retire,status,list,resolve}"


class CliParserErrorShapeParityTests(unittest.TestCase):
    def test_parser_error_shapes_match_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = os.path.join(tmp, "state")
            module_file = os.path.join(tmp, "module.lua")
            os.makedirs(state_root, exist_ok=True)
            with open(module_file, "w", encoding="utf-8") as handle:
                handle.write('local root = "/tmp/demo"\n')
            env = {"LANG": "en_US.UTF-8", "COLUMNS": "80"}
            probe = run_cli("probe", "--", "extra", env=env)
            self.assertIn(COMMANDS, probe.stderr)
            golden(self, "probe-extra.stdout", scrub(probe.stdout, TMP=tmp))
            golden(self, "probe-extra.stderr", scrub(probe.stderr, TMP=tmp))
            cases = (
                ("--wat", "gc", "--state-root", state_root, "extra"),
                ("--wat", "gc", "--state-root", state_root, "--bogus"),
                ("--wat", "retire"),
                ("resolve", "MOD", "--", "x"),
                ("resolve", "--", "a", "--", "b"),
                ("rollback", "--state-root", state_root, "r1", "--", "r2"),
                ("gc", "--state-root", state_root, "--", "extra"),
                ("probe", "--", "extra"),
                ("retire", "--state-root", state_root, "--module-file", module_file, "--", "extra"),
            )
            for index, args in enumerate(cases):
                with self.subTest(args=args):
                    binary = run_cli(*args, env=env)
                    golden(self, f"case{index}.returncode", f"{binary.returncode}\n")
                    golden(self, f"case{index}.stdout", scrub(binary.stdout, STATE=state_root, MODULE=module_file, TMP=tmp))
                    golden(self, f"case{index}.stderr", scrub(binary.stderr, STATE=state_root, MODULE=module_file, TMP=tmp))


if __name__ == "__main__":
    unittest.main()
