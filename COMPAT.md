# The Zig build and the Python tool

The Zig build in `zig/` carries every command of the Python tool: `probe`,
`status`, `resolve`, `retire`, `gc`, `run`, `resume` and `rollback`, with or
without `--ledger-config`. Either build can be used: releases carry the
binary, and CI runs the command-line and real-EasyBuild tests against it
through `TOOL_BIN`.

`tests/differential.sh` runs both on the same invocations and fails on any
difference in exit code, stdout or stderr; for the commands that change a fixture
(`retire`, `gc`, `rollback`, `run`, `resume`) it also compares the journals and
the trees they leave, with timestamps and the sandbox command normalized, and,
for whole runs, the sha256 of module text that names each fixture's own path.

## Where the Zig build differs on purpose

| case | Python tool | Zig build |
|---|---|---|
| `--help` text | wrapped to the terminal width (`COLUMNS`) | the 80-column text |
| `--version` | `eb-staged-rebuild VERSION` | `eb-staged-rebuild VERSION (TARGET, MODE, zig VERSION, COMMIT)`, `COMMIT` from `-Dcommit` (`unknown` without it); the first two words are unchanged |
| usage lines and argparse errors | as the running Python's argparse prints them, which changes between releases | as Python 3.14 prints them |
| a word that starts with `-` and looks like a number, such as `-5` or `-1e3` | a value, not an option, when the running Python's argparse takes it for a negative number: Python 3.12 does for `-5` and `-.5`, not `-1e3` | a value when `-` is followed by a digit or by `.` and a digit, as Python 3.14 decides |
| a module file or journal that is not UTF-8 | a Python traceback, exit 1 | `eb-staged-rebuild: PATH is not UTF-8 text`, exit 2 for `resolve`; `journal cannot be read: PATH: not UTF-8 text`, exit 2 for `status` |
| a journal that is not valid JSON | `journal cannot be read: PATH: ` and the JSON decoder's message | the same start, then `not valid JSON` |
| a journal value that `status` cannot format (for example a `null` status) | a Python traceback, exit 1 | `Traceback: TypeError: ...` on stderr, exit 1 |
| program output that is not UTF-8 (bwrap, eb) | a decoding error | decoded with U+FFFD for invalid bytes |
| `--lock-timeout` and `--grace-hours` | Python `float()`, which also takes underscores and non-ASCII digits | ASCII decimal and exponent forms |
| a lock wait longer than about 49 days | waits that long | capped at 2^32 ms |
| the user-namespace probe | `unshare ... python3 -m eb_staged_rebuild.userns_sandbox --probe` | `unshare ... <this binary> __userns-sandbox --probe` |
| `\d` in `eb-stack ledger append` receipts | any Unicode decimal digit | ASCII digits only |
| an operating-system error with no fixed text in the Zig build | `[Errno N] strerror: 'PATH'` | `ErrorName: 'PATH'` (`[Errno N] ...` for ENOENT, EACCES, EISDIR, ENOTDIR, ELOOP, EEXIST, ENOSPC, EROFS) |
