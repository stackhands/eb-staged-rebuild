Fall back to `statvfs.f_bsize` when `statvfs.f_frsize` is `0` in `eb_staged_rebuild.engine._check_free_space`, matching Zig `availableBytes`.
