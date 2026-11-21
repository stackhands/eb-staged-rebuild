"""Run EasyBuild so it writes a generation that is not the live software directory."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.probe import USER_NAMESPACE_FLAGS

# Options that would send the build at the live tree, or that delete a previous
# install when the module file and the new install directory differ.
_FLAGS = {
    "--bwrap",
    "--disable-bwrap",
    "--remove-ghost-install-dirs",
}
_WITH_VALUE = {
    "--installpath",
    "--installpath-software",
    "--installpath-modules",
    "--prefix",
    "--bwrap-installpath",
    "--bwrap-options",
    "--subdir-software",
    "--subdir-modules",
}


def filter_eb_args(args, drop_buildpath=False):
    """Drop install-path and bubblewrap options. Keep the easyconfig and the rest."""
    with_value = _WITH_VALUE | {"--buildpath"} if drop_buildpath else _WITH_VALUE
    kept = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            continue
        name = arg.split("=", 1)[0]
        if name in _FLAGS:
            index += 1
            continue
        if name in with_value:
            if "=" not in arg:
                index += 2
            else:
                index += 1
            continue
        kept.append(arg)
        index += 1
    return kept


def ensure_rebuild(args):
    """Return EasyBuild's arguments with ``--rebuild`` added.

    Arguments that have ``--rebuild``, ``--force`` or ``-f`` already are returned as they are.
    """
    names = {arg.split("=", 1)[0] for arg in args}
    if "--rebuild" in names or "--force" in names or "-f" in names:
        return list(args)
    return list(args) + ["--rebuild"]


def modules_suffix(args, env):
    """Return the suffix EasyBuild adds to its module install path.

    The last ``--suffix-modules-path`` that :func:`filter_eb_args` keeps,
    else ``EASYBUILD_SUFFIX_MODULES_PATH`` in ``env``, else ``all``, as
    EasyBuild reads them. One set in a configuration file is not seen.
    """
    suffix = env.get("EASYBUILD_SUFFIX_MODULES_PATH", "all")
    args = filter_eb_args(args)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--suffix-modules-path="):
            suffix = arg.split("=", 1)[1]
        elif arg == "--suffix-modules-path" and index + 1 < len(args):
            index += 1
            suffix = args[index]
        index += 1
    return suffix


def module_path(modules, suffix):
    """Return the directory of module files that EasyBuild puts on ``MODULEPATH`` for ``modules``."""
    if suffix.startswith("/"):
        return suffix
    return os.path.join(modules, suffix) if suffix else modules


def child_env(base, software, modules, live_modules, eb_args, drop_buildpath=False):
    """Return the environment EasyBuild runs with.

    It is the caller's, without EasyBuild's install-path variables, with
    ``EASYBUILD_INSTALLPATH_SOFTWARE`` and ``EASYBUILD_INSTALLPATH_MODULES`` set to ``software``
    and ``modules``, and with the module files of ``live_modules``, ``all`` under it
    unless ``eb_args`` or ``base`` give EasyBuild another suffix, put first on ``MODULEPATH``
    when they are not on it.
    """
    env = dict(base)
    for key in (
        "EASYBUILD_INSTALLPATH",
        "EASYBUILD_PREFIX",
        "EASYBUILD_REMOVE_GHOST_INSTALL_DIRS",
        "EASYBUILD_INSTALLPATH_SOFTWARE",
        "EASYBUILD_INSTALLPATH_MODULES",
    ):
        env.pop(key, None)
    if drop_buildpath:
        env.pop("EASYBUILD_BUILDPATH", None)
    env["EASYBUILD_INSTALLPATH_SOFTWARE"] = os.fspath(software)
    env["EASYBUILD_INSTALLPATH_MODULES"] = os.fspath(modules)
    live = module_path(os.fspath(live_modules), modules_suffix(eb_args, base))
    parts = [part for part in env.get("MODULEPATH", "").split(":") if part]
    if live not in parts:
        env["MODULEPATH"] = ":".join([live, *parts]) if parts else live
    return env


def installpath_dirs(generation_root):
    """Return the ``software`` and ``modules`` directories of a generation."""
    software = os.path.join(generation_root, "software")
    modules = os.path.join(generation_root, "modules")
    return software, modules


def installpath_command(ctx):
    """Return the ``installpath`` build: command, environment, software and modules directories.

    EasyBuild installs into the generation's directories, given as its install paths.
    """
    software, modules = installpath_dirs(ctx.generation_root)
    build_dir = getattr(ctx, "build_dir", None)
    drop = bool(build_dir)
    cmd = [
        ctx.eb_executable,
        f"--installpath-software={software}",
        f"--installpath-modules={modules}",
        *([f"--buildpath={build_dir}"] if build_dir else []),
        *ensure_rebuild(filter_eb_args(ctx.eb_args, drop_buildpath=drop)),
    ]
    env = child_env(os.environ, software, modules, ctx.modules, ctx.eb_args, drop_buildpath=drop)
    return cmd, env, software, modules


def bwrap_command(ctx):
    """Return the ``bwrap`` build: command and environment.

    EasyBuild's bubblewrap mode binds the generation over the live install directory.
    """
    build_dir = getattr(ctx, "build_dir", None)
    drop = bool(build_dir)
    cmd = [
        ctx.eb_executable,
        "--experimental",
        "--bwrap",
        f"--bwrap-installpath={ctx.generation_root}",
        "--bwrap-options=--die-with-parent",
        *([f"--buildpath={build_dir}"] if build_dir else []),
        *ensure_rebuild(filter_eb_args(ctx.eb_args, drop_buildpath=drop)),
    ]
    env = child_env(os.environ, ctx.software, ctx.modules, ctx.modules, ctx.eb_args, drop_buildpath=drop)
    return cmd, env


def userns_command(ctx):
    """Run an install-path build with both live trees mounted read-only."""
    inner, env, software, modules = installpath_command(ctx)
    report = (getattr(ctx, "probes", None) or {}).get("userns_sandbox") or {}
    unshare = report.get("path") or shutil.which("unshare") or "unshare"
    command = [
        unshare,
        *USER_NAMESPACE_FLAGS,
        sys.executable,
        "-m",
        "eb_staged_rebuild.userns_sandbox",
        "--read-only",
        ctx.software,
        "--read-only",
        ctx.modules,
        "--",
        *inner,
    ]
    return command, env, software, modules


def _dry_run_env(ctx):
    """Return the environment of a dry run: the build's, with basic output.

    With rich, EasyBuild wraps its output at the console width and, once
    ``FORCE_COLOR`` is set and not empty, colours it through a pipe too.
    ``EASYBUILD_OUTPUT_STYLE=basic``, which EasyBuild reads since 4.5.0,
    asks for neither; ``--output-style`` among the build's arguments wins.
    """
    env = child_env(os.environ, ctx.software, ctx.modules, ctx.modules, ctx.eb_args)
    env["EASYBUILD_OUTPUT_STYLE"] = "basic"
    return env


def dry_run_command(ctx):
    """List the modules the build replaces, without installing.

    The build's arguments with --rebuild, against the live prefix, so
    EasyBuild marks the easyconfigs to rebuild [R] and installed
    dependencies [x].
    """
    cmd = [
        ctx.eb_executable,
        "--dry-run",
        *ensure_rebuild(filter_eb_args(ctx.eb_args)),
    ]
    return cmd, _dry_run_env(ctx)


def extended_dry_run_command(ctx):
    """Name each module the build installs, without installing.

    The dry run's arguments, with ``--extended-dry-run`` for ``--dry-run``:
    EasyBuild resolves the dependencies as the build does and goes through
    each easyconfig it would build, which ``--stop=fetch`` ends after its
    first step. EasyBuild 5.4.2 creates the install lock of each in
    ``.locks`` under the live software tree and, in this mode, does not
    remove it; ``--ignore-locks`` takes none.
    """
    cmd = [
        ctx.eb_executable,
        "--extended-dry-run",
        *ensure_rebuild(filter_eb_args(ctx.eb_args)),
        "--stop=fetch",
        "--ignore-locks",
    ]
    return cmd, _dry_run_env(ctx)


def _signal_group(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _group_running(pgid):
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] not in (b"Z", b"X"):
            return True
    return False


def _kill_group(child):
    """SIGKILL is delivered asynchronously; a member keeps its descriptors until it exits."""
    _signal_group(child, signal.SIGKILL)
    while _group_running(child.pid):
        time.sleep(0.01)


def _parse_easybuild_umask(text):
    value = text.strip()
    if not value:
        return None
    if value.lower().startswith("0o"):
        digits = value[2:]
        base = 8
    elif all(ch in "01234567" for ch in value):
        digits = value
        base = 8
    else:
        digits = value
        base = 10
    try:
        parsed = int(digits, base)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 0o777 else None


def _child_preexec(env):
    text = env.get("EASYBUILD_UMASK")
    mode = _parse_easybuild_umask(text) if text is not None else None
    if mode is None:
        return None

    def _set_umask():
        os.umask(mode)

    return _set_umask


def run_command(cmd, env, faults, step):
    """Keep the caller's lock held until the command process group has stopped.

    Catchable termination signals request group shutdown. The group leader
    remains unreaped until the last group signal, so its identifier cannot be
    reassigned to an unrelated process. Commands must keep their workers in
    their process group and join them before returning.
    """
    faults.reach(step)
    if threading.current_thread() is not threading.main_thread():
        raise Refused("external build and preparation commands require the main thread")
    if not os.path.isfile("/proc/self/stat"):
        raise Refused("external commands require /proc to confirm their process group stopped")
    interrupted = [None]

    def request_stop(signum, frame):
        if interrupted[0] is None:
            interrupted[0] = signum

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    handlers = {signum: signal.getsignal(signum) for signum in signals}
    child = None
    try:
        for signum in signals:
            signal.signal(signum, request_stop)
        child = subprocess.Popen(cmd, env=env, start_new_session=True, preexec_fn=_child_preexec(env))
        while interrupted[0] is None:
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                break
            time.sleep(0.02)
        if interrupted[0] is not None:
            _signal_group(child, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        # The unreaped leader reserves the process-group identifier. Stray
        # workers must not retain write access beyond command completion.
        _kill_group(child)
        code = child.wait()
        if interrupted[0] is not None:
            return 128 + interrupted[0]
        return code if code >= 0 else 128 - code
    finally:
        try:
            if child is not None and child.returncode is None:
                _kill_group(child)
                child.wait()
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def format_command(cmd):
    """Return a command as one line of text, as the log prints it."""
    return " ".join(cmd)
