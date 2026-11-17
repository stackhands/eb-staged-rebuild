"""Prometheus metrics for a completed garbage-collection pass."""

import json
import math
import os

from eb_staged_rebuild.atomicio import atomic_write, read_bytes
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import lstat_kind


def _generation_names(state_root):
    """Return the real generation directories directly below ``state_root``."""
    directory = os.path.join(os.fspath(state_root), "generations")
    try:
        with os.scandir(directory) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return []


def _tree_run_id(path):
    """Return the run stamped into a published tree, as publication recovery does."""
    stamp = os.path.join(path, ".eb-staged-rebuild-complete")
    if lstat_kind(stamp) != "file":
        return None
    try:
        value = json.loads(read_bytes(stamp).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value.get("run_id") if isinstance(value, dict) else None


def _is_live_publication(mod):
    """Return whether the journal entry's module still publishes its software."""
    module = mod.get("live_module") or ""
    software = mod.get("live_software") or ""
    if lstat_kind(module) != "file" or not software:
        return False
    try:
        _syntax, root = parse_root(read_bytes(module).decode("utf-8"))
    except (OSError, UnicodeError):
        return False
    return bool(root) and os.path.abspath(root) == os.path.abspath(software)


def _current_created_at(journals, now):
    """Return the creation time of the newest generation still published live."""
    current = []
    for journal in journals:
        run_id = journal.get("run_id")
        value = journal.get("created_at")
        if (
            journal.get("state") != "complete"
            or not isinstance(run_id, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= now
        ):
            continue
        for mod in journal.get("modules_plan") or []:
            if (
                mod.get("status") == "published"
                and _is_live_publication(mod)
                and _tree_run_id(mod.get("live_software") or "") == run_id
            ):
                current.append(float(value))
                break
    return max(current) if current else None


def render_metrics(state_root, journals, now):
    """Render values known after gc, identifying current generations from live stamps."""
    names = _generation_names(state_root)
    current_created_at = _current_created_at(journals, now)

    lines = [
        "# HELP eb_staged_rebuild_generations_present Real generation directories present after garbage collection.",
        "# TYPE eb_staged_rebuild_generations_present gauge",
        f"eb_staged_rebuild_generations_present {len(names)}",
        "# HELP eb_staged_rebuild_current_generation_age_seconds Age of the newest generation that remains published live.",
        "# TYPE eb_staged_rebuild_current_generation_age_seconds gauge",
    ]
    if current_created_at is not None:
        lines.append(f"eb_staged_rebuild_current_generation_age_seconds {now - current_created_at:.17g}")
    lines.extend(
        [
            "# HELP eb_staged_rebuild_last_gc_timestamp_seconds Unix timestamp of the last completed garbage-collection pass.",
            "# TYPE eb_staged_rebuild_last_gc_timestamp_seconds gauge",
            f"eb_staged_rebuild_last_gc_timestamp_seconds {now:.17g}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_metrics(path, state_root, journals, now):
    """Atomically replace ``path`` with metrics from a completed gc pass."""
    if not path:
        raise Refused("--metrics-file must name a file; it is empty")
    atomic_write(path, render_metrics(state_root, journals, now), None, "metrics")
