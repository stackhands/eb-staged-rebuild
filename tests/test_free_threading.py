"""The package works on a free-threaded CPython.

Importing it must not turn the GIL back on: CPython re-enables the GIL when it
imports an extension module that does not declare free-threading support, and
the package must carry none. Its functions keep no shared state, so calls from
many threads at once give the answers one thread gets.
"""

import os
import subprocess
import sys
import sysconfig
import tempfile
import threading
import unittest
from pathlib import Path

import eb_staged_rebuild
from tests.support import tool_bin

PACKAGE_PARENT = str(Path(eb_staged_rebuild.__file__).resolve().parents[1])
FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
THREADS = 16
CALLS = 8


def gil_after_importing_the_package() -> str:
    """What sys._is_gil_enabled() says after a fresh interpreter imports every module of the package."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    env["PYTHONPATH"] = PACKAGE_PARENT
    code = (
        "import sys\n"
        "import eb_staged_rebuild, eb_staged_rebuild.ship, eb_staged_rebuild.site_hooks\n"
        "print(sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else 'no _is_gil_enabled')\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tempfile.gettempdir())
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


class FreeThreadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.dirname(os.path.abspath(tool_bin())) + os.pathsep + cls.saved_path

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls.saved_path

    def test_importing_the_package_leaves_the_gil_as_the_build_has_it(self):
        if FREE_THREADED:
            expected = "False"
        elif hasattr(sys, "_is_gil_enabled"):
            expected = "True"
        else:
            expected = "no _is_gil_enabled"
        self.assertEqual(gil_after_importing_the_package(), expected)

    def test_calls_from_many_threads_agree_with_one_thread(self):
        alone_path = eb_staged_rebuild.binary_path()
        alone = eb_staged_rebuild.run("--version")
        self.assertEqual(alone.returncode, 0, alone.stderr)
        start = threading.Barrier(THREADS)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                start.wait()
                for _ in range(CALLS):
                    path = eb_staged_rebuild.binary_path()
                    done = eb_staged_rebuild.run("--version")
                    with lock:
                        results.append((path, done.returncode, done.stdout, done.stderr))
            except BaseException as error:  # reported below, with the thread's traceback lost
                with lock:
                    errors.append(repr(error))

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), THREADS * CALLS)
        self.assertEqual(set(results), {(alone_path, 0, alone.stdout, alone.stderr)})


if __name__ == "__main__":
    unittest.main()
