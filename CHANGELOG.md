# Changelog

<!-- towncrier release notes start -->

## 3.0.1 (2026-11-27)

### Fixes

- The release workflow set up neither user namespaces nor the packages and tools the tests need, and it ran only some of the test commands. 3.0.0 therefore failed in its build job and published no GitHub release. The workflow now builds and tests in the environment the `tests` workflow uses, and builds the wheels with cibuildwheel as that workflow does. 3.0.1 is the first release of the changes in 3.0.0, with the binaries, the wheels, the `share/` archive and `SHA256SUMS`.


## 3.0.0 (2026-11-27)

### Features

- Add `verify STAGE`, which checks a built run's staged trees without publishing them and lists each problem in `runs/<run-id>/verify.json`. `run --verify` and `resume --verify` run the same checks before the swap. A failing stage is refused unless `--force` is given. The Python command and the Zig binary have the same checks, the same options and the same `verify.json`.
- The wheels declare free-threading support and carry a C extension, `eb_staged_rebuild._launch`, built against the CPython stable ABI, that calls `execv(2)` for the launcher. A `cp311-abi3` wheel serves every CPython from 3.11 that has the GIL; the free-threaded 3.13t and 3.14t, which install no abi3 wheel, get a `cp313t` and a `cp314t` wheel. cibuildwheel builds each in the manylinux2014 image for x86_64 and aarch64, and `tools/check_wheel.py` checks the tag, the single `_launch` extension, the bundled binary and the free-threading classifier. CI imports `_launch` on 3.13t and 3.14t and checks the GIL stays off, and calls it from many threads.
- With `--ledger-config`, a run that `--force` published past the failed checks of `--verify` says so in its ledger records: its publication records carry `build.verify` with the check result, the `forced` flag and the problem count of each check. The records of a run that was not checked or that passed (and the corrections a rollback appends) are unchanged.
- `gc --keep N` keeps each install path's `N` newest generations that `rollback --to` can switch back to, however old they are. Older trees are removed past their grace period as before. With `--check-in-use`, `gc` scans `/proc` before it removes a tree or a run's generation directory, with or without `--dry-run`. It looks for mounts at or inside the path, including bind mounts in any mount namespace, and for processes using it through an executable, a working or root directory, or open or mapped files. It keeps any tree in use and names the mount or process. Without `--check-in-use`, `gc` reads nothing in `/proc`, as before. A tree that `--keep` or this check keeps leaves its run's generation directory in place too. The scan does not see other nodes or other users' processes without root, and the grace period still protects those.
- `list` shows, for each install path the state root's journals name, the generations still on disk: the live tree, then the trees publications replaced and rollbacks took out, most recently published first, each with the publishing run and timestamp plus the result of any recorded stage checks. Its `--state-root` falls back to `EB_STAGED_REBUILD_STATE_ROOT`, as for the other commands.
- `rollback --latest` undoes the newest complete run, and `rollback --to GEN` switches each of a generation's install paths straight back to it, in one pass that a second call finishes after a kill. `rollback` without a run id or one of `--latest` and `--to` is still a usage error. `list` shows the trees of an unfinished pass as `rolling back`.

### Fixes

- A failed directory flush names that directory from either frontend.
- A parent flush that fails after a successful `RENAME_EXCHANGE` now
  flushes that parent again once the directories are exchanged back.
  A crash after the command reports failure no longer leaves the
  exchanged names. A failed undo reports that failure.
- A retire stays unrecorded when a directory flush fails after the rename.
- A retry that finds a created module file already removed flushes its directory, so `rollback --to` and publish recovery do not record the removal before it is durable.
- A staged tree renamed across directories is flushed in both parent directories.
- A state root that begins with exactly two slashes keeps them in the Zig state-root lock error. Python does the same, and both exit 2 with the same message.
- Live module reads use one reader with an explicit failure mode, and failures while opening previous module bytes keep Python-matching operating-system error text.
- Restoring the old live tree after an interrupted publish flushes the class-link directory before it records a created link that another entry replaced as pending, and keeps that entry in place.
- Resuming after a failed directory flush flushes that directory again before the run is recorded as published.
- Retrying `rollback --to` now flushes the class-link directory when an earlier attempt already removed the link.
- The Zig `rollback --to` reports a class link it cannot remove and keeps that run rolling back, so running it again removes the link.
- The Zig `rollback --to` reports a failed flush of the directory that held a removed class link and keeps that run rolling back, so running it again finishes the rollback.
- The Zig build reports a failed release of the state-root lock, as the
  Python tool does. If `fcntl` or `flock` fails to release the lock at the
  end of `run`, `resume`, `rollback`, `retire`, `verify` or `gc`, the
  command exits 2 with `eb-staged-rebuild: [Errno N] strerror`. Before, it
  dropped the error and exited with its own status. In that case `gc` prints
  no actions, because Python prints them only after it releases the lock.
  After a refusal under the lock the binary keeps the refusal and drops the
  failed release; COMPAT.md lists the difference. The build pins zig-common
  `a45142ad` on `develop`, where `state.RootLock.release` returns the error.
- The Zig command now escapes U+00A0 and U+00AD in path representations as
  `\xa0` and `\xad`, matching Python's `repr`.
