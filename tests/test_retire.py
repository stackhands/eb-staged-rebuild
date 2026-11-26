"""retire through the binary: usage-log guard and a non-UTF-8 module path."""

import os
import sys
import tempfile
import unittest

from tests.support import classic_install, golden, run_cli, scrub, write


def _tree(root):
    software = os.path.join(root, "software")
    modules = os.path.join(root, "modules")
    state = os.path.join(root, "state")
    os.makedirs(software)
    os.makedirs(modules)
    install, module, text = classic_install(software, modules, "demo/1", "v0\n", "all/demo/1.lua")
    return state, install, module, text


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _keep_logs_stub(root):
    script = os.path.join(root, "eb-keep-logs")
    write(
        script,
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_eb_keep_logs.py")}" "$@"\n',
        0o755,
    )
    return script


class RetireTests(unittest.TestCase):
    def test_usage_log_allows_retire_when_prefix_is_clear(self):
        with tempfile.TemporaryDirectory() as root:
            state, install, module, text = _tree(root)
            keep_logs = _keep_logs_stub(root)
            usage = os.path.join(root, "usage.log")
            write(usage, "{}\n")
            done = run_cli(
                "retire",
                "--state-root",
                state,
                "--module-file",
                module,
                "--usage-log",
                usage,
                "--eb-keep-logs",
                keep_logs,
                env={"FAKE_KEEP_LOGS_MODE": "clear"},
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertFalse(os.path.lexists(module))
            self.assertEqual(_read(os.path.join(install, "PAYLOAD")), "v0\n")
            moved = [
                os.path.join(directory, name)
                for directory, _dirs, names in os.walk(os.path.join(state, "retired"))
                for name in names
            ]
            self.assertEqual(len(moved), 1, moved)
            self.assertTrue(moved[0].endswith(module.lstrip(os.sep)), moved[0])
            self.assertEqual(_read(moved[0]), text)


class RetireCliParityTests(unittest.TestCase):
    def test_retire_refuses_non_utf8_module_path(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, exist_ok=True)
            module = os.path.join(root, (b"nonutf-\xff.lua").decode("utf-8", "surrogateescape"))
            with open(module, "w", encoding="utf-8") as handle:
                handle.write('local root = "/x"\n')

            done = run_cli("retire", "--state-root", state, "--module-file", module)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertEqual(done.stdout, "")
            self.assertIn("module path is not UTF-8 text", done.stderr)
            golden(self, "stderr", scrub(done.stderr, ROOT=root))


if __name__ == "__main__":
    unittest.main()
