"""Check a built run's staged trees before they are published.

``elf``
    Each ``DT_NEEDED`` entry of each dynamic ELF object is found the way the
    dynamic loader searches (``DT_RPATH`` when there is no ``DT_RUNPATH``,
    then ``DT_RUNPATH``, then the loader's system directories) and lies inside
    the stage, the live software tree or EESSI. ``LD_LIBRARY_PATH`` and
    ``ld.so.cache`` are not consulted.
``references``
    No file outside EasyBuild's own ``easybuild/`` records names the build
    directory or an EasyBuild temporary directory (``eb-XXXXXXXX`` under a
    temporary directory), and no ``RPATH``/``RUNPATH`` entry, symbolic link
    or prepared module file names a path under a temporary directory. Files
    over ``SCAN_LIMIT`` bytes are not read.
``modules``
    ``$LMOD_CMD`` loads each staged module file with the stage first on
    ``MODULEPATH``, and each ``EBROOT*`` directory the load sets exists.
``access``
    The publishing user owns the staged tree and can write where publication
    renames and writes. Without a ``--prepare`` program, which sets the access
    policy itself, the staged tree has the live tree's owner, group, group and
    other mode bits and ACLs, and nothing in it is less readable than its top
    directory.
"""

import errno

import json
import os
import re
import stat
import struct
import subprocess

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import NeedsIntervention
from eb_staged_rebuild.journal import run_dir
from eb_staged_rebuild.paths import abspath

CHECKS = ("elf", "references", "modules", "access")
REPORT = "verify.json"
SHOWN = 50
STAMP = ".eb-staged-rebuild-complete"
SCAN_LIMIT = 64 << 20
LMOD_TIMEOUT = 120
LOADER_TIMEOUT = 30
# A path ends at the first byte that is none of these.
_PATH = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-/")
# EasyBuild's temporary directories are tempfile.mkdtemp(prefix="eb-") names.
_TMP_NAME = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789_")
# The loader a shared object without PT_INTERP is loaded by, per e_machine.
_LOADERS = {
    21: "lib64/ld64.so.2",
    62: "lib64/ld-linux-x86-64.so.2",
    183: "lib/ld-linux-aarch64.so.1",
    243: "lib/ld-linux-riscv64-lp64d.so.1",
}
_SYSTEM = b"(system search path)"
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*);\Z")


def _log(message):
    print(f"eb-staged-rebuild: {message}", flush=True)


def _option(args, name):
    flag = f"--{name}"
    found = None
    for index, arg in enumerate(args):
        if arg.startswith(flag + "="):
            found = arg[len(flag) + 1 :]
        elif arg == flag and index + 1 < len(args):
            found = args[index + 1]
    return found


def context(eb_args, environ):
    """The build and temporary directories and EESSI roots a run's stage is judged against."""
    build = []
    tmp = []

    def remember(dirs, value):
        if value:
            value = abspath(value)
            if value not in dirs:
                dirs.append(value)

    remember(build, _option(eb_args, "buildpath"))
    remember(build, environ.get("EASYBUILD_BUILDPATH"))
    if environ.get("EASYBUILD_PREFIX"):
        remember(build, os.path.join(environ["EASYBUILD_PREFIX"], "build"))
    if environ.get("HOME"):
        remember(build, os.path.join(environ["HOME"], ".local", "easybuild", "build"))
    remember(tmp, _option(eb_args, "tmpdir"))
    remember(tmp, environ.get("EASYBUILD_TMPDIR"))
    remember(tmp, environ.get("TMPDIR"))
    remember(tmp, "/tmp")
    return {
        "build_dirs": build,
        "tmp_dirs": tmp,
        "eessi": abspath(environ["EESSI_CVMFS_REPO"]) if environ.get("EESSI_CVMFS_REPO") else None,
        "eessi_prefix": abspath(environ["EESSI_EPREFIX"]) if environ.get("EESSI_EPREFIX") else None,
    }