- The `userns` backend works on an install path, state root or `TMPDIR` on a file system mounted `nosuid`, `nodev`, `noexec` or with an atime option, such as `/dev/shm` or many scratch file systems. A user namespace locks those flags, and the read-only remount of the sandbox left them out, so it failed with `Operation not permitted` and `probe` reported the sandbox unusable. The remount now carries them over, as bwrap does.
- The build subprocess applies `EASYBUILD_UMASK`, so `verify` sees the expected staged-tree access mode.
- The journal keeps a staged tree, created file, created link or generation when its directory flush fails.
- The ledger outbox of a run is read and written under the state root the command was given, in both front ends, rather than under the one the journal records.
- The release publishes the completions and man page as `eb-staged-rebuild-VERSION-share.tar.gz`; a release asset cannot be a directory, so the 2.0.1 publish step failed.
- Uses a complete run id in the docs rollback-to walkthrough.
- `generation_complete` treats an unreadable `easybuild` metadata directory as incomplete instead of raising `OSError`, so `resolve` emits its JSON document with exit 1 and `run` marks the journal failed in line with the Zig binary.
- `rollback --to` now flushes the class-link directory before it records a link that another entry replaced as removed, and keeps that entry in place.
- `rollback RUN_ID`, `rollback --latest` and `rollback --to GEN` read journals through the run journal loader in Python and Zig. `--latest` and `--to` refuse every run if any journal under `runs/` names another run, names another state root, or lacks a field. They refuse before they choose runs or print anything. `--to` reads `GEN` at `runs/GEN/journal.json`.
- `rollback` flushes the class-link directory before it records a link that another entry replaced as removed, and keeps that entry in place.
- `verify` and `resume` read a run journal through one loader in Python and Zig. They refuse a run id that is not a plain name, a journal that names another run, and a journal without a field the commands read. The fields are `state_root`, `software`, `modules`, `generation_root`, `state`, `backend`, `modules_plan`, and each plan entry's `rel`, `live_module`, `prepared`, `staged`, `live_software` and `retired`. The refusal exits 2 with the same message, before anything is written.
- `verify` and `resume` refuse a journal whose `state_root` is not the state root they were given. The refusal exits 2 with the same message in both front ends. Both commands write `verify.json` and resume under the given state root and ignore the one the journal records.

### Documentation

- Add `--verify` and `--force` to the `resume` line of the command summary in the CLI reference. The section for `resume` and its option table already list them.
- Document stage verification and the publish check in a how-to guide and a reference page, and add a `verify` case to the docs example.
- The README, install how-to and COMPAT.md say that the site hook file and the checksum list for a copied install exist only in the Python package.
- The install guide and README name release 2.0.1, with its checksum, attestation and version output.

### Removals

- The Python implementation of the commands is gone. The Zig binary is the only implementation, and the `eb_staged_rebuild` package is a thin layer over it: the wheel carries the binary, `python3 -m eb_staged_rebuild` and the console script replace the Python process with it, and `binary_path()`, `run()` and `run_json()` give Python code the binary and its `--json` documents. `eb_staged_rebuild.site_hooks` and `eb_staged_rebuild.ship` stay Python. The importable modules of the old implementation (`engine`, `cli`, `retire` and the rest) are removed. The tests that compared the two implementations now compare the binary with recorded files under `tests/golden/`, and the release carries completions and a man page from `share/`. COMPAT.md records what a site that ran the Python version should know.

### Miscellaneous

- Add differential and matrix tests for `verify`, `run --verify` and `--force`, and run `tests.test_verify_cli` against the release binary in CI.
- Both userns helper spellings become one token before the dry-run path-byte comparison.
- CI runs the ready-unlink parity test against the Zig test artifact.
- EB_STAGED_REBUILD_NOW is read only by the test artifact and tests/now_cli.py, and only a timestamp the run id can record replaces the wall clock.
- Each EasyBuild integration run builds under its own temporary directory.
- Fault-injection tests share helpers for skip policy, Python 3.14 selection, ptrace checks, per-test tracer builds, and Filesystem in Userspace mount probes, removing duplicated setup code across parity suites.
- The Zig `run` command sets `EASYBUILD_UMASK` in the build command through one helper. It leaves its own umask alone, as the Python command does.
- The Zig build now takes repr and errno-path formatting from zig-common
  `pytext`, including the punch-hole refusal and the test clock refusal
  of the replaceable build. The rendered text does not change.
- The Zig build now uses zig-common v0.12.0. A directory open goes from
  `[Errno 22] Invalid argument` to `[Errno 21] Is a directory`, and any
  other non-regular file goes from `[Errno 22] Invalid argument` to
  `not a regular file`.
- The Zig build now uses zig-common v0.14.0. A relative path under a removed working directory stays normalized as written, as before, where `fsx.absPath` now reports `error.NotFound`. The state-root lock and the path helpers of the commands keep that behaviour. A close that fails while a state file, an outbox record or a metrics file is written is now reported as that write's failure and leaves the destination as it was.
- The Zig job installs `jsonschema`, as the unit job does, and builds release and test-ops binaries. It then runs required test discovery against both binaries, with user namespaces enabled.
- The Zig unit tests compile with the error `chmod` returns, which the link unlink test handles.
- The `gc-keep` case of `docs/examples/site.sh` waits for its job to say it is in the replaced tree and ends it with a line on its input, instead of polling `/proc` and killing a `sleep 600`.
- The access-control test store now saves through zig-common `state.writePrivate`,
  the exclusive private replace, instead of its own fixed temporary name.
- The existing zig tests compile and run with zig 0.17.1.
- The filesystem fault step of the zig job runs the retire flush test.
- The generations and ledger command-line tests run each front end through the shared `sites()` and runners in `tests/support.py`, with their own subtest per front end, and the crash matrix reads tree markers with the engine's own reader.
- The generations tests and `tests/differential.sh` wait for the paused rollback with a named pipe. They wait for the processes that hold trees in use with standard input. Neither sleeps or polls.
- The integration job runs on Python 3.14, and both stagedemo test modules run after the zig binary is built.
- The lock-holding child in the stale-lock recovery test now signals readiness, so the test no longer competes with it for the lock.
- The parser error shape parity test fails when `TOOL_BIN` is unset, so it no longer skips its comparisons.
- The path bytes parity test passes `--backend installpath`, so Python and the binary print the same dry-run text with or without user namespaces.
- The publish recovery suite covers a failed removal of a created link, including a failed open or flush of its directory, and the resume refusal when the link stays in place.
- The rollback suite compares a failed open of the directory that holds a created link.
- The rollback suite compares a failed unlink of a created link, and a failed flush of its directory, with Python.
- The test artifact can fail the unlink of a finished hard-link replacement, and publication stops before the live tree moves.
- The unit, docs and changelog draft jobs run on Python 3.14.
- The zig tests that compile faultfs run after fuse3 is installed.
- The zig tests that mount faultfs read all of `/proc/self/mountinfo` while they wait for the mount. They read one page of it, so on a host whose mountinfo is longer than a page they never saw the mount and failed with `FuseTimeout`.
- `implementations()` in `tests/support.py` gives the ledger command-line tests the Python tool and the program `TOOL_BIN` names, and fails when `TOOL_BIN` is unset. The unit job builds the Zig binary and sets `TOOL_BIN`.
- `loadConfig` maps a directory or other non-regular file from
  `fsx.openRegular` to `EINVAL` and leaves a symlink loop unchanged.
  With zig-common v0.10.0 those opens already fail with `EINVAL`, so the
  ledger config refusal text does not change.


