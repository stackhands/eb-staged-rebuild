# Moving from the Python implementation

Releases before the retirement of the Python implementation shipped a pure
Python `eb-staged-rebuild` beside the Zig binary. This page records what a
site that ran the Python version should know. The command is now the Zig
binary alone: every command of the Python tool (`probe`, `status`, `list`,
`resolve`, `retire`, `gc`, `run`, `resume`, `verify` and `rollback`, with or
without `--ledger-config`) is in it. The Python package `eb_staged_rebuild`
stays as a thin layer: its wheel carries the binary, and
`python3 -m eb_staged_rebuild` and the console script replace the Python
process with the binary, with the same arguments, exit status and streams.

## The Python parts that stay

`eb_staged_rebuild.site_hooks` (an EasyBuild hook file, which EasyBuild loads as Python) and `eb_staged_rebuild.ship` (a checksum list, run as `python3 -m eb_staged_rebuild.ship`) are Python modules of the package. They are not subcommands of `eb-staged-rebuild`. `site_hooks.py` is also installed by `zig build` under `share/eb-staged-rebuild/`.

The importable Python modules of the old implementation (`eb_staged_rebuild.engine`, `.cli`, `.retire`, and the rest) are gone. The package offers `binary_path()`, `run(*args)` and `run_json(*args)` for code that called the command.

## Where the output differs from the Python tool

| case | Python tool | Zig binary |
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
| the live-tree snapshot a journal records | an in-process function, no command | the hidden helper `__snapshot PATH`, which prints it as JSON; it is not part of the public command line |
| `\d` in `eb-stack ledger append` receipts | any Unicode decimal digit | ASCII digits only |
| an operating-system error with no fixed text in the Zig build | `[Errno N] strerror: 'PATH'` | `ErrorName: 'PATH'` (`[Errno N] ...` for ENOENT, EACCES, EISDIR, ENOTDIR, ELOOP, EEXIST, ENOSPC, EROFS) |
| a failed release of the state-root lock after a refusal or another error under the lock | the release's `OSError` from `lockfile.py:146` or `:153` replaces the first error as it leaves `finally: lock.release()` (`engine.py:1459`, `:1545`, `:1566`, `:1661`, `retire.py:249`, `gc.py:432`), so the message is `[Errno N] strerror` | the first error is reported and the failed release is dropped (`zig/rootlock.zig` `releaseAfterError`); a refusal that exits at once leaves the release to process exit; after a command that succeeded both report the release's `[Errno N] strerror`, exit 2 |
