"""Crash-safe run journal. Readers only ever see a fully written JSON file."""

import json
import os

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import NeedsIntervention, Refused

# Schema 2 records a directory swap: stable live path, staged path, retired sibling.
# Schema 1 is the generation-pointer journal this line refuses to interpret.
# Schema 3 is that unmerged line's ledger journal. It is not a directory swap.
# Schema 4 is a directory-swap journal whose ledger policy is frozen.
SCHEMA = 2
LEDGER_SCHEMA = 4
GENERATION_POINTER_SCHEMAS = (1, 3)


def plain_run_id(run_id):
    """Return whether ``run_id`` is one non-special path component."""
    return (
        isinstance(run_id, str)
        and run_id not in ("", ".", "..")
        and "/" not in run_id
        and "\0" not in run_id
    )


def require_plain_run_id(run_id):
    """Return a safe run id, or refuse a value that can leave its state directory."""
    if not plain_run_id(run_id):
        raise Refused(f"run id is not a plain name: {run_id!r}")
    return run_id


def require_matching_run_id(data, run_id):
    """Require a journal to name the directory from which it was read."""
    if not plain_run_id(data.get("run_id")) or data["run_id"] != run_id:
        raise NeedsIntervention("journal run id does not match its directory")


def journal_path(state_root, run_id):
    """Return ``STATE_ROOT/runs/RUN_ID/journal.json``."""
    return os.path.join(os.fspath(state_root), "runs", run_id, "journal.json")


def run_dir(state_root, run_id):
    """Return ``STATE_ROOT/runs/RUN_ID``, the directory of a run's journal and outbox."""
    return os.path.join(os.fspath(state_root), "runs", run_id)


def generation_root(state_root, run_id):
    """Return ``STATE_ROOT/generations/RUN_ID``, where a run's build installs."""
    return os.path.join(os.fspath(state_root), "generations", run_id)


def save_journal(path, data, faults, step):
    """Write a journal atomically, with its schema and publication set.

    A journal that does not record a directory swap needs intervention and is not written.
    """
    if data.get("kind") == "retire":
        data["schema"] = SCHEMA
    elif data.get("ledger_policy"):
        data["schema"] = LEDGER_SCHEMA
        data["publication"] = "directory-swap"
    else:
        data["schema"] = SCHEMA
        if data.get("publication") is None:
            data["publication"] = "directory-swap"
    _require_directory_swap(data)
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(path, raw, faults, step)


def _require_directory_swap(data):
    publication = data.get("publication")
    if publication not in (None, "directory-swap"):
        raise NeedsIntervention(
            f"journal publication {publication!r} is not a directory swap"
        )
    if data.get("kind") == "retire":
        return
    plan = data.get("modules_plan")
    if not plan:
        return
    if not isinstance(plan, list):
        raise NeedsIntervention("journal modules_plan is not a list")
    for mod in plan:
        if not isinstance(mod, dict):
            raise NeedsIntervention("journal modules_plan entry is not an object")
        if "live_software" not in mod or "retired" not in mod:
            raise NeedsIntervention(
                "journal does not record a directory swap; "
                "a generation-pointer plan is not applied"
            )


def load_journal(path):
    """Read a journal.

    A path that is not a regular file is refused. A journal that cannot be read, or whose
    schema this program does not apply, needs intervention.
    """
    if not os.path.isfile(path) or os.path.islink(path):
        raise Refused(f"no journal at {path}")
    try:
        data = json.loads(read_bytes(path).decode("utf-8"))
    except OSError as exc:
        raise NeedsIntervention(f"journal cannot be read: {path}: {exc}") from exc
    except UnicodeError as exc:
        raise NeedsIntervention(f"journal cannot be read: {path}: not UTF-8 text") from exc
    except json.JSONDecodeError as exc:
        raise NeedsIntervention(f"journal cannot be read: {path}: not valid JSON") from exc
    if not isinstance(data, dict):
        raise NeedsIntervention(f"journal is not a JSON object: {path}")
    schema = data.get("schema")
    if schema in GENERATION_POINTER_SCHEMAS:
        raise NeedsIntervention(
            f"journal schema {schema} is a generation-pointer record"
            + (" and carries that line's ledger policy" if schema == 3 else "")
            + "; this program publishes by directory swap and will not apply it"
        )
    if schema == LEDGER_SCHEMA:
        if data.get("publication") != "directory-swap" or not isinstance(data.get("ledger_policy"), dict):
            raise NeedsIntervention(
                "schema 4 journal is not a frozen directory-swap ledger record"
            )
    elif schema != SCHEMA:
        raise NeedsIntervention(f"journal schema {schema!r} is not {SCHEMA} or {LEDGER_SCHEMA}")
    _require_directory_swap(data)
    return data


# What a rebuild journal has to record before a command reads it.
RUN_FIELDS = ("software", "modules", "generation_root", "state", "backend")
PLAN_FIELDS = ("rel", "live_module", "prepared", "staged", "live_software", "retired")


def load_run_journal(state_root, run_id):
    """Return the path and journal of run RUN_ID under the caller's STATE_ROOT, checked.

    The path is made from STATE_ROOT and the plain name RUN_ID alone; see
    ``check_run_journal`` for what the journal has to record.
    """
    require_plain_run_id(run_id)
    path = journal_path(state_root, run_id)
    data = load_journal(path)
    check_run_journal(run_id, data)
    return path, data


def check_run_journal(run_id, data):
    """Require a journal read from ``runs/RUN_ID`` to be one a command may act on.

    It has to name that run and record its state root. A rebuild journal also
    has to record, as non-empty text, each field of RUN_FIELDS and each field
    of PLAN_FIELDS in every module plan entry, so no command reads a field that
    is missing.
    """
    require_matching_run_id(data, run_id)
    _recorded(data, "state_root")
    if data.get("kind") == "retire":
        return
    for field in RUN_FIELDS:
        _recorded(data, field)
    plan = data.get("modules_plan")
    if not isinstance(plan, list):
        raise NeedsIntervention("journal does not record modules_plan")
    for mod in plan:
        for field in PLAN_FIELDS:
            value = mod.get(field)
            if not isinstance(value, str) or not value:
                raise NeedsIntervention(f"journal module plan does not record {field}")


def _recorded(data, field):
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise NeedsIntervention(f"journal does not record {field}")
    return value


def list_journals(state_root):
    """Return the journals under ``STATE_ROOT/runs``, in name order.

    An entry without a regular ``journal.json`` is left out.
    """
    runs = os.path.join(os.fspath(state_root), "runs")
    found = []
    if not os.path.isdir(runs):
        return found
    for name in sorted(os.listdir(runs)):
        path = os.path.join(runs, name, "journal.json")
        if os.path.isfile(path) and not os.path.islink(path):
            found.append(load_journal(path))
    return found