## 2.0.1 (2026-11-24)

### Fixes

- A dry run reports the same free-space refusal as a real run, and it does not create the state root.
- A failed flush of the directory holding a swapped install names that directory.
- A filename-less flush of the parent directory is reported on that directory.
- A full or read-only filesystem during publication names the directory or temporary file that could not be written.
- A hole that cannot be punched in a rewritten file stops publication, and the original file stays.
- A killed rewrite of a hard-linked file is finished by the next publication, instead of refusing because a temporary link is still in the tree.
- A retire that cannot list a directory it created still removes that directory when it is empty. An interrupted listing is tried again.
- A rewritten file stays unpublished when its directory cannot be flushed.
- A sealed directory that cannot be made writable stops publication before the completion stamp is written.
- A user attribute that cannot be copied onto a rewritten file stops publication, and the original file stays.
- An interrupted read of the staged module tree is reported on standard error. A directory that cannot be read for another reason is skipped.
- Garbage collection rejects a lock timeout that is not a finite number.
- Module-tree discovery now refuses an unreadable subdirectory instead of skipping it without a message, so publication decisions rest on a complete listing.
- Planning a staged run refuses a non-UTF-8 or unreadable staged module file with the path and reason on both frontends, leaving the live tree untouched and marking the run journal failed.
- Propagate failures removing the completed hard-link replacement during prefix publication instead of publishing with a stranded temporary.
- Refuse a prefix rewrite when a directory cannot be listed, matching the release binary.
- Report a failure removing a private prefix temporary during recovery instead of publishing the staged tree with that temporary still inside it.
- Report symlink unlink errors in Zig publish recovery and rollback instead of discarding them and clearing link ownership in the journal.
- Resume rejects a lock timeout that is not a finite number.
- Retire rejects a lock timeout that is not a finite number.
- Rollback rejects a lock timeout that is not a finite number.
- Run rejects a lock timeout that is not a finite number.
- The Zig binary reports directory-open errors such as `Permission denied` when `STATE_ROOT/runs` is an unreadable directory instead of silently treating the run list as empty, matching the Python implementation.
- The Zig state-root lock normalizes `--state-root` before checking symbolic-link components and avoids double slashes in lock error paths, matching the Python `FileLock` behavior.
- The deep walks hold one descriptor at a time: the prefix walkers close
  their directory before descending, the livecheck walk keeps a worklist of
  paths, and the admin cleanup refuses a tree nested past 512 levels with
  `error.TooDeep`, so a run tree past the file-descriptor limit still walks
  whole.
- When `--state-root` points to an existing regular file, the Python frontend now reports `Not a directory` like the Zig frontend instead of reporting `File exists`.
- `eb-staged-rebuild run --min-free-bytes` accepts every unsigned 128-bit integer in the Zig build.
- `probe --json` reports a failed file operation with Python's own text,
  `[Errno N] text: 'path'`, through the helper the rest of the program
  uses.
- resolve reports a module file that has no root assignment.

### Miscellaneous

- Add a parity test for `gc` refusal while another process has the state-root lock.
- Delegate `admin.abspath` in `zig/admin.zig` to `zig-common` `fsx.absPath` instead of carrying a local path normalization loop.
- One function checks that a lock timeout is finite.
- The empty `--metrics-file` test compares against the binary named by `TOOL_BIN` instead of a fixed `zig-out/bin` path.
- The prefix-walk test checks the Python rewrite when the release binary
  is unset.
- The remaining black-box CLI tests use the shared runners in `tests/support.py` instead of their own copies.
- The remaining parity and CLI tests run through the shared helpers in `tests/support.py`, so `TOOL_BIN` reaches every parity check instead of a fixed `zig-out/bin` path.
- The test of a link unlink in a sealed directory compiles again: it ignored the error `chmod` returns since the sealed-directory refusal.
- The user attribute test reports an unexpected child status, and the zig job requires it.
- The user attribute test skips when a user namespace or an overlay mount is unavailable, and the zig job runs it with TOOL_BIN set.
- The zig job installs fuse3 once and requires the filesystem fault tests.


## 2.0.0 (2026-11-19)

### Features

- Add opt-in `--usage-log` and `--eb-keep-logs` to `gc` and `retire`.

  When enabled, both commands call `eb-keep-logs used --prefix ... --require-none`
  before removing or retiring paths. `gc` keeps matching trees and generations,
  and `retire` refuses to move a module file when usage is reported.