class Elf:
    """The dynamic-linking entries of one ELF object: machine, interpreter, DT_NEEDED, DT_RPATH and DT_RUNPATH."""

    def __init__(self, machine, interp, needed, rpath, runpath):
        self.machine = machine
        self.interp = interp
        self.needed = needed
        self.rpath = rpath
        self.runpath = runpath


def read_elf(handle):
    """The dynamic-linking entries of an ELF executable or shared object, or None."""
    try:
        return _read_elf(handle)
    except (OSError, OverflowError, ValueError, struct.error):
        return None


def _read_elf(handle):
    head = handle.read(64)
    if len(head) < 52 or head[:4] != b"\x7fELF" or head[4] not in (1, 2) or head[5] not in (1, 2):
        return None
    wide = head[4] == 2
    order = "<" if head[5] == 1 else ">"
    if wide and len(head) < 64:
        return None
    kind, machine = struct.unpack_from(order + "HH", head, 16)
    if kind not in (2, 3):
        return None
    if wide:
        (phoff,) = struct.unpack_from(order + "Q", head, 32)
        size, count = struct.unpack_from(order + "HH", head, 54)
        program, minimum, dynamic, step = order + "IIQQQQ", 56, order + "qQ", 16
    else:
        (phoff,) = struct.unpack_from(order + "I", head, 28)
        size, count = struct.unpack_from(order + "HH", head, 42)
        program, minimum, dynamic, step = order + "IIIII", 32, order + "iI", 8
    if size < minimum or count == 0:
        return None
    handle.seek(phoff)
    table = handle.read(size * count)
    if len(table) < size * count:
        return None
    loads = []
    found = None
    interp_at = None
    for index in range(count):
        fields = struct.unpack_from(program, table, index * size)
        if wide:
            kind, _flags, offset, vaddr, _paddr, filesz = fields
        else:
            kind, offset, vaddr, _paddr, filesz = fields
        if kind == 1:
            loads.append((vaddr, offset, filesz))
        elif kind == 2:
            found = (offset, filesz)
        elif kind == 3:
            interp_at = (offset, filesz)
    if found is None:
        return None
    handle.seek(found[0])
    entries = handle.read(min(found[1], 1 << 20))
    needed = []
    strtab = strsz = rpath = runpath = None
    for index in range(len(entries) // step):
        tag, value = struct.unpack_from(dynamic, entries, index * step)
        if tag == 0:
            break
        if tag == 1:
            needed.append(value)
        elif tag == 5:
            strtab = value
        elif tag == 10:
            strsz = value
        elif tag == 15:
            rpath = value
        elif tag == 29:
            runpath = value
    if strtab is None or strsz is None:
        return None
    base = None
    for vaddr, offset, filesz in loads:
        if vaddr <= strtab < vaddr + filesz:
            base = strtab - vaddr + offset
            break
    if base is None:
        return None
    handle.seek(base)
    strings = handle.read(min(strsz, 16 << 20))

    def text(offset):
        end = strings.find(b"\0", offset)
        if offset >= len(strings) or end < 0:
            raise struct.error("string outside the string table")
        return os.fsdecode(strings[offset:end])

    interp = None
    if interp_at is not None:
        handle.seek(interp_at[0])
        interp = os.fsdecode(handle.read(min(interp_at[1], 4096)).split(b"\0", 1)[0])
    return Elf(
        machine,
        interp,
        [text(offset) for offset in needed],
        None if rpath is None else text(rpath),
        None if runpath is None else text(runpath),
    )


def _under(path, roots):
    return any(root == "/" or path == root or path.startswith(root + "/") for root in roots)


def _find(data, root, exempt, eb_tmp=False):
    """The first path in ``data`` under ``root`` (an EasyBuild temporary
    directory in it, with ``eb_tmp``) that is not under ``exempt``."""
    needle = root.rstrip(b"/") + (b"/eb-" if eb_tmp else b"")
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            return None
        start = index + 1
        if index > 0 and data[index - 1] in _PATH:
            continue
        end = index + len(needle)
        if eb_tmp:
            name = data[end : end + 8]
            if len(name) < 8 or any(byte not in _TMP_NAME for byte in name):
                continue
            end += 8
        if end < len(data) and data[end] != 0x2F and data[end] in _PATH:
            continue
        while end < len(data) and data[end] in _PATH:
            end += 1
        token = os.fsdecode(data[index:end])
        if not _under(token, exempt):
            return token


def _entries(directory, prefix=""):
    """(rel, path, lstat or None, error or None) for each entry, depth first, names in byte order."""
    try:
        names = sorted(os.listdir(directory), key=os.fsencode)
    except OSError as exc:
        yield prefix.rstrip("/"), directory, None, exc
        return
    for name in names:
        path = os.path.join(directory, name)
        rel = prefix + name
        try:
            info = os.lstat(path)
        except OSError as exc:
            yield rel, path, None, exc
            continue
        yield rel, path, info, None
        if stat.S_ISDIR(info.st_mode):
            yield from _entries(path, rel + "/")


def _loader_dirs(loader):
    """The loader's own system search path, as ``ld.so --help`` prints it."""
    dirs = []
    try:
        done = subprocess.run(
            [loader, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
            timeout=LOADER_TIMEOUT,
            check=False,
        )
        for line in done.stdout.split(b"\n"):
            line = line.strip(b" \t\r")
            if line.endswith(_SYSTEM):
                dirs.append(os.fsdecode(line[: -len(_SYSTEM)].strip(b" \t")))
    except (OSError, subprocess.SubprocessError):
        pass
    return dirs or _beside(loader)


def _beside(loader):
    base = os.path.dirname(loader)
    return [base, os.path.join(os.path.dirname(base), "usr", os.path.basename(base))]


def _directory_acls(path):
    """A directory's ``system.*`` attributes, its access and default ACLs among them."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            names = os.listxattr(fd)
        except OSError as exc:
            if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                return {}
            raise
        return {name: os.getxattr(fd, name) for name in sorted(names) if name.startswith("system.")}
    finally:
        os.close(fd)


def _nearest_dir(path):
    """The closest existing directory above ``path``: where publication creates what is missing."""
    directory = os.path.dirname(path)
    while directory != "/" and not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    return directory


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


class _Stage:
    def __init__(self, journal, trees, environ):
        self.journal = journal
        self.environ = environ
        self.ctx = journal.get("verify_context") or context(journal.get("eb_args") or [], environ)
        self.software = abspath(journal["software"])
        self.generation = abspath(journal["generation_root"])
        self.mods = list(zip(journal.get("modules_plan") or [], trees))
        pairs = []
        for mod, tree in self.mods:
            pairs.append((abspath(mod["staged"]), tree))
            pairs.append((abspath(mod["live_software"]), tree))
        self.pairs = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)
        self.exempt = [
            abspath(journal[key]) for key in ("generation_root", "state_root", "software", "modules")
        ]
        self.eessi = self.ctx.get("eessi")
        self.allowed = [self.generation, self.software] + ([self.eessi] if self.eessi else [])
        self.temporary = list(self.ctx.get("build_dirs") or []) + list(self.ctx.get("tmp_dirs") or [])
        self.loaders = {}
        self.problems = []
        self.objects = 0

    def add(self, check, message):
        self.problems.append((check, message))

    def locate(self, path):
        """Where a path the stage records is now: a run's install paths name its staged trees."""
        for old, new in self.pairs:
            if path == old or path.startswith(old + "/"):
                return new + path[len(old) :]
        return path

    def temporary_root(self, path):
        if not path.startswith("/") or _under(path, self.exempt):
            return None
        for root in self.temporary:
            if _under(path, [root]):
                return root
        return None

    def check(self, lmod):
        for mod, tree in self.mods:
            install = abspath(mod["live_software"])[len(self.software) + 1 :]
            self.tree(install, tree)
            self.module_file(mod)
            if lmod:
                self.load(mod, lmod)
            self.access(install, mod, tree)

    def tree(self, install, tree):
        try:
            top = os.lstat(tree)
        except OSError as exc:
            self.add("access", f"{install} cannot be read: {os.strerror(exc.errno)}")
            return
        try:
            acls = _directory_acls(tree)
        except OSError:
            acls = {}
        policy = acls or self.journal.get("prepare_executable")
        wants = 0 if policy else stat.S_IMODE(top.st_mode) & 0o044
        unreadable = 0
        first = None
        for rel, path, info, error in _entries(tree):
            shown = f"{install}/{rel}" if rel else install
            if error is not None:
                self.add("access", f"{shown} cannot be read: {os.strerror(error.errno)}")
                continue
            if stat.S_ISLNK(info.st_mode):
                self.link(path, shown)
                continue
            if wants and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                need = wants | (wants >> 2 if stat.S_ISDIR(info.st_mode) else 0)
                if stat.S_IMODE(info.st_mode) & need != need:
                    unreadable += 1
                    first = first or shown
            if stat.S_ISREG(info.st_mode) and rel != STAMP:
                self.file(path, rel, shown, info)
        if unreadable:
            self.add(
                "access",
                f"{install}: {unreadable} entries are less readable than the tree's top directory, "
                f"the first {first}",
            )

    def file(self, path, rel, shown, info):
        try:
            with open(path, "rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    handle.seek(0)
                    elf = read_elf(handle)
                    if elf is not None:
                        self.elf(path, shown, elf)
                if rel == "easybuild" or rel.startswith("easybuild/") or info.st_size > SCAN_LIMIT:
                    return
                handle.seek(0)
                data = handle.read()
        except OSError as exc:
            self.add("access", f"{shown} cannot be read: {os.strerror(exc.errno)}")
            return
        for root in self.ctx.get("build_dirs") or []:
            token = _find(data, os.fsencode(root), self.exempt)
            if token:
                self.add("references", f"{shown}: names {token}, in the build directory {root}")
        for root in self.ctx.get("tmp_dirs") or []:
            token = _find(data, os.fsencode(root), self.exempt, eb_tmp=True)
            if token:
                self.add("references", f"{shown}: names {token}, an EasyBuild temporary directory")

    def link(self, path, shown):
        try:
            target = os.readlink(path)
        except OSError:
            return
        root = self.temporary_root(target)
        if root:
            self.add("references", f"{shown}: symbolic link to {target}, under {root}")

    def elf(self, path, shown, elf):
        self.objects += 1
        build = self.ctx.get("build_dirs") or []
        for label, value in (("RPATH", elf.rpath), ("RUNPATH", elf.runpath)):
            for entry in (value or "").split(":"):
                root = self.temporary_root(entry)
                if root and not _under(entry, build):
                    self.add("references", f"{shown}: {label} entry {entry} is under {root}")
        if elf.interp:
            loader = self.locate(elf.interp)
            if not os.path.exists(loader):
                self.add("elf", f"{shown}: loader {elf.interp} not found")
                return
            if self.eessi and not _under(abspath(loader), self.allowed):
                self.add("elf", f"{shown}: loader {elf.interp} is outside the stage, the site prefix and EESSI")
        else:
            name = _LOADERS.get(elf.machine)
            loader = os.path.join(self.ctx.get("eessi_prefix") or "/", name) if name else None
        system = self.system_dirs(loader)
        origin = os.path.dirname(path)
        dirs = []
        for value in (elf.rpath if elf.runpath is None else None, elf.runpath):
            for entry in (value or "").split(":"):
                if entry:
                    dirs.append(self.locate(entry.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)))
        dirs += system
        allowed = self.allowed if self.eessi else self.allowed + [abspath(d) for d in system]
        for name in elf.needed:
            found = None
            if "/" in name:
                candidate = self.locate(name)
                if os.path.exists(candidate):
                    found = candidate
            else:
                for directory in dirs:
                    candidate = os.path.join(directory, name)
                    if os.path.exists(candidate):
                        found = candidate
                        break
            if found is None:
                bare = "" if elf.rpath or elf.runpath else " (no RPATH or RUNPATH)"
                self.add("elf", f"{shown}: {name} not found{bare}")
            elif not _under(abspath(found), allowed):
                self.add("elf", f"{shown}: {name} resolves to {found}, outside the stage, the site prefix and EESSI")

    def system_dirs(self, loader):
        if loader is None:
            return []
        if loader not in self.loaders:
            inside = _under(abspath(loader), [self.generation])
            self.loaders[loader] = _beside(loader) if inside else _loader_dirs(loader)
        return self.loaders[loader]

    def module_file(self, mod):
        try:
            data = read_bytes(mod["prepared"])
        except OSError:
            return
        for root in self.temporary:
            token = _find(data, os.fsencode(root), self.exempt)
            if token:
                self.add("references", f"module file {mod['rel']}: names {token}, under {root}")
                return

    def load(self, mod, lmod):
        rel = mod["rel"]
        parts = rel.split("/")
        name = "/".join(parts[-2:])
        if name.endswith(".lua"):
            name = name[:-4]
        stage = abspath(self.journal["stage_modules"])
        staged = os.path.join(stage, rel)
        path = [os.path.join(stage, *parts[:-2]), os.path.join(abspath(self.journal["modules"]), *parts[:-2])]
        path += [entry for entry in (self.environ.get("MODULEPATH") or "").split(":") if entry]
        env = {
            key: value
            for key, value in self.environ.items()
            if key not in ("LOADEDMODULES", "_LMFILES_")
            and not key.startswith("_ModuleTable")
            and not key.startswith("__LMOD_REF_COUNT")
        }
        env["MODULEPATH"] = ":".join(path)
        env["LMOD_IGNORE_CACHE"] = "1"
        try:
            done = subprocess.run(
                [lmod, "bash", "load", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=env,
                timeout=LMOD_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.add("modules", f"module file {rel}: Lmod did not load {name} within {LMOD_TIMEOUT} seconds")
            return
        except OSError as exc:
            self.add("modules", f"module file {rel}: cannot run {lmod}: {os.strerror(exc.errno)}")
            return
        values = {}
        for line in done.stdout.decode("utf-8", "replace").split("\n"):
            match = _ASSIGN.match(line.strip(" \t\r"))
            if match:
                values[match.group(1)] = _unquote(match.group(2))
        if done.returncode != 0 or "_LMFILES_" not in values:
            said = []
            for part in done.stderr.decode("utf-8", "replace").split("\n"):
                part = part.strip(" \t\r")
                if part:
                    said.append(part)
                elif said:
                    break
            reason = " ".join(said) or f"exit status {done.returncode}"
            self.add("modules", f"module file {rel}: Lmod did not load {name}: {reason}")
            return
        if staged not in values["_LMFILES_"].split(":"):
            self.add("modules", f"module file {rel}: Lmod loaded {values['_LMFILES_']}, not {staged}")
        for key in sorted(values):
            if key.startswith("EBROOT") and not os.path.isdir(self.locate(values[key])):
                self.add("modules", f"module file {rel}: {key} names {values[key]}, which is not a directory")

    def access(self, install, mod, tree):
        euid = os.geteuid()
        live = abspath(mod["live_software"])
        seen = []
        for path in (tree, live):
            try:
                seen.append((os.lstat(path), _directory_acls(path)))
            except OSError as exc:
                self.add("access", f"{install}: cannot read access metadata of {path}: {os.strerror(exc.errno)}")
                return
        (mine, mine_acls), (theirs, their_acls) = seen
        if euid != 0 and mine.st_uid != euid:
            self.add("access", f"{install}: the staged tree belongs to uid {mine.st_uid}, not uid {euid}, which publishes it")
        if not self.journal.get("prepare_executable"):
            if (mine.st_uid, mine.st_gid) != (theirs.st_uid, theirs.st_gid):
                self.add(
                    "access",
                    f"{install}: the staged tree is owned by {mine.st_uid}:{mine.st_gid}, "
                    f"the live tree by {theirs.st_uid}:{theirs.st_gid}",
                )
            if stat.S_IMODE(mine.st_mode) & 0o077 != stat.S_IMODE(theirs.st_mode) & 0o077:
                self.add(
                    "access",
                    f"{install}: the staged tree has mode {stat.S_IMODE(mine.st_mode):04o} and the live tree "
                    f"{stat.S_IMODE(theirs.st_mode):04o}; publishing would change what group and others may do",
                )
            if mine_acls != their_acls:
                self.add("access", f"{install}: the staged tree's ACLs are not the live tree's")
        for entry in (live, abspath(mod["live_module"])):
            directory = _nearest_dir(entry)
            if not os.access(directory, os.W_OK | os.X_OK):
                self.add("access", f"{install}: uid {euid} cannot write {directory}")
                continue
            try:
                parent = os.stat(directory)
                owner = os.lstat(entry).st_uid
            except OSError:
                continue
            if euid != 0 and parent.st_mode & stat.S_ISVTX and euid not in (parent.st_uid, owner):
                self.add("access", f"{install}: {directory} is sticky and {entry} belongs to uid {owner}")


def require_stage_modules(journal, environ=None):
    """Refuse a journal the module check cannot read: with LMOD_CMD set, it loads each module from ``stage_modules``."""
    environ = os.environ if environ is None else environ
    stage = journal.get("stage_modules")
    if environ.get("LMOD_CMD") and (not isinstance(stage, str) or not stage):
        raise NeedsIntervention("journal does not record stage_modules")


def verify_stage(journal, trees, faults, clock, environ=None):
    """Check a built run's staged trees, write ``verify.json`` beside its journal, and return the record."""
    environ = os.environ if environ is None else environ
    lmod = environ.get("LMOD_CMD") or None
    stage = _Stage(journal, trees, environ)
    stage.check(lmod)
    problems = [problem for name in CHECKS for problem in stage.problems if problem[0] == name]
    checks = {name: sum(1 for check, _message in problems if check == name) for name in CHECKS}
    skipped = {}
    if lmod is None:
        checks["modules"] = None
        skipped["modules"] = "LMOD_CMD is not set"
    result = "fail" if problems else "pass"
    at = clock()
    path = os.path.join(run_dir(journal["state_root"], journal["run_id"]), REPORT)
    report = {
        "run_id": journal["run_id"],
        "result": result,
        "at": at,
        "checks": checks,
        "skipped": skipped,
        "elf_objects": stage.objects,
        "problems": [{"check": check, "message": message} for check, message in problems],
    }
    atomic_write(path, json.dumps(report, indent=2, sort_keys=True) + "\n", faults, "verify.report")
    for check, message in problems[:SHOWN]:
        _log(f"verify: {check}: {message}")
    if len(problems) > SHOWN:
        _log(f"verify: {len(problems) - SHOWN} more problem(s) in {path}")
    counts = ", ".join(
        f"{name} skipped ({skipped[name]})" if checks[name] is None else f"{name} {checks[name]}" for name in CHECKS
    )
    _log(f"verify {journal['run_id']}: {result} ({counts}); {stage.objects} ELF object(s); report {path}")
    return {
        "result": result,
        "at": at,
        "checks": checks,
        "skipped": skipped,
        "elf_objects": stage.objects,
        "report": path,
        "forced": False,
    }
