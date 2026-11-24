The Zig state-root lock normalizes `--state-root` before checking symbolic-link components and avoids double slashes in lock error paths, matching the Python `FileLock` behavior.
