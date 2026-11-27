The run judges the generation before any log, journal save, eb, or creation of the runs directory, on publish, resume, `resume --verify` and `run --dry-run`, for every backend.
The judgment does not depend on the backend or on `bwrap_info.json`. A symlink above the generation is followed.
Each existing child of the generation is checked without following a link.
A symlink or a special file there is refused, and a regular file is left.
`software` and `modules` must be directories.
The run refuses a symlink, a fifo, a socket, or a regular file at each component under `software`.
A dot-named regular file is left.
The walk opens every dot-named directory, up to 32 components deep.
A non-dot directory at version depth stays closed.
`Core`, `Compiler`, and `MPI` stay open through the name directory.
EasyBuild's install lock is left when it is a directory or a regular file directly under `software/.locks` and the name ends with `.lock`.
The refusal exits 2.
Every component that cannot be classified is refused.
A real `modules` directory is opened one component at a time.
A symlink or non-directory is refused at any component of a recorded stage path, from the generation through the install directory.
A link inside any install is left.
Resume of a complete run reports that the run is already complete before this judgment.
A stage being rebuilt is removed without following links before eb.
The judge keeps at most three directories open at a time and reports too many open files as a resource error.
An absent path rebuilds only when its existing ancestors are real directories inside the generation.
A module scan that cannot read a file keeps that failure on the journal, and resume refuses it before it saves the journal.
The sealed rename opens the final name as a directory
without following a link. When that directory is not writable, the
rename fchmods the open descriptor, and it keeps the rename when
the destination is the same file. The prefix rewrite reads and writes
through that open directory. The flush now fails when closing a
pinned parent fails, even after `fsync` succeeds.
The pin does not follow a link inside the state root, so a symlinked
state directory cannot swap in another tree.
After a rename, a swap or a retried resume, the parent flush fsyncs
that pinned descriptor, and a failed close fails the flush.
After the same-file check, an undone rename flushes the directory and
both parents even when restoring the mode fails. When that reverse rename or
those flushes fail, the run stops for intervention.
When the close or flush of a landed swap exchange or fallback rename
fails, the run keeps the new names and saves that stop on every owning
run's journal.
Resume and rollback stop for intervention and keep those names.
