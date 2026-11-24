"""Retire a module file whose package is published elsewhere.

``retire`` moves one module file out of the module tree and into
``<state-root>/retired/<timestamp>/<absolute path without the leading />``
with one same-filesystem ``os.replace``. The software directory the module
named is not touched. A job that already loaded the module keeps its paths. A
job that loads the module afterwards no longer finds it in this tree and falls
through to the next entry on MODULEPATH.

Symbolic links that resolve to the module file, such as EasyBuild's module
class links, move with it, first, so that none is left dangling.

The record is a journal under ``<state-root>/runs/retire-<timestamp>/`` in the
same format as a run journal (``kind`` is ``retire``, ``modules_plan`` is
empty, and ``links_plan`` lists the links). ``rollback RUN_ID`` puts the file
back, then the links. ``gc`` reads the record to decide when the software
directory is ready to remove.
"""

import errno
import os
import time

from eb_staged_rebuild.atomicio import fsync_dir, read_bytes, sha256_bytes
from eb_staged_rebuild.errors import NeedsIntervention, Refused
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.journal import journal_path, list_journals, run_dir, save_journal
from eb_staged_rebuild.lockfile import FileLock
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import abspath, is_real_dir, is_within, lstat_kind
from eb_staged_rebuild.usage_log import check_prefix

KIND = "retire"


def is_retire(journal):
    """Return whether a journal is a retire journal."""
    return journal.get("kind") == KIND


def retired_dir(state_root):
    """Return ``STATE_ROOT/retired``, where ``retire`` moves module files."""
    return os.path.join(abspath(state_root), "retired")


def _stamp(now):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _log(message):
    print(f"eb-staged-rebuild: {message}", flush=True)


def _already_retired(state_root, module):
    for journal in list_journals(state_root):
        if not is_retire(journal):
            continue
        if journal.get("module_path") == module and journal.get("state") in ("retiring", "retired"):
            return journal
    return None


def _unique(state_root, now):
    stamp = _stamp(now)
    candidate = stamp
    count = 1
    while os.path.lexists(os.path.join(retired_dir(state_root), candidate)) or os.path.lexists(
        run_dir(state_root, f"retire-{candidate}")
    ):
        count += 1
        candidate = f"{stamp}-{count}"
    return candidate


def _check_same_filesystem(source_parent, destination_parent, message):
    if os.stat(source_parent).st_dev != os.stat(destination_parent).st_dev:
        raise Refused(message)


def _links_to(module):
    """Symbolic links that resolve to ``module``, sorted.

    They are looked for in the module file's directory and, for each ``all``
    directory above it, in the same place under each sibling of that
    directory: EasyBuild puts a module class link at
    ``<modules>/<class>/<name>/...`` beside ``<modules>/all/<name>/...``.
    """
    real = os.path.realpath(module)
    directories = [os.path.dirname(module)]
    parts = os.path.dirname(module).split(os.sep)
    for index, part in enumerate(parts):
        if part != "all":
            continue
        top = os.sep.join(parts[:index]) or os.sep
        try:
            names = sorted(os.listdir(top))
        except OSError:
            continue
        for name in names:
            candidate = os.path.join(top, name, *parts[index + 1 :])
            if name != "all" and is_real_dir(os.path.join(top, name)) and is_real_dir(candidate):
                directories.append(candidate)
    links = set()
    for directory in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if os.path.islink(path) and os.path.realpath(path) == real:
                links.add(path)
    return sorted(links)


def _plan(state_root, module):
    module = abspath(module)
    try:
        module.encode("utf-8")
    except UnicodeError as exc:
        raise Refused("module path is not UTF-8 text") from exc
    if is_within(module, state_root):
        raise Refused(f"{module} is inside the state root")
    previous = _already_retired(state_root, module)
    if previous is not None:
        raise Refused(f"{module} is already retired ({previous['run_id']})")
    kind = lstat_kind(module)
    if kind != "file":
        raise Refused(f"{module} is not a regular file ({kind})")
    data = read_bytes(module)
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise Refused(f"{module} is not UTF-8 text") from exc
    _syntax, root = parse_root(text)
    if not root:
        raise Refused(f"{module} has no root assignment, so its software directory is unknown")
    return module, data, abspath(root)


