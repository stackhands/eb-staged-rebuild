"""Shared fixtures and CLI helpers for eb-staged-rebuild tests."""

import contextlib
import os
import stat
import subprocess
import sys

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




def _lock_and_sleep(state_root: str) -> None:
    from eb_staged_rebuild.lockfile import FileLock

    lock = FileLock(state_root)
    lock.acquire(0)
    try:
        import time

        time.sleep(30)
    finally:
        lock.release()


def start_lock_holder_process(state_root: str):
    import multiprocessing as mp
    import time

    from eb_staged_rebuild.errors import Refused
    from eb_staged_rebuild.lockfile import FileLock

    proc = mp.Process(target=_lock_and_sleep, args=(state_root,))
    proc.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        contender = FileLock(state_root)
        try:
            contender.acquire(0)
        except Refused:
            return proc
        else:
            contender.release()
            time.sleep(0.01)
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
