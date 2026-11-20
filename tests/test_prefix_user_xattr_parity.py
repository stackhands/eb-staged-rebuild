"""A user attribute the filesystem will not copy stops the prefix rewrite.

The staged install is an overlay whose upper layer is ramfs. listxattr
on the lower file succeeds. setxattr of the new temporary file returns
ENOTSUP. The mount is made in a user namespace, which is what can mount
the overlay. Without that namespace, or without overlay, the test skips.
A child that exits with a status other than 0 or 2 did not run the tool.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TIMEOUT = 60

_PROBE = """
import os, subprocess, sys, tempfile
root = tempfile.mkdtemp(prefix="xattr-probe-")
lower = os.path.join(root, "lower")
ram = os.path.join(root, "ram")
mnt = os.path.join(root, "mnt")
os.makedirs(lower)
os.makedirs(ram)
os.makedirs(mnt)
try:
    subprocess.run(["mount", "-t", "ramfs", "ramfs", ram], check=True, timeout=30)
    os.makedirs(os.path.join(ram, "upper"))
    os.makedirs(os.path.join(ram, "work"))
    subprocess.run(
        [
            "mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir={lower},upperdir={os.path.join(ram, 'upper')},workdir={os.path.join(ram, 'work')}",
            mnt,
        ],
        check=True,
        timeout=30,
    )
except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
    sys.stderr.write("overlay mounts are not available\\n")
    raise SystemExit(1)
"""


def _namespace_problem() -> str | None:
    if shutil.which("unshare") is None:
        return "user namespaces are not available"
    try:
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--fork", sys.executable, "-c", _PROBE],
            check=False,
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "user namespaces are not available"
    if done.returncode == 0:
        return None
    err = done.stderr.decode("utf-8", "replace")
    if "overlay mounts are not available" in err:
        return "overlay mounts are not available"
    return "user namespaces are not available"


def _inside(tool: str) -> int:
    work = "/tmp/prefix-user-xattr"
    shutil.rmtree(work, ignore_errors=True)
    lower = os.path.join(work, "lower")
    ram = os.path.join(work, "ram")
    mnt = os.path.join(work, "mnt")
    os.makedirs(os.path.join(lower, "generations", "r1", "software", "demo", "1"))
    os.makedirs(ram)
    os.makedirs(mnt)
    kept_rel = os.path.join("generations", "r1", "software", "demo", "1", "kept.txt")
    prefix = os.path.join(mnt, "generations", "r1")
    original = f"prefix={prefix}/software/demo/1\n"
    kept = os.path.join(lower, kept_rel)
    with open(kept, "w", encoding="utf-8") as handle:
        handle.write(original)
    os.setxattr(kept, "user.ebsr", b"kept")
    subprocess.run(["mount", "-t", "ramfs", "ramfs", ram], check=True, timeout=_TIMEOUT)
    os.makedirs(os.path.join(ram, "upper"))
    os.makedirs(os.path.join(ram, "work"))
    subprocess.run(
        [
            "mount",
            "-t",
            "overlay",
            "overlay",
            "-o",
            f"lowerdir={lower},upperdir={os.path.join(ram, 'upper')},workdir={os.path.join(ram, 'work')}",
            mnt,
        ],
        check=True,
        timeout=_TIMEOUT,
    )
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
    live = os.path.join(mnt, "software", "demo", "1")
    os.makedirs(live)
    os.makedirs(os.path.join(mnt, "modules", "all", "demo"))
    with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
        handle.write("old\n")
    with open(os.path.join(mnt, "modules", "all", "demo", "1.lua"), "w", encoding="utf-8") as handle:
        handle.write(f'local root = "{live}"\n')
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO
    env["PYTHONIOENCODING"] = "utf-8:strict"
    if tool == "python":
        argv = [sys.executable, "-m", "eb_staged_rebuild"]
    else:
        argv = [tool]
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
        env=env,
        timeout=_TIMEOUT,
    )
    visible = os.path.join(mnt, kept_rel)
    if os.path.isfile(visible):
        with open(visible, encoding="utf-8") as handle:
            body = handle.read()
    else:
        body = ""
    attrs = os.listxattr(visible) if os.path.lexists(visible) else []
    sys.stdout.write(done.stdout.decode("utf-8", "surrogateescape"))
    sys.stderr.write(done.stderr.decode("utf-8", "surrogateescape"))
    print(f"BODY {body!r}")
    print(f"XATTR {attrs!r}")
    return done.returncode


class PrefixUserXattrParityTests(unittest.TestCase):
    def test_refused_user_xattr_keeps_the_original_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the overlay is mounted in a user namespace")
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            self.skipTest("TOOL_BIN is unset")
        problem = _namespace_problem()
        if problem:
            self.skipTest(problem)
        python = self._run("python")
        binary = self._run(tool)
        detail_py = python.stdout + python.stderr
        detail_bin = binary.stdout + binary.stderr
        self.assertEqual(python.returncode, 2, detail_py)
        self.assertEqual(binary.returncode, 2, detail_bin)
        self.assertIn("Operation not supported", python.stderr, detail_py)
        self.assertIn("Operation not supported", binary.stderr, detail_bin)
        self.assertIn("user.ebsr", python.stdout, detail_py)
        self.assertIn("user.ebsr", binary.stdout, detail_bin)
        self.assertIn("generations/r1", python.stdout, detail_py)
        self.assertIn("generations/r1", binary.stdout, detail_bin)

    def _run(self, tool: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8:strict"
        try:
            done = subprocess.run(
                [
                    "unshare",
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--fork",
                    sys.executable,
                    os.path.abspath(__file__),
                    "--inside",
                    tool,
                ],
                check=False,
                capture_output=True,
                env=env,
                timeout=_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            self.skipTest("user namespaces are not available")
        if done.returncode not in (0, 2):
            err = done.stderr.decode("utf-8", "replace").strip()
            self.skipTest(f"user namespace or overlay mount is not available: exit {done.returncode} {err}")
        return subprocess.CompletedProcess(
            done.args,
            done.returncode,
            done.stdout.decode("utf-8", "surrogateescape"),
            done.stderr.decode("utf-8", "surrogateescape"),
        )


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--inside":
        raise SystemExit(_inside(sys.argv[2]))
    unittest.main()