- Add opt-in atomic Prometheus metrics output to garbage collection.
- `EB_STAGED_REBUILD_MIN_FREE_BYTES` sets the default free-space reserve for every staged rebuild.
- `eb-staged-rebuild gc --json` prints collection actions for site automation.
- `eb-staged-rebuild run --require-exchange` refuses publication unless the filesystem supports one-step directory exchange.
- `eb_staged_rebuild/site_hooks.py` is an EasyBuild hook for a site prefix. `EASYBUILD_HOOKS` names the file. With `EB_LOCAL_PREFIX` or `EESSI_SITE_SOFTWARE_PATH` set, `pre_install_hook` refuses an install outside that prefix. `EB_LOCAL_MODULE_FOOTER` appends a module footer, and `EB_LOCAL_RPATH=1` turns toolchain rpath on. Unset, each of those does nothing.
- `probe` has a `--no-create` option that reports a missing `--path`, exits with status 1, and leaves the filesystem unchanged.
- `probe` has a `--require-eb` option that exits with status 1 and a clear message when the selected EasyBuild command is not found.
- `python3 -m eb_staged_rebuild.ship` records file checksums and link targets and verifies a copied install tree.
- `run --min-free-bytes BYTES` refuses before creating the run journal or starting the build command when the generation filesystem has less than the requested number of bytes available. Without the option, `run` does not check free space.
- `run` accepts an opt-in `--build-dir DIR` option that passes `--buildpath=DIR` to EasyBuild and removes `EASYBUILD_BUILDPATH` from its environment, while refusing directories inside the generation root or the live `--software` or `--modules` trees.
- `status --json` prints one JSON document of the journals: each run's state, backend, message and module plan, or a retire record's module file and software directory. With no journal the document is `{"runs": []}`. The text is unchanged without the option. Both the Python tool and the binary.
- eb-staged-rebuild now generates bash and zsh completion scripts and a man page from the live parser through pinned shtab and argparse-manpage calls in CI and release builds.

### Fixes

- A grace period below 0, `nan` or `inf` was accepted from `--grace-hours` and `EB_STAGED_REBUILD_GRACE_HOURS`, and made `gc` treat a tree as past its grace period, or never past it; the binary also took the `-5` of `--grace-hours -5` for an option. `run` and `gc` now refuse such a value with exit 2, `argument --grace-hours: not a number of hours, 0 or more: '-5'` for the option, and the binary takes a word that looks like a negative number for a value, as Python 3.14's argparse does, for every option and positional argument. Both the Python tool and the binary.
- A relative `--software`, `--modules` or `--state-root` reached EasyBuild's `MODULEPATH`, the `userns` sandbox's read-only mounts, the journal's module and link paths and `prepare-context.json` as given, so a relative `MODULEPATH` entry named another directory once EasyBuild changed directory, and a rollback from another directory stopped halfway with the module file "restoring". `run` now makes the three paths absolute once it has checked them. Both the Python tool and the binary.
- A rollback whose ledger records were not acknowledged said `run resume`, which refuses a rolled-back run and, after a refused rollback, delivers the correction's `started` record without rolling back. Both messages now end with `run rollback again`, which delivers the records and goes on. `run`, `resume` and `rollback` print their last line once when a record waits, instead of printing it again with the warning appended; `resume` of a complete run whose ledger policy changed no longer prints that the run is complete before refusing. Both the Python tool and the binary.
- A value of `EB_STAGED_REBUILD_GRACE_HOURS` that is not a number stopped the Python tool's `run` and `gc` with a `ValueError` traceback and exit 1. Both builds now refuse it with `EB_STAGED_REBUILD_GRACE_HOURS is not a number: 'abc'` and exit 2, as the binary did, and quote the value as Python's `repr()` does, where the binary printed it between single quotes as it was. Both the Python tool and the binary.
- An `installpath` or `userns` build records the generation directory in RPATH, pkg-config files, shebangs and other installed files. Publication rewrites that prefix to the stable install path before it exchanges the directory. A binary whose recorded path is shorter than the stable prefix is refused instead of published with the generation path still in it. That rewrite edited each file in place. A crash tore the bytes, a symbolic link was followed, a sparse file was filled, hard links were not a single replacement, and `chmod` (which replaces an NFSv4 ACL) was used to open a file that was not owner-writable. The rewrite now publishes a same-directory replacement that keeps the owner, mode, ACL and user attributes, keeps the tree's hard links on one inode, leaves a hole a hole, and replaces a symbolic link by rename.
- EasyBuild got the live module root, the directory `--modules` names, first on `MODULEPATH`, where Lmod finds no module under its usual name, so a build with the `installpath` or `userns` backend found the dependencies installed in the live tree only when the caller's `MODULEPATH` named its `modules/all` already, and failed with "Missing modules for dependencies" otherwise. Every EasyBuild command now gets the live tree's module files first on `MODULEPATH`: `all` under the module root, or the suffix that `--suffix-modules-path` after `--` or else `EASYBUILD_SUFFIX_MODULES_PATH` gives, as EasyBuild reads them. Both the Python tool and the binary.
- Fall back to `statvfs.f_bsize` when `statvfs.f_frsize` is `0` in `eb_staged_rebuild.engine._check_free_space`, matching Zig `availableBytes`.
- Handles probe JSON path arguments with non-UTF-8 bytes consistently across Python and Zig front ends.
- Invalid journal JSON now reports the same refusal text in both implementations by using `journal cannot be read: PATH: not valid JSON` instead of a backend-specific decoder exception string.
- Journal readers report invalid record shapes as operator intervention.
- Off a terminal, EasyBuild 5.4's rich output wraps each dry-run line at 80 columns, so `run` without `--module` found no module line and refused. A wrapped module line is now read whole.
- Publication preparation keeps each run ready to resume until directory swapping starts.
- Python probe now handles non-UTF-8 bytes from external probe commands without
  crashing, and keeps the same JSON probe response shape as Zig.
- Python probe now reports unreadable probe paths at the probed directory path,
  matching Zig and avoiding transient temp-directory names in the refusal text.
- Python resolve output now preserves an explicitly empty quoted Tcl root value,
  matching Zig and avoiding a Python-only `null` root in parity output.
- Python snapshot parity now refuses unreadable subdirectories instead of
  returning a partial record, matching Zig's exit-2 refusal.
