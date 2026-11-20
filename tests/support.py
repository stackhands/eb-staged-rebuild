"""Shared fixtures and CLI helpers for eb-staged-rebuild tests."""

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Collection
from typing import NamedTuple

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


def _run(argv, env=None, cwd=None, clear=()):
    merged = {key: value for key, value in os.environ.items() if key not in clear}
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
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None, clear: Collection[str] = ()
) -> subprocess.CompletedProcess[str]:
    """Run the Python CLI with ARGS, importing it from this checkout when CWD is elsewhere.

    The variables named in CLEAR are left out of the inherited environment; ENV is added after.
    """
    if cwd is not None:
        search = [_REPO, os.environ.get("PYTHONPATH", "")]
        env = {"PYTHONPATH": os.pathsep.join(filter(None, search)), **(env or {})}
    return _run([*PYTHON_CLI, *args], env, cwd, clear)


def run_cli_optional_binary(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None, clear: Collection[str] = ()
) -> subprocess.CompletedProcess[str] | None:
    """Run the program named by TOOL_BIN with ARGS, or return None when TOOL_BIN is unset."""
    tool = os.environ.get("TOOL_BIN")
    if not tool:
        return None
    return _run([tool, *args], env, cwd, clear)


def run_cli_or_binary(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the program named by TOOL_BIN with ARGS when set, otherwise the Python CLI."""
    binary = run_cli_optional_binary(*args, env=env, cwd=cwd)
    if binary is not None:
        return binary
    return run_cli(*args, env=env, cwd=cwd)


PYTHON_CLI = (sys.executable, "-m", "eb_staged_rebuild.cli")


class Implementation(NamedTuple):
    """One front end: its name, the command that starts it, and how a test runs it to completion."""

    name: str
    command: tuple[str, ...]

    def run(self, *args, env=None, cwd=None, clear=()):
        runner = run_cli if self.name == "python" else run_cli_optional_binary
        return runner(*args, env=env, cwd=cwd, clear=clear)


def implementations():
    """The Python front end and, when TOOL_BIN is set, the program it names."""
    found = [Implementation("python", PYTHON_CLI)]
    if os.environ.get("TOOL_BIN"):
        found.append(Implementation("binary", (os.environ["TOOL_BIN"],)))
    return found


def remove_tree(root):
    """Remove ROOT, first giving back the write access a test took from its directories."""
    for directory, dirs, _files in os.walk(root):
        for entry in dirs:
            path = os.path.join(directory, entry)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
    shutil.rmtree(root)


def sites(test, make, prefix):
    """``make(implementation, root)`` for each front end, each in a fresh ROOT that TEST removes when it ends.

    A list, so a test opens its own ``subTest`` around each site rather than a generator yielding from inside one.
    """
    made = []
    for implementation in implementations():
        root = tempfile.mkdtemp(prefix=prefix)
        test.addCleanup(remove_tree, root)
        made.append(make(implementation, root))
    return made


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
    """A live demo/1 and the commands that stage, verify and publish it with one front end.

    LMOD_CMD, the EESSI variables and EASYBUILD_* are unset unless a command sets them.
    """

    def __init__(self, implementation, root):
        self.implementation = implementation
        self.name = implementation.name
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
        self.env = {"PYTHONPATH": _REPO, "TMPDIR": self.tmp, "HOME": os.path.join(root, "home")}

    def cli(self, *args, **env):
        clear = [key for key in os.environ if key in CLEARED or key.startswith("EASYBUILD_")]
        return self.implementation.run(*args, env={**self.env, **env}, cwd=self.root, clear=clear)

    def run(self, *options, extra=None, verify=True, **env):
        if extra is not None:
            env["FAKE_EB_EXTRA"] = json.dumps(extra)
        gate = ["--verify"] if verify else []
        return self.cli(
            "run", "--software", self.software, "--modules", self.modules, "--state-root", self.state,
            "--backend", "installpath", "--eb", self.eb, "--run-id", "r1", "--skip-live-check",
            "--module", "demo/1", *gate, *options, "--", "demo-1.eb", f"--buildpath={self.root}/build", **env,
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