def retire(
    state_root,
    module_file,
    reason=None,
    dry_run=False,
    faults=None,
    clock=None,
    lock_timeout=0,
    usage_log=None,
    eb_keep_logs="eb-keep-logs",
):
    """Move ``module_file`` into the state root and journal it. Returns the journal."""
    from eb_staged_rebuild.engine import Result

    state_root = abspath(state_root)
    faults = faults or FaultInjector()
    now = time.time if clock is None else clock
    if dry_run:
        module, _data, software_dir = _plan(state_root, module_file)
        in_use, detail = check_prefix(software_dir, usage_log, eb_keep_logs)
        if in_use:
            reason = f"refusing to retire {module}: usage log reports paths under {software_dir}"
            if detail:
                reason = f"{reason}: {detail.splitlines()[0]}"
            raise Refused(reason)
        for link in _links_to(module):
            _log(f"would retire the link {link}")
        message = f"would retire {module} (software directory {software_dir} is left in place)"
        _log(message)
        return Result(0, None, "dry-run", message)
    lock = FileLock(state_root)
    lock.acquire(lock_timeout)
    try:
        module, data, software_dir = _plan(state_root, module_file)
        in_use, detail = check_prefix(software_dir, usage_log, eb_keep_logs)
        if in_use:
            reason = f"refusing to retire {module}: usage log reports paths under {software_dir}"
            if detail:
                reason = f"{reason}: {detail.splitlines()[0]}"
            raise Refused(reason)
        when = now()
        name = _unique(state_root, when)
        run_id = f"retire-{name}"
        top = os.path.join(retired_dir(state_root), name)
        destination = os.path.join(top, module.lstrip(os.sep))
        links = [
            {
                "live_path": link,
                "target": os.readlink(link),
                "retired_path": os.path.join(top, link.lstrip(os.sep)),
                "status": "pending",
            }
            for link in _links_to(module)
        ]
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            _check_same_filesystem(
                os.path.dirname(module),
                os.path.dirname(destination),
                f"{module} and the state root are on different filesystems; "
                "retire needs one atomic rename, so put --state-root on the same filesystem as the module tree",
            )
            for link in links:
                os.makedirs(os.path.dirname(link["retired_path"]), exist_ok=True)
                _check_same_filesystem(
                    os.path.dirname(link["live_path"]),
                    os.path.dirname(link["retired_path"]),
                    f"{link['live_path']} and the state root are on different filesystems; "
                    "retire needs one atomic rename, so put --state-root on the same filesystem as the module tree",
                )
        except Refused:
            _discard_empty(top)
            raise
        path = journal_path(state_root, run_id)
        journal = {
            "schema": 1,
            "kind": KIND,
            "run_id": run_id,
            "state": "retiring",
            "state_root": state_root,
            "module_path": module,
            "module_hash": sha256_bytes(data),
            "software_dir": software_dir,
            "retired_path": destination,
            "reason": reason or "",
            "created_at": when,
            "updated_at": when,
            "retired_at": None,
            "rolled_back_at": None,
            "modules_plan": [],
            "links_plan": links,
            "message": "",
        }
        save_journal(path, journal, faults, "journal.retiring")
        for number, link in enumerate(links, 1):
            _rename(link["live_path"], link["retired_path"], faults, f"retire.link:{number}")
            link["status"] = "retired"
            _log(f"retired the link {link['live_path']} -> {link['retired_path']}")
        _move(module, destination, faults, "retire")
        journal["state"] = "retired"
        journal["retired_at"] = now()
        journal["updated_at"] = journal["retired_at"]
        journal["message"] = "module file retired"
        save_journal(path, journal, faults, "journal.retired")
        message = f"retired {module} -> {destination}; software directory {software_dir} left in place"
        _log(message)
        return Result(0, run_id, "retired", message)
    finally:
        lock.release()


