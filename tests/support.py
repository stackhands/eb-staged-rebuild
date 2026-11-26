"""Shared fixtures and CLI helpers for eb-staged-rebuild tests."""

import contextlib
import ctypes
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Collection, Sequence

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(_REPO, "tests", "golden")


def lua(root, name="demo", version="1", loads=()):
    lines = [
        f'local root = "{root}"',
        f'conflict("{name}")',
    ]
    for dep in loads:
        lines.append(f'load("{dep}")')
    lines.append(f'setenv("EBROOT{name.upper()}", root)')
    lines.append(f'setenv("EBVERSION{name.upper()}", "{version}")')
    lines.append("")
    return "\n".join(lines)


def abspath(path):
    """PATH made absolute and normalized, without resolving symbolic links."""
    return os.path.abspath(os.path.normpath(os.fspath(path)))


def classic_install(software, modules, rel, payload, module_rel, name="demo", version="1", loads=()):
    """A live install under SOFTWARE/REL with its module file under MODULES/MODULE_REL.

    Returns the install directory, the module file and the module text.
    """
    install = os.path.join(software, rel)
    write(os.path.join(install, "PAYLOAD"), payload)
    write(os.path.join(install, "easybuild", f"{name}-{version}.eb"), f"name = '{name}'\n")
    module = os.path.join(modules, module_rel)
    text = lua(abspath(install), name=name, version=version, loads=loads)
    write(module, text)
    return abspath(install), abspath(module), text


class StateRootLock:
    """The lock a command holds on its state root.

    An flock gate on ``.lock`` and a POSIX record lock on ``.lock.posix``, taken
    without waiting; a command that finds either held refuses. ``acquire`` raises
    BlockingIOError when another holder has one of them.
    """

    def __init__(self, state_root):
        self.state_root = os.fspath(state_root)
        self.gate = self.posix = None

    def acquire(self):
        os.makedirs(self.state_root, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        gate = os.open(os.path.join(self.state_root, ".lock"), flags, 0o600)
        posix = None
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            posix = os.open(os.path.join(self.state_root, ".lock.posix"), flags, 0o600)
            fcntl.lockf(posix, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(gate, 0)
            os.write(gate, f"{os.getpid()}\n".encode())
        except BaseException:
            for fd in (posix, gate):
                if fd is not None:
                    os.close(fd)
            raise
        self.gate, self.posix = gate, posix

    def release(self):
        gate, posix = self.gate, self.posix
        self.gate = self.posix = None
        for fd in (posix, gate):
            if fd is not None:
                os.close(fd)


def _lock_and_sleep(state_root: str, acquired) -> None:
    lock = StateRootLock(state_root)
    lock.acquire()
    acquired.set()
    try:
        time.sleep(30)
    finally:
        lock.release()


def start_lock_holder_process(state_root: str):
    """A process that holds the state root's lock until it is terminated."""
    import multiprocessing as mp

    acquired = mp.Event()
    proc = mp.Process(target=_lock_and_sleep, args=(state_root, acquired))
    proc.start()
    if acquired.wait(5):
        return proc
    proc.terminate()
    proc.join(5)
    raise RuntimeError("lock holder did not acquire the state-root lock")


@contextlib.contextmanager
def hold_state_root_lock(state_root: str):
    """Hold the state root's lock in the test process for the duration of the block."""
    lock = StateRootLock(state_root)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def pause_fifo(directory):
    """A FIFO to name in EB_STAGED_REBUILD_PAUSE_READY, made in DIRECTORY, and its read end.

    The pause hook opens that path for writing and closes it when the command
    reaches its pause step. With the read end opened before the command starts,
    the close is an event ``wait_for_pause`` can wait for.
    """
    path = os.path.join(tempfile.mkdtemp(dir=directory, prefix="pause-"), "ready")
    os.mkfifo(path)
    return path, os.open(path, os.O_RDONLY | os.O_NONBLOCK)


def wait_for_pause(proc, fifo, timeout=60):
    """Whether PROC reached its pause step: its hook opened and closed FIFO before PROC exited or TIMEOUT seconds.

    FIFO is the read end ``pause_fifo`` returned; it is closed here.
    """
    exited = os.pidfd_open(proc.pid)
    try:
        poller = select.poll()
        poller.register(fifo, select.POLLIN | select.POLLHUP)
        poller.register(exited, select.POLLIN)
        return fifo in dict(poller.poll(timeout * 1000))
    finally:
        os.close(exited)
        os.close(fifo)


def _run(argv, env=None, cwd=None, clear=(), prefix=()):
    merged = {key: value for key, value in os.environ.items() if key not in clear}
    if env:
        merged.update(env)
    completed = subprocess.run([*prefix, *argv], capture_output=True, text=False, check=False, env=merged, cwd=cwd)
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        completed.stdout.decode("utf-8", "surrogateescape"),
        completed.stderr.decode("utf-8", "surrogateescape"),
    )


def path_without_eb() -> str:
    """PATH without the directories that hold an ``eb``.

    A dry run reports the EasyBuild it finds on PATH; the recorded output is
    the one for a host with none, so a host that has EasyBuild installed must
    not change it.
    """
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not os.access(os.path.join(d, "eb"), os.X_OK)]
    return os.pathsep.join(keep)