- Reject `NaN` and `Infinity` ledger config timeouts in `eb_staged_rebuild.ledger_adapter.load_config` and `zig/publish.zig` instead of letting `_bounded` spin without a deadline.
- Resolve reports non-UTF-8 module text as an operational refusal.
- Resume reports an incomplete publication plan before accessing staged module files.
- Resume reports an interrupted rollback and directs operators to complete it.
- Rollback now reports invalid `outbox.json` as `not valid JSON` in the Python CLI, matching the Zig CLI's refusal text for corrupt or zero-byte outbox files.
- The Python tool read the test hook `EB_STAGED_REBUILD_FAIL_AT` in every install, so a value left in the environment made a run or a rollback fail at the steps it named with `[Errno 5] injected failure at STEP`, where the release binary ignores it. Only `tests/fail_cli.py`, which runs the Python tool for the fail tests, reads it now, as only the binary's `-Dtest-ops` test artifact does.
- The Zig parser keeps `--` in unrecognized-argument errors as Python 3.14 does: every `--` for subcommands without positional arguments, all but the first for the others.
- The Zig parser reports unknown options from before the subcommand together with the subcommand's own leftovers, after its required-argument checks, as Python 3.14 does.
- The `userns` backend, which `--backend auto` takes when bubblewrap cannot run, ran EasyBuild as root of its user namespace, and EasyBuild refused to build ("You seem to be running EasyBuild with root privileges"). The sandbox now runs EasyBuild in a second user namespace that maps the caller's uid and gid onto that root, so EasyBuild runs as the caller, without capabilities, and cannot remount the read-only live trees. The probe checks that the second namespace can be made. Both the Python tool and the binary.
- The binary put a command-line value it refused between single quotes as it was, where the Python tool quotes it as `repr()` does: `--lock-timeout "a'b"` gave `invalid float value: 'a'b'` instead of `invalid float value: "a'b"`. The binary now quotes the value as the Python tool does in the errors for an invalid float (`--lock-timeout`, `--grace-hours`), an invalid choice (the command, `--backend`) and a value given to a flag, such as `--dry-run=x`. The binary only.
- The binary's build takes `-Dcommit`, as eb-local's and eb-keep-logs' do, and its `--version` prints `eb-staged-rebuild VERSION (TARGET, MODE, zig VERSION, COMMIT)`; the first two words are the Python tool's line. The version comes from `build.zig.zon` alone, and release binaries name the tag's commit.
- The state-root gate `.lock` was truncated and rewritten with the holder's pid without checking that it was a regular file with one hard link. A hard link planted on that name lost the other name's bytes, a FIFO was opened, and a symbolic link used as the state root was followed. The gate is refused unless it is a private regular file, a symlink component of the state root is refused, and both lock files are owner-only so another user who can traverse the directory cannot open them and take the flock.
- Treat a `NaN` deadline in `FileLock.acquire` as expired (`not (remaining > 0)`) instead of hanging indefinitely on a contended lock, matching the Zig binary's exit `2` refusal.
- Trim ASCII whitespace when parsing `EB_STAGED_REBUILD_MIN_FREE_BYTES` in the Zig CLI (`zig/main.zig`) to match Python's `int()` parsing.
- Validates hostile ledger configuration file paths in both front ends during run dry runs.
- `eb-staged-rebuild run --min-free-bytes` trims surrounding whitespace and accepts integers above `u64::MAX` in the Zig build.
- `eb-staged-rebuild run` checks the staging filesystem before starting the build.
- `eb-staged-rebuild run` falls back to `f_bsize` in the Zig build when `statfs` reports zero `f_frsize`.
- `eb-staged-rebuild` confines every run ID to one state-root name before executing EasyBuild.
- `eb-staged-rebuild` validates every live-tree manifest before publication.
- `gc` removes a run's generation after all of its hidden trees are settled, including failed unpublished runs when `--include-failed` is given.
- `probe --json` now reports the path `shutil.which` reports, so a relative `PATH` directory or an empty `PATH` entry stays relative instead of being made absolute.
- `probe` reports no executable for `bwrap`, `unshare` or a bare `--eb` name when `PATH` is an empty string, as the Python probe does.
- `probe` says to install EasyBuild and put `eb` on PATH, and links the EasyBuild installation guide, when the EasyBuild command is not there. Pass `--eb` with its path instead.
- `resolve` now accepts Tcl module roots written as braced values with spaces (`set root {/path with spaces}`), matching Tcl token rules and keeping Python/Zig parity.
- `resume`, `rollback`, `gc`, `retire` and `status` refused to run without `--state-root`, even with `EB_STAGED_REBUILD_STATE_ROOT` set, which `run` uses. They now take the variable as the default of `--state-root` too, unless it is empty, and their usage then shows the option in brackets, as argparse prints it. Both the Python tool and the binary.
- `retire` moves module-class and nearby symbolic links with their module file, and `rollback` restores them instead of leaving dangling links.
- `retire` refuses a module path that is not UTF-8 text with exit code 2 in Python and in the binary.
- `rollback` of a run that a later run of the same installation had replaced went ahead: it put back the tree from before the earlier run, moved the later run's tree to the earlier run's hidden name, and left the later run `complete` and `published` in its journal. `rollback` now checks first, by the completion marker, that each stable path holds the tree the run published, and otherwise refuses before it changes anything or records anything in a ledger, naming the run to roll back first. Both the Python tool and the binary.
- `run --dry-run` writes non-UTF-8 path bytes instead of failing with `UnicodeEncodeError` when stdout uses strict UTF-8 encoding.
- `run --grace-hours` was written into the journal and never used: `gc` kept every tree for its own `--grace-hours`, `EB_STAGED_REBUILD_GRACE_HOURS` or 144 hours. `gc` now keeps each tree for the grace period its run recorded; `gc --grace-hours` gives every tree the same one instead, and `EB_STAGED_REBUILD_GRACE_HOURS`, or 144, applies to the directories of retired module files and to a run whose journal records no number of hours, at least 0. Both the Python tool and the binary.
- `run` refused a module to rebuild that does not exist with "PATH is a missing, not an existing rebuild directory", and one that is a FIFO, socket or device with "PATH is a other, ...". The refusal now says "PATH is missing", "PATH is a file" or "PATH is a special file", and the rebuild guide shows it. Both the Python tool and the binary.
- `run` refused to build, with "run needs --module Name/version or an eb --dry-run module line", whenever EasyBuild could use rich and `FORCE_COLOR` was set and not empty, even to 0, unless `TERM` was `dumb` or `unknown`: rich then writes ANSI escape sequences into the output of `eb --dry-run` even through a pipe, and the module lines no longer matched. With `--module`, the run went ahead without the checks those lines feed, such as the refusal of a dependency that `--robot` would install. Both dry runs now get `EASYBUILD_OUTPUT_STYLE=basic`, which EasyBuild reads since 4.5.0, while the build keeps the caller's output style, and their output is read without ANSI CSI and OSC sequences, which an `--output-style` after `--` can bring back. Both the Python tool and the binary.
- `run` with `--robot` on EESSI refused before building, naming an EESSI dependency such as `M4/1.4.20` as "a missing, not an existing rebuild directory". The module preflight now runs `eb --dry-run` with the build's arguments and `--rebuild`, and leaves out the installed dependencies EasyBuild marks `[x]`; the easyconfigs to rebuild are `[R]`. Both the Python tool and the binary.
- `run` with `--robot` refused before building when EasyBuild's dry run marked `[ ]` a dependency that the build would not install, such as a build dependency of an installed toolchain, naming it "a missing, not an existing rebuild directory". When the dry run marks a module `[ ]`, the preflight now asks `eb --extended-dry-run`, with the build's arguments, `--stop=fetch` and `--ignore-locks`, which modules EasyBuild would build, and leaves out the others; a missing dependency it would build is still refused, and so is a run whose extended dry run fails. With `bwrap`, the empty mount points that EasyBuild makes in the live tree for the left-out modules are recorded in the journal and removed after the build. Both the Python tool and the binary.
- run loads ledger config before probe and backend selection, including dry-run.

