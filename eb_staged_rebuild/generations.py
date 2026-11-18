"""The generations of each install path: its live tree and the hidden trees beside it.

A publication swaps the staged tree in and keeps the tree it replaced at the
run's hidden sibling (``.<version>.<run-id>``); a rollback swaps them back
and keeps the rolled-back tree there. Each tree names the run that published
it in its run marker. A tree without a marker predates eb-staged-rebuild. A
staged tree that was never published is not a generation. Journal entries
without the paths this needs are skipped.
"""

import os
import time

from eb_staged_rebuild.engine import _tree_run_id
from eb_staged_rebuild.paths import abspath, lstat_kind
from eb_staged_rebuild.retire import is_retire
from eb_staged_rebuild.verify import CHECKS

PUBLISHED = {"complete": "replaced", "rolled_back": "rolled back", "rolling_back": "rolling back"}


def _iso(when):
    return "-" if when is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def _text(value):
    return value if isinstance(value, str) and value else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _plan(journal):
    plan = journal.get("modules_plan")
    return [mod for mod in plan if isinstance(mod, dict)] if isinstance(plan, list) else []


def _paths(mod):
    """A plan entry's live path and hidden sibling, or None when it lacks either."""
    live, retired = _text(mod.get("live_software")), _text(mod.get("retired"))
    return (abspath(live), abspath(retired)) if live and retired else None


def _runs(journals):
    return [journal for journal in journals if not is_retire(journal) and _text(journal.get("software"))]


def _module(journal, live):
    for mod in _plan(journal):
        paths = _paths(mod)
        if paths and paths[0] == live:
            return mod
    return None


def install_paths(journals):
    """Each install path the run journals name, with the hidden siblings they record for it."""
    paths = {}
    for journal in _runs(journals):
        for mod in _plan(journal):
            found = _paths(mod)
            if not found:
                continue
            live, retired = found
            entry = paths.setdefault(
                live, {"install": os.path.relpath(live, abspath(journal["software"])), "hidden": []}
            )
            if retired not in entry["hidden"]:
                entry["hidden"].append(retired)
    return paths


def generations(journals):
    """Install paths by name, each with its generations on disk: the live tree, then newest first."""
    runs = {}
    for journal in _runs(journals):
        if _text(journal.get("run_id")):
            runs[journal["run_id"]] = journal
    found = []
    for live, entry in sorted(install_paths(journals).items(), key=lambda item: (item[1]["install"], item[0])):
        rows = []
        for path in [live] + entry["hidden"]:
            if lstat_kind(path) != "dir":
                continue
            run = _text(_tree_run_id(path))
            source = runs.get(run) if run else None
            mod = _module(source, live) if source else None
            when = _number(mod.get("published_at")) if mod else None
            state = _text(source.get("state")) if source else None
            if path == live:
                state = "live"
            elif run is None:
                state = "original"
            elif mod is None:
                state = "unknown"
            elif state in PUBLISHED and when is not None:
                state = PUBLISHED[state]
            else:
                continue
            rows.append(
                {
                    "path": path,
                    "run": run,
                    "state": state,
                    "published_at": when,
                    "verify": source.get("verify") if source else None,
                }
            )
        rows.sort(
            key=lambda row: (row["state"] != "live", row["published_at"] is None, -(row["published_at"] or 0), row["path"])
        )
        found.append({"install": entry["install"], "live": live, "rows": rows})
    return found


def verify_text(record):
    """The source run's verify result: pass, fail or forced, with its problem count."""
    if not isinstance(record, dict) or not record:
        return "-"
    checks = record.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    if record.get("result") == "pass":
        skipped = [name for name in CHECKS if name in checks and checks[name] is None]
        return "pass" + (f" ({', '.join(skipped)} skipped)" if skipped else "")
    problems = sum(checks[name] for name in CHECKS if isinstance(checks.get(name), int) and not isinstance(checks[name], bool))
    noun = "problem" if problems == 1 else "problems"
    return f"{'forced' if record.get('forced') is True else 'fail'} ({problems} {noun})"


def format_generations(found):
    """The lines ``list`` prints: each install path, then one line for each of its generations."""
    if not found:
        return ["no generations"]
    lines = []
    for group in found:
        lines.append(f"{group['install']}  {group['live']}")
        for row in group["rows"]:
            text = f"  {row['state']:<12} {_iso(row['published_at']):<20}  {row['run'] or '-'}  verify {verify_text(row['verify'])}"
            if row["state"] != "live":
                text += f"  {row['path']}"
            lines.append(text)
    return lines
