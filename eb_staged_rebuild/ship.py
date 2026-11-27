"""Checksum list of an install tree, for a node that does not share the disk.

This is not a software bill of materials and not a signed build cache. It
records the bytes of regular files and the text of symbolic links, so a copy
can be checked. Directories are not listed. The command does nothing until
someone runs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _row_path(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if ".." in Path(rel).parts or rel.startswith("/"):
        raise ValueError(f"path escapes the tree: {rel}")
    if "\n" in rel or "\x00" in rel:
        raise ValueError(f"path has a newline: {rel}")
    return rel


def _inside(root: Path, path: Path):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def _walk_error(error):
    raise error


def snapshot(root, skip=None):
    """File and link rows under root, sorted by relative path. skip is not hashed.

    A directory that cannot be listed raises OSError. Skipping it would omit
    its files from the list and from the check.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        current = Path(dirpath)
        linked = []
        for name in dirnames:
            path = current / name
            if path.is_symlink():
                linked.append(name)
                rows.append(_link(root, path))
        for name in linked:
            dirnames.remove(name)
        for name in filenames:
            path = current / name
            rel = _row_path(root, path)
            if skip and rel == skip:
                continue
            if path.is_symlink():
                rows.append(_link(root, path))
            elif path.is_file():
                rows.append(_file(root, path))
            else:
                raise ValueError(f"not a file or a link: {rel}")
    rows.sort(key=lambda row: row["path"])
    return rows


def _file(root: Path, path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"kind": "file", "path": _row_path(root, path), "sha256": digest.hexdigest()}


def _link(root: Path, path: Path):
    target = os.readlink(path)
    rel = _row_path(root, path)
    if "\n" in target or "\x00" in target:
        raise ValueError(f"link target has a newline: {rel}")
    return {"kind": "link", "path": rel, "target": target}


def write_manifest(root, dest) -> None:
    root = Path(root)
    dest = Path(dest)
    rows = snapshot(root, skip=_inside(root, dest))
    lines = [json.dumps({"kind": "header", "version": 1}, sort_keys=True)]
    lines.extend(json.dumps(row, sort_keys=True) for row in rows)
    dest.write_text("\n".join(lines) + "\n")


def verify(root, manifest):
    """Problems, empty when the tree matches the list. The list itself is skipped."""
    root = Path(root)
    manifest = Path(manifest)
    got = {row["path"]: row for row in snapshot(root, skip=_inside(root, manifest))}
    want = {}
    saw_header = False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "header":
            if row.get("version") != 1:
                return [f"version {row.get('version')}"]
            saw_header = True
            continue
        path = row.get("path")
        if not isinstance(path, str):
            return ["bad path"]
        want[path] = row
    if not saw_header:
        return ["no header"]
    problems = []
    for path in sorted(set(want) | set(got)):
        if path not in got:
            problems.append(f"missing {path}")
        elif path not in want:
            problems.append(f"extra {path}")
        else:
            left, right = want[path], got[path]
            if left.get("kind") != right.get("kind"):
                problems.append(f"kind {path}")
            elif left.get("kind") == "file" and left.get("sha256") != right.get("sha256"):
                problems.append(f"sha256 {path}")
            elif left.get("kind") == "link" and left.get("target") != right.get("target"):
                problems.append(f"link {path}")
    return problems


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] not in ("write", "verify"):
        sys.stderr.write("usage: python3 -m eb_staged_rebuild.ship write or verify ROOT MANIFEST\n")
        return 2
    command, root, manifest = argv
    try:
        if command == "write":
            write_manifest(root, manifest)
            sys.stdout.write(f"wrote {manifest}\n")
            return 0
        problems = verify(root, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"eb-staged-rebuild ship: {exc}\n")
        return 2
    if problems:
        sys.stderr.write("".join(f"eb-staged-rebuild ship: {item}\n" for item in problems))
        return 1
    sys.stdout.write("matches\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