### Documentation

- A how-to guide for the ledger: record each rebuild in a signed ledger. It runs on the example site of the other guides, with eb-stack 0.7.0 and keys made with openssl, and shows the records the run and its rollback wrote.
- Add `[--require-exchange]` to the `run` synopsis in `cli.org`, list `--min-free-bytes`, `--require-exchange`, and `--ledger-config` pre-build refusals in `exit-status.org`, and note finite `timeout` validation in `configuration.org`.
- Add the `--json` option to the `gc` synopsis in `reference/cli.org`.
- Clarifies `probe --json` exit-1 behavior in the JSON-output reference and adds a test that pins the documented contract to both front ends.
- Clarifies when `resolve` prints JSON on stdout and adds a test that pins the documented stdout contract.
- Document `TMPDIR`, `TEMP`, and `TMP` in `eb-staged-rebuild` `reference/environment.org`.
- Document exit status 1 for `probe --no-create` and `probe --require-eb` in `cli.org` and `exit-status.org`.
- Documents the JSON stdout formatting contract (sorted keys, two-space indent, trailing newline) and adds a test that pins it to both front ends.
- Draft 2020-12 JSON Schema files for `probe --json` and `status --json` live under the reference docs. Unit tests validate command output against them for both the Python tool and the binary when `TOOL_BIN` is set.
- Every `pub` declaration of the Zig build has a doc comment. The module comments of `zig/main.zig`, `zig/engine.zig` and `zig/run.zig` said that run, resume and rollback of a run were not in the binary yet, or that a run had no ledger; they say what the files do now.
- Every public module, class, function and method of the Python package has a docstring, so the API pages describe each of them. The comment on `DEFAULT_GRACE_HOURS` and the `gc` module's docstring cited notes a reader does not have for the 144-hour grace period; they say what it assumes.
- Explanation pages: why a rebuild is staged and swapped in, how a build is kept off the live tree, how a rebuild is published, what the journal records, how runs keep out of each other's way, and what the ledger records. Each says what was turned down and where the approach stops; the limits page and the measurements of a walk-through host are gone. The figures are SVG files committed beside their Graphviz sources, so the docs build does not need Graphviz.
- How-to guides for an operator's tasks: install the tool and check the software filesystem, rebuild an installed module, set permissions before the swap, finish a run that failed or stopped, roll a rebuild back, remove old trees, and retire a module. Their output is recorded on an example site that EasyBuild 5.4.2 builds, by `docs/examples/site.sh`, which the docs job of the tests workflow runs.
- Make the first staged-rebuild tutorial work from a clean Ubuntu installation by naming the venv package it needs and following the current main branch.
- Map the new `test_site_hooks` unit tests in `TESTMAP.md` so the unit CI check sees complete coverage.
- Reference pages for the ledger configuration, the environment variables, the exit statuses of every command, and the files the tool writes in the state root and beside an installation.
- The README release pointer uses the latest release URL, and the rebuild how-to documents the install-path fallback when bubblewrap and user namespaces are both unavailable.
- The README says when the tool is not needed: on EESSI's CernVM-FS repositories the publish transaction already swaps atomically.
- The design note's atomic-write temporary file is `.{basename}.new`. A name already there is refused unless it is a regular file with one hard link, owned by the writer or the metadata owner.
- The documentation is built from Org pages in `docs/orgmode/`, with the theme, menu and tooling of the other stackhands sites, and starts with a tutorial, the command reference and the API reference generated from the Python and Zig code. `pixi run -e docs docbld` builds it with warnings as errors and checks its links; the docs workflow checks the external links as well, weekly. The tutorial's output is recorded from `docs/examples/tutorial.sh`, which CI runs. The Markdown site, its walk-through script and the `docs` extra in `pyproject.toml` are gone.
- The guides to removing old trees and to retiring a module file show what `gc` and `retire` print now that they remove a run's generation and move the module file's links: a `remove:` line for each generation, and the `would retire the link`, `retired the link` and `restored the link` lines.
- The how-to guide for a rebuild gave as the cause of a `run --robot` refused for `M4/1.4.20` a dependency that the run would build as well, and the explanation of the isolation said the same. EasyBuild's dry run lists the dependencies of installed modules too, such as the build dependencies of an installed toolchain, which `eb --robot` does not build; both pages say so now, and the explanation counts the refusal among its limits.
- The install how-to now says that Ubuntu users should install `python3-venv` before they create a virtual environment for the Python package.
- The rebuild how-to says that `run` is the command that swaps, that `eb exited N; live modules were not changed` means the live tree was not swapped, and that `eb-keep-logs why` reads a log tree of `YYYYMMDD_HHMMSS` runs rather than the state root.
- The stackhands table and the documentation menu list eb-pipeline.
- The stand-in `eb` of the tutorial, `docs/examples/eb-standin`, said that the documentation walk-through used it; the walk-through is gone, and the tutorial installs it.
- `COMPAT.md` said the Python tool stays the one to use; it now says either build can be used, as the README does.
- `README.md` shows `probe`, `run` and `status` on the example site of the how-to guides, and downloads the release with the `curl` options the install guide uses. `DESIGN.md` says that the state root's lock is a `flock` and a POSIX record lock, gives the 144-hour grace period as an assumption to replace, and names the documentation pages where it named a runbook, the README and Org pages that are gone. `RELEASING.md` says that v1.0.0 is built without `-Dcommit`, and `TESTMAP.md` no longer cites a plan that is not in the repository.