def tool_bin() -> str:
    """The binary under test: TOOL_BIN, else zig-out/bin/eb-staged-rebuild of this checkout.

    ``zig build -Drelease=true`` builds the second.
    """
    tool = os.environ.get("TOOL_BIN")
    if tool:
        return tool
    built = os.path.join(_REPO, "zig-out", "bin", "eb-staged-rebuild")
    if os.access(built, os.X_OK):
        return built
    raise AssertionError("TOOL_BIN names the eb-staged-rebuild binary under test; `zig build -Drelease=true` builds it")


_USERNS_TOKEN = "SANDBOX"


def normalize_userns_helper(text, binary=None):
    """Replace the argv of the user-namespace helper, the binary and its ``__userns-sandbox`` command, with one token."""
    tool = tool_bin() if binary is None else binary
    text = text.replace(f"{tool} __userns-sandbox", _USERNS_TOKEN)
    resolved = os.path.realpath(tool)
    if resolved != tool:
        text = text.replace(f"{resolved} __userns-sandbox", _USERNS_TOKEN)
    return text


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    clear: Collection[str] = (),
    prefix: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the binary tool_bin() names with ARGS.

    The variables named in CLEAR are left out of the inherited environment; ENV is added after.
    PREFIX is a command, such as ``unshare``, that runs the binary.
    """
    return _run([tool_bin(), *args], env, cwd, clear, prefix)


def _skip(case: unittest.TestCase, reason: str) -> None:
    if os.environ.get("EB_STAGED_REBUILD_REQUIRE_TESTS") == "1":
        case.fail(reason)
    case.skipTest(reason)


def _ptrace_problem() -> str | None:
    libc = ctypes.CDLL(None, use_errno=True)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        if libc.ptrace(0, 0, None, None) != 0:
            os.write(write_fd, os.strerror(ctypes.get_errno()).encode())
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    _pid, status = os.waitpid(pid, 0)
    detail = os.read(read_fd, 200).decode()
    os.close(read_fd)
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return None
    if detail:
        return detail
    return f"ptrace(PTRACE_TRACEME) failed with wait status {status}"


def _program(case: unittest.TestCase, binary_name: str) -> str:
    if shutil.which("gcc") is None:
        case.skipTest("gcc is not on PATH, so tests/fs_fail.c cannot be built")
    problem = _ptrace_problem()
    if problem:
        case.skipTest(f"ptrace is unavailable: {problem}")
    src = os.path.join(_REPO, "tests", "fs_fail.c")
    build_root = tempfile.TemporaryDirectory(prefix=f"{binary_name}-")
    case.addCleanup(build_root.cleanup)
    out = os.path.join(build_root.name, binary_name)
    built = subprocess.run(
        ["gcc", "-O2", "-o", out, src],
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        detail = (built.stderr or built.stdout or "gcc failed").strip()
        case.skipTest(f"gcc could not build tests/fs_fail.c: {detail}")
    return out


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


def _compile_fuse_faultfs(src: str, binary: str) -> None:
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "fuse3"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    done = subprocess.run(
        ["cc", "-O2", "-Wall", "-Wextra", "-o", binary, src, *flags.stdout.split()],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)


def _wait_mount(case: unittest.TestCase, mnt: str, mounted: subprocess.Popen[bytes]) -> None:
    for _ in range(50):
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            if f" {mnt} " in handle.read():
                return
        if mounted.poll() is not None:
            err = mounted.stderr.read().decode() if mounted.stderr else ""
            case.fail(f"FUSE exited: {err}")
        time.sleep(0.05)
    case.fail("FUSE mount did not appear")


def remove_tree(root):
    """Remove ROOT, first giving back the write access a test took from its directories."""
    for directory, dirs, _files in os.walk(root):
        for entry in dirs:
            path = os.path.join(directory, entry)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
    shutil.rmtree(root)


def site(test, prefix):
    """A Site in a fresh directory that TEST removes when it ends."""
    root = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(remove_tree, root)
    return Site(root)


def write(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as handle:
        handle.write(data)
    if mode is not None:
        os.chmod(path, mode)


FAKE_EB = os.path.join(_REPO, "tests", "fake_eb.py")
FAKE_LMOD = os.path.join(_REPO, "tests", "fake_lmod.py")
CLEARED = ("LMOD_CMD", "MODULEPATH", "LOADEDMODULES", "_LMFILES_", "EESSI_CVMFS_REPO", "EESSI_EPREFIX")


class Site:
    """A live demo/1 and the commands that stage, verify and publish it with the binary.

    LMOD_CMD, the EESSI variables and EASYBUILD_* are unset unless a command sets them.
    """

    def __init__(self, root):
        self.root = root
        self.software = os.path.join(root, "software")
        self.modules = os.path.join(root, "modules")
        self.state = os.path.join(root, "state")
        self.tmp = os.path.join(root, "tmp")
        self.live = os.path.join(self.software, "demo", "1")
        write(os.path.join(self.live, "PAYLOAD"), "old\n")
        write(
            os.path.join(self.modules, "all", "demo", "1.lua"),
            f'local root = "{self.live}"\nsetenv("EBROOTDEMO", root)\n',
        )
        os.makedirs(self.tmp)
        self.eb = os.path.join(root, "eb")
        write(self.eb, f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n', 0o755)
        self.env = {"TMPDIR": self.tmp, "HOME": os.path.join(root, "home")}

    def cli(self, *args, prefix=(), **env):
        return run_cli(*args, env={**self.env, **env}, cwd=self.root, clear=self.cleared(), prefix=prefix)

    def cleared(self):
        return [key for key in os.environ if key in CLEARED or key.startswith("EASYBUILD_")]

    def environ(self, **env):
        """The environment ``cli`` gives a command, for a process the test starts itself."""
        kept = {key: value for key, value in os.environ.items() if key not in self.cleared()}
        return {**kept, **self.env, **env}

    def run(self, *options, extra=None, verify=True, run_id="r1", modules=("demo/1",), **env):
        if extra is not None:
            env["FAKE_EB_EXTRA"] = json.dumps(extra)
        gate = ["--verify"] if verify else []
        named = [arg for rel in modules for arg in ("--module", rel)]
        return self.cli(
            "run", "--software", self.software, "--modules", self.modules, "--state-root", self.state,
            "--backend", "installpath", "--eb", self.eb, "--run-id", run_id, "--skip-live-check",
            *named, *gate, *options, "--", "demo-1.eb", f"--buildpath={self.root}/build", **env,
        )

    def journal_path(self, run_id="r1"):
        return os.path.join(self.state, "runs", run_id, "journal.json")

    def journal(self, run_id="r1"):
        with open(self.journal_path(run_id), encoding="utf-8") as handle:
            return json.load(handle)

    def report(self, run_id="r1"):
        with open(self.report_path(run_id), encoding="utf-8") as handle:
            return json.load(handle)

    def report_path(self, run_id="r1"):
        return os.path.join(self.state, "runs", run_id, "verify.json")

    def staged(self):
        return self.journal()["modules_plan"][0]["staged"]

    def payload(self):
        with open(os.path.join(self.live, "PAYLOAD"), encoding="utf-8") as handle:
            return handle.read()


def scrub(text, **placeholders):
    """TEXT with each value of PLACEHOLDERS replaced by ``@NAME@``, the longest value first.

    A path the test made is a different string on every run; the golden files hold the placeholder.
    """
    for name, value in sorted(placeholders.items(), key=lambda item: -len(item[1])):
        if value:
            text = text.replace(value, f"@{name}@")
            resolved = os.path.realpath(value)
            if resolved != value:
                text = text.replace(resolved, f"@{name}@")
    # A journal's snapshot records the device and inode numbers of the host.
    text = re.sub(r'"(device|inode)": [0-9]+', r'"\1": HOST', text)
    # What the probe reads from this host's kernel, file system and bwrap.
    text = re.sub(r"(user\.max_user_namespaces|kernel\.unprivileged_userns_clone): [0-9]+", r"\1: HOST", text)
    text = re.sub(r"RENAME_EXCHANGE on [A-Za-z0-9_.]+:", "RENAME_EXCHANGE on HOST:", text)
    text = re.sub(r'(\(|"version": ")bubblewrap [0-9][0-9.]*', r"\1bubblewrap HOST", text)
    # The faultfs mount a test makes, and placeholders, stay.
    text = re.sub(r'"(mount|fstype|device)": "(?!@|faultfs"|fuse\.faultfs")[^"]*"', r'"\1": "HOST"', text)
    text = re.sub(r'"(max_user_namespaces|unprivileged_userns_clone)": [0-9]+', r'"\1": HOST', text)
    # A kernel that does not publish this sysctl reports null, not an int.
    text = re.sub(r'"unprivileged_userns_clone": null', '"unprivileged_userns_clone": HOST', text)
    return re.sub(r"^( {6,})[0-9]{5,}(,?)$", r"\1INO\2", text, flags=re.M)


def golden(case, part, text):
    """Assert that TEXT is what ``tests/golden/MODULE/CLASS.TEST.PART`` holds.

    ``RECORD=1`` writes the file instead of comparing, so a change in output that a
    person has read and accepted is one command and a diff to review.
    """
    module, klass, method = case.id().split(".")[-3:]
    path = os.path.join(GOLDEN, module, f"{klass}.{method}.{part}")
    if os.environ.get("RECORD") == "1":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(text)
        return
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            expected = handle.read()
    except FileNotFoundError:
        case.fail(f"no golden file {os.path.relpath(path, _REPO)}; RECORD=1 writes it")
    case.assertEqual(text, expected, f"differs from {os.path.relpath(path, _REPO)}; RECORD=1 rewrites it")