def _list_dirs(top):
    """Names of real subdirectories of ``top``.

    An interrupted read is tried again. Any other error is raised. A symlink
    is not a subdirectory. An entry whose type cannot be read is not one either.
    """
    while True:
        try:
            with os.scandir(top) as scan:
                names = []
                for entry in scan:
                    while True:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            break
                        except OSError as exc:
                            if exc.errno == errno.EINTR:
                                continue
                            is_dir = False
                            break
                    if is_dir:
                        names.append(entry.name)
            return names
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise


def _discard_empty(top):
    """Remove ``top`` and empty directories under it.

    An interrupted listing is tried again, so one signal does not leave the
    directory behind. When the listing fails for another reason the directory
    is removed if it is empty, and the walk does not descend.
    """
    try:
        names = _list_dirs(top)
    except OSError:
        try:
            os.rmdir(top)
        except OSError:
            pass
        return
    for name in names:
        _discard_empty(os.path.join(top, name))
    try:
        os.rmdir(top)
    except OSError:
        pass


def _move(source, destination, faults, step):
    """Rename ``source`` onto ``destination`` after the file bytes are durable."""
    faults.reach(f"{step}.fsync")
    fd = os.open(source, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _rename(source, destination, faults, f"{step}.rename")


def _rename(source, destination, faults, step):
    faults.reach(step)
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise Refused(f"{source} and {destination} are on different filesystems") from exc
        raise
    fsync_dir(os.path.dirname(destination))
    fsync_dir(os.path.dirname(source))


def _restore_link(link, faults, step, run_id):
    live = link["live_path"]
    retired = link["retired_path"]
    if lstat_kind(retired) == "symlink":
        if os.readlink(retired) != link["target"]:
            raise NeedsIntervention(f"{retired} does not match the link recorded for {run_id}")
        if lstat_kind(live) != "missing":
            raise NeedsIntervention(f"{live} exists again; refusing to overwrite it with the retired link")
        _rename(retired, live, faults, step)
        _log(f"restored the link {live}")
    elif not (lstat_kind(live) == "symlink" and os.readlink(live) == link["target"]):
        raise NeedsIntervention(f"neither {live} nor {retired} holds the link recorded for {run_id}")
    link["status"] = "restored"


def rollback_retire(state_root, journal, faults, clock):
    """Put a retired module file back and journal it. Caller holds the lock."""
    from eb_staged_rebuild.engine import Result

    run_id = journal["run_id"]
    path = journal_path(state_root, run_id)
    if journal["state"] == "rolled_back":
        message = f"run {run_id} is already rolled back"
        _log(message)
        return Result(0, run_id, "rolled_back", message)
    module = journal["module_path"]
    retired = journal["retired_path"]
    live_kind = lstat_kind(module)
    retired_kind = lstat_kind(retired)
    if retired_kind == "file":
        if sha256_bytes(read_bytes(retired)) != journal["module_hash"]:
            raise NeedsIntervention(f"{retired} does not match the module file recorded for {run_id}")
        if live_kind != "missing":
            raise NeedsIntervention(f"{module} exists again; refusing to overwrite it with the retired copy")
        _move(retired, module, faults, "rollback.retire")
        _log(f"restored {module}")
    elif live_kind == "file" and sha256_bytes(read_bytes(module)) == journal["module_hash"]:
        pass
    else:
        raise NeedsIntervention(f"neither {module} nor {retired} holds the retired module file")
    links = journal.get("links_plan") or []
    for number in range(len(links), 0, -1):
        _restore_link(links[number - 1], faults, f"rollback.retire.link:{number}", run_id)
    journal["state"] = "rolled_back"
    journal["rolled_back_at"] = clock()
    journal["updated_at"] = journal["rolled_back_at"]
    journal["message"] = "rolled back"
    save_journal(path, journal, faults, "journal.rolled_back")
    message = f"run {run_id} rolled back; {module} restored"
    _log(message)
    return Result(0, run_id, "rolled_back", message)