### Removals

- The `ledger-maintenance` command (`backfill`, `anchor`, `verify-anchors` and `project`) and the `build-events` command are removed from the binary, with their Python modules `eb_staged_rebuild.ledger_maintenance` and `eb_staged_rebuild.build_events`. Ledger maintenance lives in eb-stack as `eb-stack ledger backfill`, `eb-stack ledger anchor`, `eb-stack ledger verify-anchors` and `eb-stack ledger project`. A pipeline that records the outcome of another build command appends its records with `eb-stack ledger append`. `run --ledger-config` is unchanged.

### Miscellaneous

- A release is cut with `cog bump`: `cog.toml` runs `tools/release-prepare`, which builds the changelog and sets the version wherever the release workflow or a download command names it. `RELEASING.md` has the steps.
- Add `run_cli` and `run_cli_optional_binary` to `tests/support.py`. The second runs the program named by `TOOL_BIN` and returns `None` when it is unset.
- Added CLI parity coverage for `probe` when `--path` names a regular file, asserting exit code 2 with the filesystem refusal message.
- Added CLI parity coverage for `status` with a missing run id, asserting exit code 2 and the `no journal` stderr message.
- Added CLI parity coverage for rejecting a relative `--prepare` executable path with exit code 2 and a stable stderr message.
- Added CLI parity coverage for rejecting an empty `--metrics-file` value with exit code 2 and the expected stderr text.
- Added black-box CLI tests for run, gc, and status behavior, including an expected-failure case for non-finite run lock-timeout input.
- Added extra black-box CLI coverage for probe, resume, and rollback, including a reproducer-backed expected failure for non-finite resume lock timeout input.
- Added new black-box CLI tests for resolve, resume, and retire command behavior and edge cases.
- Adds a CLI parity test for `gc` refusing a non-numeric `--grace-hours` value.
- Adds a CLI parity test for `run` refusing a non-integer `EB_STAGED_REBUILD_MIN_FREE_BYTES` value.
- Adds a CLI parity test for `run` refusing an empty `--build-dir` value.
- Adds a CLI parity test for `run` refusing an empty `--prepare` value.
- Adds a CLI parity test for `run` refusing when no easyconfig argument is supplied.
- Adds a CLI parity test for `run` refusing when software and modules roots are not set.
- Adds a CLI parity test for refusing `retire` when a module file has no root assignment.
- Adds a CLI parity test for refusing `retire` when the same module file was already retired.
- Adds a CLI parity test for the `gc --json` refusal-path stdout/stderr contract.
- Adds a CLI parity test for the `probe --json` happy-path output contract.
- Adds a CLI parity test for the `status --json` refusal-path stdout/stderr contract.
- Adds a parity test for `resume` refusing a run id containing path components.
- Adds a parity test for `rollback` refusing a run id containing path components.
- Adds a parity test for gc refusing non-regular metrics destination paths.
- Adds a parity test for lock recovery after the lock holder process is terminated.
- Adds a parity test for retire refusing a state root path that traverses a symbolic link.
- Adds a parity test for retire rejecting a FIFO path passed as the module file.
- Changes are recorded as news fragments in `changelog.d/`, and towncrier assembles them into `CHANGELOG.md` at release, with zig-common's fragment types. CI drafts the release notes and fails on a fragment whose name has no type.
- Pin `zig-common` to `v0.10.0` in `build.zig.zon` so `eb-staged-rebuild` can use `fsx.mkdtemp` and `fsx.absPath`.
- Switch `zig/probe.zig` in `eb-staged-rebuild` to call `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` retry loop.
- The comparison of the two implementations also rolls back a userns run that a later run with a ledger replaced, and the test of that refusal checks that the live tree was not moved and moved back.
- The docs job of the tests workflow runs `docs/examples/site.sh` with the release binary, and the tutorial with the Python package.
- The static build uses zig-common 0.8.1.
- The tests of the bounded ledger call no longer fail under load: their helper processes write each pid and progress file under a temporary name and rename it into place, so a test never reads a file that a helper killed in the middle of writing it left empty.


## 1.0.0

* A static Zig build of every command, one binary per architecture
  (x86_64, aarch64) built for `linux-musl` on zig-common 0.8.0: `probe`,
  `status`, `resolve`, `retire`, `gc`, `run`, `resume` and `rollback`, with
  and without `--ledger-config`, plus `ledger-maintenance` and
  `build-events` as subcommands. Its output matches the Python tool byte for
  byte except where `COMPAT.md` lists a deliberate difference. Releases carry
  the binaries, `SHA256SUMS` and build provenance attestations.
