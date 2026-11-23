"""Shared fixtures and CLI helpers for eb-staged-rebuild tests."""

import contextlib
import ctypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.paths import abspath

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBES = {
    "bwrap": {
        "installed": True,
        "usable": True,
        "reason": None,
        "version": "test",
        "path": "bwrap",
    },
    "user_namespaces": {"max_user_namespaces": None, "readable": False},
    "userns_sandbox": {
        "installed": True,
        "usable": True,
        "path": "unshare",
        "reason": None,
    },
    "rename_exchange": {"ok": False, "errno": 22, "error": "test", "fstype": "test"},
    "easybuild": {
        "found": True,
        "framework_version": [5, 4, 2],
        "stage_path_keys": True,
        "keys_source": "test",
        "version_text": "test",
        "reason": None,
    },
    "filesystem": {"fstype": "test"},
    "flock": {"cross_node": "unmeasured", "method": "flock"},
}


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


def classic_install(software, modules, rel, payload, module_rel, name="demo", version="1", loads=()):
    install = os.path.join(software, rel)
    os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
    silent = FaultInjector()
    atomic_write(os.path.join(install, "PAYLOAD"), payload, silent, "fixture.payload")
    atomic_write(
        os.path.join(install, "easybuild", f"{name}-{version}.eb"),
        f"name = '{name}'\n",
        silent,
        "fixture.eb",
    )
    module = os.path.join(modules, module_rel)
    text = lua(abspath(install), name=name, version=version, loads=loads)
    atomic_write(module, text, silent, "fixture.module")
    return abspath(install), abspath(module), text


def generation_of(state_root, run_id, rel):
    return abspath(os.path.join(state_root, "generations", run_id, "software", rel))


def installpath_builder(packages):
    """In-process EasyBuild stand-in.

    ``packages`` is a list of dicts: rel, payload, module_rel, name, version, loads.
    Fault points ``build.payload:<rel>`` and ``build.module:<rel>`` are raised
    before those files are written.
    """

    def build(ctx):
        silent = FaultInjector()
        for package in packages:
            rel = package["rel"]
            ctx.faults.reach(f"build.payload:{rel}")
            generation = os.path.join(ctx.generation_root, "software", rel)
            os.makedirs(os.path.join(generation, "easybuild"), exist_ok=True)
            atomic_write(
                os.path.join(generation, "PAYLOAD"),
                package["payload"],
                silent,
                "silent.payload",
            )
            name = package.get("name", "demo")
            version = package.get("version", "1")
            atomic_write(
                os.path.join(generation, "easybuild", f"{name}-{version}.eb"),
                f"name = '{name}'\n",
                silent,
                "silent.eb",
            )
            ctx.faults.reach(f"build.module:{rel}")
            module = os.path.join(ctx.generation_root, "modules", package["module_rel"])
            text = lua(
                abspath(generation),
                name=name,
                version=version,
                loads=package.get("loads") or (),
            )
            atomic_write(module, text, silent, "silent.module")
            link_rel = package.get("link_rel")
            if link_rel:
                link = os.path.join(ctx.generation_root, "modules", link_rel)
                os.makedirs(os.path.dirname(link), exist_ok=True)
                if not os.path.lexists(link):
                    os.symlink(abspath(module), link)
        return 0

    return build


def guard_calls(live_dirs):
    """Raise if a rename or delete names a path inside a live install directory."""
    banned = [abspath(path) for path in live_dirs]

    def illegal(path):
        path = abspath(path)
        return any(path == root or path.startswith(root + os.sep) for root in banned)

    def wrap(original):
        def wrapped(*args, **kwargs):
            for arg in args:
                if isinstance(arg, (str, os.PathLike)) and illegal(arg):
                    raise AssertionError(f"{original.__name__} touched live install {arg}")
            return original(*args, **kwargs)

        return wrapped

    return {
        "rename": wrap(os.rename),
        "replace": wrap(os.replace),
        "remove": wrap(os.remove),
        "unlink": wrap(os.unlink),
        "rmdir": wrap(os.rmdir),
    }


def patch_os(guards):
    originals = {name: getattr(os, name) for name in guards}
    for name, wrapped in guards.items():
        setattr(os, name, wrapped)
    return originals


def restore_os(originals):
    for name, original in originals.items():
        setattr(os, name, original)




def _lock_and_sleep(state_root: str, acquired) -> None:
    from eb_staged_rebuild.lockfile import FileLock

    lock = FileLock(state_root)
    lock.acquire(0)
    acquired.set()
    try:
        import time

        time.sleep(30)
    finally:
        lock.release()


def start_lock_holder_process(state_root: str):
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
def hold_state_root_lock(state_root: str, timeout: float | None = 0):
    """Hold ``FileLock(state_root)`` in the test process for the duration of the block."""
    from eb_staged_rebuild.lockfile import FileLock

    lock = FileLock(state_root)
    lock.acquire(timeout)
    try:
        yield lock
    finally:
        lock.release()


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _run(argv, env=None, cwd=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    completed = subprocess.run(argv, capture_output=True, text=False, check=False, env=merged, cwd=cwd)
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        completed.stdout.decode("utf-8", "surrogateescape"),
        completed.stderr.decode("utf-8", "surrogateescape"),
    )


def run_cli(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the Python CLI with ARGS, importing it from this checkout when CWD is elsewhere."""
    if cwd is not None:
        search = [_REPO, os.environ.get("PYTHONPATH", "")]
        env = {"PYTHONPATH": os.pathsep.join(filter(None, search)), **(env or {})}
    return _run([sys.executable, "-m", "eb_staged_rebuild.cli", *args], env, cwd)


def run_cli_optional_binary(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run the program named by TOOL_BIN with ARGS, or return None when TOOL_BIN is unset."""
    tool = os.environ.get("TOOL_BIN")
    if not tool:
        return None
    return _run([tool, *args], env, cwd)


def run_cli_or_binary(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the program named by TOOL_BIN with ARGS when set, otherwise the Python CLI."""
    binary = run_cli_optional_binary(*args, env=env, cwd=cwd)
    if binary is not None:
        return binary
    return run_cli(*args, env=env, cwd=cwd)


def _skip(case: unittest.TestCase, reason: str) -> None:
    if os.environ.get("EB_STAGED_REBUILD_REQUIRE_TESTS") == "1":
        case.fail(reason)
    case.skipTest(reason)


def _python314() -> str | None:
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    found = shutil.which("python3.14")
    if not found:
        return None
    completed = subprocess.run(
        [found, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)"],
        check=False,
    )
    return found if completed.returncode == 0 else None


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
