"""A failed punch of a zero page stops the prefix rewrite.

The file is on a FUSE passthrough whose fallocate returns EIO. Python
raises. The mount is the whole state root, so the refusal is the punch
and not a cross-device rename.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faultfs.c")


def _fuse_ready() -> str | None:
    if not os.path.exists("/dev/fuse"):
        return "FUSE is not available"
    if shutil.which("fusermount3") is None or shutil.which("cc") is None:
        return "FUSE is not available"
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "fuse3"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if flags.returncode != 0:
        return "FUSE is not available"
    return None


def _compile(binary: str) -> None:
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "fuse3"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    done = subprocess.run(
        ["cc", "-O2", "-Wall", "-Wextra", "-o", binary, _SRC, *flags.stdout.split()],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)


class PrefixPunchEioTests(unittest.TestCase):
    def test_failed_punch_keeps_the_original_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the filesystem is mounted by an unprivileged user")
        reason = _fuse_ready()
        if reason:
            self.skipTest(reason)
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            self.skipTest("TOOL_BIN is unset")
        python = self._run("python")
        binary = self._run(tool)
        self.assertEqual(python.returncode, 2, python.stderr)
        self.assertEqual(binary.returncode, 2, binary.stderr)
        self.assertIn("fallocate(PUNCH_HOLE)", python.stderr)
        self.assertIn("fallocate(PUNCH_HOLE)", binary.stderr)
        self.assertIn("Input/output error", python.stderr)
        self.assertIn("Input/output error", binary.stderr)
        self.assertIn("generations/r1", python.stdout)
        self.assertIn("generations/r1", binary.stdout)
        self.assertIn("LIVE old", python.stdout)
        self.assertIn("LIVE old", binary.stdout)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        work = f"/tmp/punch-eio-{os.getpid()}-{tool if tool == 'python' else 'zig'}"
        if os.path.isdir(os.path.join(work, "mnt")) and os.path.ismount(os.path.join(work, "mnt")):
            subprocess.run(["fusermount3", "-u", os.path.join(work, "mnt")], check=False)
        shutil.rmtree(work, ignore_errors=True)
        back = os.path.join(work, "back")
        mnt = os.path.join(work, "mnt")
        os.makedirs(os.path.join(back, "software", "demo", "1"))
        os.makedirs(os.path.join(back, "modules", "all", "demo"))
        os.makedirs(os.path.join(back, "generations", "r1", "software", "demo", "1"))
        with open(os.path.join(back, "software", "demo", "1", "PAYLOAD"), "w", encoding="utf-8") as handle:
            handle.write("old\n")
        with open(os.path.join(back, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{mnt}/software/demo/1"\n')
        generation = f"{mnt}/generations/r1"
        text = f"prefix={generation}/software/demo/1\n".encode()
        pad = 4096 - (len(text) % 4096)
        blob = text + b"\0" * pad + b"\0" * 8192
        kept_back = os.path.join(back, "generations", "r1", "software", "demo", "1", "kept.txt")
        with open(kept_back, "wb") as handle:
            handle.write(blob)
        eb = os.path.join(work, "fake-eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    for arg in "$@"; do
                      case "$arg" in
                        --installpath-software=*) sw="${arg#*=}" ;;
                        --installpath-modules=*) mods="${arg#*=}" ;;
                      esac
                    done
                    mkdir -p "$sw/demo/1" "$mods/all/demo"
                    printf 'new\\n' > "$sw/demo/1/PAYLOAD"
                    printf 'local root = "%s/demo/1"\\n' "$sw" > "$mods/all/demo/1.lua"
                    """
                )
            )
        os.chmod(eb, 0o755)
        binary = os.path.join(work, "faultfs")
        _compile(binary)
        os.makedirs(mnt)
        env = dict(os.environ)
        env["FAULTFS_FALLOCATE"] = "eio"
        env.pop("FAULTFS_SEAL", None)
        mounted = subprocess.Popen(
            [binary, back, mnt],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            self._wait_mount(mnt, mounted)
            run_env = dict(os.environ)
            run_env["PYTHONPATH"] = _REPO
            run_env["PYTHONIOENCODING"] = "utf-8:strict"
            argv = [sys.executable, "-m", "eb_staged_rebuild"] if tool == "python" else [tool]
            done = subprocess.run(
                [
                    *argv,
                    "run",
                    "--software",
                    os.path.join(mnt, "software"),
                    "--modules",
                    os.path.join(mnt, "modules"),
                    "--state-root",
                    mnt,
                    "--backend",
                    "installpath",
                    "--eb",
                    eb,
                    "--module",
                    "demo/1",
                    "--run-id",
                    "r1",
                    "--",
                    "demo-1.eb",
                ],
                check=False,
                capture_output=True,
                env=run_env,
                timeout=60,
            )
            kept = os.path.join(mnt, "generations", "r1", "software", "demo", "1", "kept.txt")
            if not os.path.isfile(kept):
                kept = os.path.join(mnt, "software", "demo", "1", "kept.txt")
            if os.path.isfile(kept):
                with open(kept, "rb") as handle:
                    body = handle.read()[:80]
            else:
                body = b""
            live = os.path.join(mnt, "software", "demo", "1", "PAYLOAD")
            if os.path.isfile(live):
                with open(live, encoding="utf-8") as handle:
                    live_text = handle.read().rstrip()
            else:
                live_text = "missing"
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=False, timeout=30)
            if mounted.stderr is not None and not mounted.stderr.closed:
                mounted.stderr.close()
            try:
                mounted.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mounted.kill()
        detail = done.stderr.decode("utf-8", "surrogateescape")
        stdout = done.stdout.decode("utf-8", "surrogateescape")
        stdout += f"\nBODY {body!r}\nLIVE {live_text}\n"
        return subprocess.CompletedProcess(done.args, done.returncode, stdout, detail)

    def _wait_mount(self, mnt: str, mounted: subprocess.Popen[bytes]) -> None:
        for _ in range(50):
            with open("/proc/self/mountinfo", encoding="utf-8") as handle:
                if f" {mnt} " in handle.read():
                    return
            if mounted.poll() is not None:
                err = mounted.stderr.read().decode() if mounted.stderr else ""
                self.fail(f"FUSE exited: {err}")
            time.sleep(0.05)
        self.fail("FUSE mount did not appear")


if __name__ == "__main__":
    unittest.main()