* CI checks both builds against each other. A differential runs every
  command and compares the trees and journals it leaves. A crash matrix kills
  a run, a rollback and a retire at every fault step (295 cases). A fail
  matrix gives every step an I/O error (152 cases). The signal, ledger,
  metadata, access-control, build-events and real-EasyBuild suites also run
  against the binary. `TESTMAP.md` maps each test that imports the Python
  code to what covers it for the binary, and CI fails on a missing row.
* Fix: a run killed right after `RENAME_EXCHANGE` resumes to the published
  state, and a rollback killed right after its exchange rolls back once.
* Fix (binary): an allocation failure stops the program. It never runs
  `eb` with the parent environment and without the staged install paths.
  A restore that cannot be recorded is not ignored. A damaged build-events
  state file is refused instead of crashing. Error text names the errno the
  kernel gave.
* Fix (binary): a missing `eb-stack` is "cannot start", as in Python, and a
  stop during ledger delivery kills the ledger's process group first.
* The ledger call keeps the first 64 KiB of each stream, returns when
  `eb-stack` exits and stops any process it left behind.
* Documentation: <https://stackhands.github.io/eb-staged-rebuild/>, with the
  release-binary install and the Zig build's differences.

## 0.4.4

* Restore the required access control list before removing inherited
  virtual ACL views, retaining the selected publication policy.
* Accept an unlisted NFSv4-family EINVAL probe only when the same inspection
  lists a readable POSIX access ACL. Listed, denied and POSIX failures
  remain errors.
* Keep the command's reported version consistent with the distribution
  metadata.

## 0.4.3

* From the `fix/cluster-record-lock` line, on the directory-swap model: a
  publication-preparation program that leaves a staged tree without build
  evidence blocks the swap; an empty or relative `--prepare` is refused
  before anything runs; a sealed (read-only) install takes the run marker
  and moves beside the live tree through a writable window on its top
  directory; a read-only module stage is cleared through the owned-tree
  cleanup on resume; the preparation context names each staged tree as
  `generation` too; a prepared access policy is published and rolled back
  even when the module bytes did not change. The branch's prepare,
  publication-metadata, read-only, sealed-install, ledger-policy and
  bounded-output tests come along, written for the swap model; its
  generation-pointer gc and outbox suites stay on the branch.

## 0.4.2

* The ledger tests and the pinned-ledger CI action build eb-stack 0.7.0
  (defb6ae); the ledger CLI's receipt, tile-log and checkpoint lines are
  unchanged from 0.6.2.

## 0.4.1

* `ledger_maintenance.anchor` accepts the `index checkpoint` line eb-stack
  0.6.0 prints after the head's checkpoint once the tile log has sealed a
  run (every 256 records), instead of refusing the receipt.

## 0.4.0

* `eb_staged_rebuild.build_events` records `started`, `finished` and
  `aborted` ledger events around one build command through a durable
  outbox, and `eb_staged_rebuild.ledger_maintenance` backfills observed
  history, publishes daily signed anchors, proves the anchor chain and
  projects package history from a verified log (`docs/howto/build-events.md`,
  `docs/howto/ledger.md`).
* The ledger adapter and the maintenance verifier read the eb-stack 0.5.2
  CLI: the receipt is the first line `ledger append`, `ledger head` and
  `ledger verify` print, exactly one tile-log or checkpoint line for the
  same size and root follows it (none for the size-0 head of an empty
  log), and a tile log is written beside every private ledger copy before
  `ledger verify` audits it.
* `_bounded` runs the ledger CLI through pipes and kills its process group
  on every exit, including a signal to the caller, so an interrupted
  delivery leaves no eb-stack process behind.

## 0.3.1

* EasyBuild 5.4.1 is in the bwrap key table: its `bwrap_info.json` carries
  `bwrap_installpath_software` and `bwrap_installpath_modules`
  (easybuild-framework-v5.4.1, `easybuild/tools/bwrap.py`), so `probe` on a
  5.4.1 whose Python the probe cannot import reports the keys as read from
  source instead of unknown.

## 0.3.0

* Pair the local flock lock with a POSIX record lock on `.lock.posix`.
  Cross-node coherence is still unmeasured. A 0.2.0 flock-only process does
  not take the sidecar.
* Module-file replacement keeps ownership, mode, and the listed access
  control list bytes. A stale privileged temporary file is refused.
* Drain a build's process group before the writer lock is released.
* Refuse garbage collection when a tracked module cannot be read, and keep a
  hidden sibling that a module reaches through a symlink. Removal stays on
  the sibling's filesystem and owned directories.
* Record a directory-swap publication in an eb-stack ledger outbox. The
  record's `location` is the stable install directory. The journal schema is
  4 when a ledger policy is frozen, and schema 2 otherwise. Schema 1 and
  schema 3 generation-pointer journals are refused.
* This version creates no git tag.
* Recorded checks use ext4.
* Cross-node locking remains unmeasured.
* Behavior on the general parallel file system remains unmeasured.

## 0.2.0

* Stage rebuilds with automatic bubblewrap, user/mount-namespace, and
  documented unsandboxed install-path fallback selection.
* Publish complete trees with `renameat2(RENAME_EXCHANGE)` and a restoring
  ordinary-rename fallback for unsupported filesystems.
* Keep one `flock` across build and publish; add transactional rollback and
  interrupted-fallback recovery.
* Make dry-run report sandbox and swap decisions without invoking EasyBuild.
* Add exhaustive failure injection for build, journal, swap, module, link, and
  rollback paths.
* Bump the journal schema to 2; version 1 generation-pointer runs are refused
  instead of being interpreted as directory-swap transactions.
* Name retention and kernel constants and record their sources.
* Add Org-mode how-to, reference, baseline design audit, and recommendations
  for the open measurements.
