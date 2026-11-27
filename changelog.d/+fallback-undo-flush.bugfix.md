When a parent flush fails after the three-rename fallback, the swap
puts the names back with the inverse three renames. The swap flushes
that undo. When that undo was flushed, a crash leaves the restored
names rather than the swapped names after the command reports
failure. A failed undo is reported. The failed undo can leave the
swapped names.
