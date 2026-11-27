const std = @import("std");

/// Publish the shared access-list helpers a held directory needs.
/// A rewrite is finished only when `closeChecked` is public. An earlier
/// rewrite that already discards through the directory descriptor keeps
/// that body and only publishes it. The rewrite that still discards by
/// name is upgraded in place.
pub fn rewrite(a: std.mem.Allocator, src: []const u8) ![]const u8 {
    if (std.mem.indexOf(u8, src, "pub fn closeChecked(") != null) return src;
    if (std.mem.indexOf(u8, src, "fn discard(dir:") != null) {
        return replaceOnce(a, src, "fn closeChecked(", "pub fn closeChecked(");
    }
    // A later package closes the temporary file through closeTemp and
    // closeDiscarded. The older anchors are gone; the same helpers are added
    // around those calls.
    if (std.mem.indexOf(u8, src, "fn closeTemp(") != null) {
        return rewriteClosed(a, src);
    }
    if (std.mem.indexOf(u8, src, "sync_parent: bool = true") != null) {
        return upgradeDiscard(a, src);
    }
    return rewriteFresh(a, src);
}

fn rewriteClosed(a: std.mem.Allocator, src: []const u8) ![]const u8 {
    var step = src;
    if (std.mem.indexOf(u8, step, "pub var skip_listed_attribute") == null) {
        const with_pub = replaceOnce(a, step, "fn readAllAcls(", "pub fn readAllAcls(") catch |e| return e;
        const with_skip = replaceOnce(a, with_pub, "        if (name.len == 0) continue;\n", "        if (name.len == 0) continue;\n        if (skip_listed_attribute) |skip| if (std.mem.eql(u8, name, skip)) continue;\n") catch |e| return e;
        const decl =
            \\/// One system attribute omitted from the list for the current read.
            \\/// A directory read sets this to `system.posix_acl_default` and
            \\/// clears it when the read returns. Null lists every name.
            \\pub var skip_listed_attribute: ?[]const u8 = null;
            \\
            \\
        ;
        step = replaceOnce(a, with_skip, "fn listedAcls(", decl ++ "fn listedAcls(") catch |e| return e;
    }
    if (std.mem.indexOf(u8, step, "pub fn xattrGet(") == null) {
        step = replaceOnce(a, step, "fn xattrGet(", "pub fn xattrGet(") catch |e| return e;
    }
    const close_checked =
        \\pub fn closeChecked(fd: sys.fd_t) sys.Error!void {
        \\    const rc = linux.close(fd);
        \\    const errno = linux.errno(rc);
        \\    if (errno == .SUCCESS) return;
        \\    // Linux releases the descriptor before returning EINTR, so this
        \\    // close is not retried. sys.result hides EINTR, and sys.describe
        \\    // then prints Unexpected as EIO. Keep the errno for the caller.
        \\    sys.last_errno = errno;
        \\    if (errno == .INTR) return error.Unexpected;
        \\    _ = sys.result(rc) catch |err| return err;
        \\}
        \\
        \\
    ;
    step = replaceOnce(a, step, "/// atomic_write: replace `path` only after its bytes and access metadata\n", close_checked ++ "/// atomic_write: replace `path` only after its bytes and access metadata\n") catch |e| return e;
    step = replaceOnce(a, step,
        \\    step: []const u8 = "write",
        \\};
    ,
        \\    step: []const u8 = "write",
        \\    /// When false, the parent directory is not flushed. The caller
        \\    /// flushes it after a held directory's access list is restored.
        \\    sync_parent: bool = true,
        \\    /// When true, close the new file before the rename and refuse a
        \\    /// failed close. A lost write is a failed update.
        \\    check_close: bool = false,
        \\};
    ) catch |e| return e;
    step = replaceOnce(a, step,
        \\    renameTemp(tmp, path) catch |err| return failOs(a, why, err, tmp, path);
        \\    renamed = true;
        \\    // Opening the directory names it. Flushing the descriptor names no file.
        \\    const directory = sys.openat(fsx.cwd, parent, .{}, 0) catch |err| return failOs(a, why, err, parent, null);
    ,
        \\    renameTemp(tmp, path) catch |err| return failOs(a, why, err, tmp, path);
        \\    renamed = true;
        \\    if (!opts.sync_parent) return;
        \\    // Opening the directory names it. Flushing the descriptor names no file.
        \\    const directory = sys.openat(fsx.cwd, parent, .{}, 0) catch |err| return failOs(a, why, err, parent, null);
    ) catch |e| return e;
    step = replaceOnce(a, step,
        \\    // Once, before the rename. A later failure must not close again.
        \\    open = false;
        \\    closeTemp(fd) catch |err| return failOs(a, why, err, null, null);
    ,
        \\    // Once, before the rename. A later failure must not close again.
        \\    open = false;
        \\    if (opts.check_close) {
        \\        closeChecked(fd) catch |err| return failOs(a, why, err, null, null);
        \\    } else {
        \\        closeTemp(fd) catch |err| return failOs(a, why, err, null, null);
        \\    }
    ) catch |e| return e;
    const at_opts =
        \\pub const AtOptions = struct {
        \\    /// When false, `dir` is not flushed. The caller flushes it after
        \\    /// a held directory's access list is restored.
        \\    sync_parent: bool = true,
        \\    /// When true, close the new file before the rename and refuse a
        \\    /// failed close. A lost write is a failed update.
        \\    check_close: bool = false,
        \\};
        \\
        \\pub fn atomicWriteAt(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32) (Allocator.Error || sys.Error)!void {
        \\    return atomicWriteAtOpts(a, dir, name, data, mode, .{});
        \\}
        \\
        \\pub fn atomicWriteAtOpts(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32, opts: AtOptions) (Allocator.Error || sys.Error)!void {
        \\    sys.last_errno = .SUCCESS;
        \\    var rnd: [4]u8 = undefined;
        \\    _ = try sys.call(linux.getrandom, .{ &rnd, rnd.len, 0 });
        \\    const tmp = try a.print(".{s}.{d}.{x:0>8}.tmp", .{ name, linux.getpid(), std.mem.readInt(u32, &rnd, .little) });
        \\    defer a.free(tmp);
        \\    var zb: [sys.path_max:0]u8 = undefined;
        \\    const ztmp = try sys.z(&zb, tmp);
        \\    const flags: sys.O = .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true, .CLOEXEC = true };
        \\    const fd: sys.fd_t = @intCast(try op(ops.openat, .{ dir, ztmp.ptr, flags, @as(linux.mode_t, 0o600) }));
        \\    const created = fsx.fstat(fd) catch |err| {
        \\        closeDiscarded(fd);
        \\        return err;
        \\    };
        \\    var done = false;
        \\    var closed = false;
        \\    defer {
        \\        if (!closed) closeDiscarded(fd);
        \\        if (!done) discard(dir, tmp, created);
        \\    }
        \\    // openat applies the umask to the create, so the exact mode the
        \\    // caller names is set on the descriptor before anything is written
        \\    _ = try op(ops.fchmod, .{ fd, mode });
        \\    try writeTemp(fd, data);
        \\    _ = try op(ops.fsync, .{fd});
        \\    // Once, before the rename. A later failure must not close again.
        \\    closed = true;
        \\    if (opts.check_close) try closeChecked(fd) else try closeTemp(fd);
        \\    var zn: [sys.path_max:0]u8 = undefined;
        \\    _ = try op(ops.renameat, .{ dir, ztmp.ptr, dir, (try sys.z(&zn, name)).ptr });
        \\    done = true;
        \\    if (opts.sync_parent) try sys.fsync(dir);
        \\}
    ;
    step = replaceOnce(a, step,
        \\pub fn atomicWriteAt(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32) (Allocator.Error || sys.Error)!void {
    , at_opts ++ "pub fn atomicWriteAtRemoved(") catch |e| return e;
    const stub = std.mem.indexOf(u8, step, "pub fn atomicWriteAtRemoved(") orelse return error.Missing;
    const tail = std.mem.indexOfPos(u8, step, stub, "    try sys.fsync(dir);\n}\n") orelse return error.Missing;
    const end = tail + "    try sys.fsync(dir);\n}\n".len;
    const trimmed = try a.alloc(u8, step.len - (end - stub));
    @memcpy(trimmed[0..stub], step[0..stub]);
    @memcpy(trimmed[stub..], step[end..]);
    return rewriteDiscard(a, trimmed);
}

fn rewriteFresh(a: std.mem.Allocator, src: []const u8) ![]const u8 {
    var step = src;
    if (std.mem.indexOf(u8, step, "pub var skip_listed_attribute") == null) {
        const with_pub = replaceOnce(a, step, "fn readAllAcls(", "pub fn readAllAcls(") catch |e| return e;
        const with_skip = replaceOnce(a, with_pub, "        if (name.len == 0) continue;\n", "        if (name.len == 0) continue;\n        if (skip_listed_attribute) |skip| if (std.mem.eql(u8, name, skip)) continue;\n") catch |e| return e;
        const decl =
            \\/// One system attribute omitted from the list for the current read.
            \\/// A directory read sets this to `system.posix_acl_default` and
            \\/// clears it when the read returns. Null lists every name.
            \\pub var skip_listed_attribute: ?[]const u8 = null;
            \\
            \\
        ;
        step = replaceOnce(a, with_skip, "fn listedAcls(", decl ++ "fn listedAcls(") catch |e| return e;
    }
    if (std.mem.indexOf(u8, step, "pub fn xattrGet(") == null) {
        step = replaceOnce(a, step, "fn xattrGet(", "pub fn xattrGet(") catch |e| return e;
    }
    step = replaceOnce(a, step,
        \\    step: []const u8 = "write",
        \\};
    ,
        \\    step: []const u8 = "write",
        \\    /// When false, the parent directory is not flushed. The caller
        \\    /// flushes it after a held directory's access list is restored.
        \\    sync_parent: bool = true,
        \\    /// When true, close the new file before the rename and refuse a
        \\    /// failed close. A lost write is a failed update.
        \\    check_close: bool = false,
        \\};
    ) catch |e| return e;
    const close_checked =
        \\pub fn closeChecked(fd: sys.fd_t) sys.Error!void {
        \\    const rc = linux.close(fd);
        \\    const errno = linux.errno(rc);
        \\    if (errno == .SUCCESS) return;
        \\    // Linux releases the descriptor before returning EINTR, so this
        \\    // close is not retried. sys.result hides EINTR, and sys.describe
        \\    // then prints Unexpected as EIO. Keep the errno for the caller.
        \\    sys.last_errno = errno;
        \\    if (errno == .INTR) return error.Unexpected;
        \\    _ = sys.result(rc) catch |err| return err;
        \\}
        \\
        \\
    ;
    step = replaceOnce(a, step, "/// atomic_write: replace `path` only after its bytes and access metadata\n", close_checked ++ "/// atomic_write: replace `path` only after its bytes and access metadata\n") catch |e| return e;
    step = replaceOnce(a, step,
        \\        defer sys.close(fd);
        \\        writeTemp(fd, data) catch |err| return failOs(a, why, err, null, null);
    ,
        \\        var closed = false;
        \\        defer if (!closed) sys.close(fd);
        \\        writeTemp(fd, data) catch |err| return failOs(a, why, err, null, null);
    ) catch |e| return e;
    step = replaceOnce(a, step,
        \\        _ = op(ops.fsync, .{fd}) catch |err| return failOs(a, why, err, null, null);
        \\    }
    ,
        \\        _ = op(ops.fsync, .{fd}) catch |err| return failOs(a, why, err, null, null);
        \\        if (opts.check_close) {
        \\            closed = true;
        \\            closeChecked(fd) catch |err| return failOs(a, why, err, path, null);
        \\        }
        \\    }
    ) catch |e| return e;
    step = replaceOnce(a, step,
        \\    renameTemp(tmp, path) catch |err| return failOs(a, why, err, tmp, path);
        \\    renamed = true;
        \\    // Opening the directory names it. Flushing the descriptor names no file.
        \\    const directory = sys.openat(fsx.cwd, parent, .{}, 0) catch |err| return failOs(a, why, err, parent, null);
        \\    defer sys.close(directory);
        \\    sys.fsync(directory) catch |err| return failOs(a, why, err, null, null);
        \\}
    ,
        \\    renameTemp(tmp, path) catch |err| return failOs(a, why, err, tmp, path);
        \\    renamed = true;
        \\    if (!opts.sync_parent) return;
        \\    // Opening the directory names it. Flushing the descriptor names no file.
        \\    const directory = sys.openat(fsx.cwd, parent, .{}, 0) catch |err| return failOs(a, why, err, parent, null);
        \\    defer sys.close(directory);
        \\    sys.fsync(directory) catch |err| return failOs(a, why, err, null, null);
        \\}
    ) catch |e| return e;
    const at_opts =
        \\pub const AtOptions = struct {
        \\    /// When false, `dir` is not flushed. The caller flushes it after
        \\    /// a held directory's access list is restored.
        \\    sync_parent: bool = true,
        \\    /// When true, close the new file before the rename and refuse a
        \\    /// failed close. A lost write is a failed update.
        \\    check_close: bool = false,
        \\};
        \\
        \\pub fn atomicWriteAt(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32) (Allocator.Error || sys.Error)!void {
        \\    return atomicWriteAtOpts(a, dir, name, data, mode, .{});
        \\}
        \\
        \\pub fn atomicWriteAtOpts(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32, opts: AtOptions) (Allocator.Error || sys.Error)!void {
        \\    sys.last_errno = .SUCCESS;
        \\    var rnd: [4]u8 = undefined;
        \\    _ = try sys.call(linux.getrandom, .{ &rnd, rnd.len, 0 });
        \\    const tmp = try a.print(".{s}.{d}.{x:0>8}.tmp", .{ name, linux.getpid(), std.mem.readInt(u32, &rnd, .little) });
        \\    defer a.free(tmp);
        \\    var zb: [sys.path_max:0]u8 = undefined;
        \\    const ztmp = try sys.z(&zb, tmp);
        \\    const flags: sys.O = .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true, .CLOEXEC = true };
        \\    const fd: sys.fd_t = @intCast(try op(ops.openat, .{ dir, ztmp.ptr, flags, @as(linux.mode_t, 0o600) }));
        \\    const created = fsx.fstat(fd) catch |err| {
        \\        sys.close(fd);
        \\        return err;
        \\    };
        \\    var done = false;
        \\    var closed = false;
        \\    defer {
        \\        if (!closed) sys.close(fd);
        \\        if (!done) discard(dir, tmp, created);
        \\    }
        \\    // openat applies the umask to the create, so the exact mode the
        \\    // caller names is set on the descriptor before anything is written
        \\    _ = try op(ops.fchmod, .{ fd, mode });
        \\    try writeTemp(fd, data);
        \\    _ = try op(ops.fsync, .{fd});
        \\    if (opts.check_close) {
        \\        closed = true;
        \\        try closeChecked(fd);
        \\    }
        \\    var zn: [sys.path_max:0]u8 = undefined;
        \\    _ = try op(ops.renameat, .{ dir, ztmp.ptr, dir, (try sys.z(&zn, name)).ptr });
        \\    done = true;
        \\    if (opts.sync_parent) try sys.fsync(dir);
        \\}
    ;
    step = replaceOnce(a, step,
        \\pub fn atomicWriteAt(a: Allocator, dir: sys.fd_t, name: []const u8, data: []const u8, mode: u32) (Allocator.Error || sys.Error)!void {
    , at_opts ++ "pub fn atomicWriteAtRemoved(") catch |e| return e;
    // The original body follows the renamed stub. Drop it through its last
    // flush, which is the only `try sys.fsync(dir)` in the file.
    const stub = std.mem.indexOf(u8, step, "pub fn atomicWriteAtRemoved(") orelse return error.Missing;
    const tail = std.mem.indexOfPos(u8, step, stub, "    try sys.fsync(dir);\n}\n") orelse return error.Missing;
    const end = tail + "    try sys.fsync(dir);\n}\n".len;
    const trimmed = try a.alloc(u8, step.len - (end - stub));
    @memcpy(trimmed[0..stub], step[0..stub]);
    @memcpy(trimmed[stub..], step[end..]);
    return rewriteDiscard(a, trimmed);
}

fn upgradeDiscard(a: std.mem.Allocator, src: []const u8) ![]const u8 {
    const close_checked =
        \\pub fn closeChecked(fd: sys.fd_t) sys.Error!void {
        \\    const rc = linux.close(fd);
        \\    const errno = linux.errno(rc);
        \\    if (errno == .SUCCESS) return;
        \\    // Linux releases the descriptor before returning EINTR, so this
        \\    // close is not retried. sys.result hides EINTR, and sys.describe
        \\    // then prints Unexpected as EIO. Keep the errno for the caller.
        \\    sys.last_errno = errno;
        \\    if (errno == .INTR) return error.Unexpected;
        \\    _ = sys.result(rc) catch |err| return err;
        \\}
    ;
    const with_close = replaceOnce(a, src,
        \\fn closeChecked(fd: sys.fd_t) sys.Error!void {
        \\    const rc = linux.close(fd);
        \\    const mapped = sys.result(rc) catch |err| return err;
        \\    if (mapped == null) {
        \\        sys.last_errno = .INTR;
        \\        return error.Unexpected;
        \\    }
        \\}
    , close_checked) catch |e| return e;
    const with_unlink = replaceOnce(a, with_close,
        \\    const fd: sys.fd_t = @intCast(try op(ops.openat, .{ dir, ztmp.ptr, flags, @as(linux.mode_t, 0o600) }));
        \\    var done = false;
        \\    var closed = false;
        \\    defer {
        \\        if (!closed) sys.close(fd);
        \\        if (!done) {
        \\            // The temporary name may already be gone. That failure is not
        \\            // the call the caller is reporting.
        \\            const saved = sys.last_errno;
        \\            defer sys.last_errno = saved;
        \\            sys.unlinkat(dir, tmp, 0) catch {};
        \\        }
        \\    }
    ,
        \\    const fd: sys.fd_t = @intCast(try op(ops.openat, .{ dir, ztmp.ptr, flags, @as(linux.mode_t, 0o600) }));
        \\    const created = fsx.fstat(fd) catch |err| {
        \\        sys.close(fd);
        \\        return err;
        \\    };
        \\    var done = false;
        \\    var closed = false;
        \\    defer {
        \\        if (!closed) sys.close(fd);
        \\        if (!done) discard(dir, tmp, created);
        \\    }
    ) catch |e| return e;
    return rewriteDiscard(a, with_unlink);
}

fn rewriteDiscard(a: std.mem.Allocator, step: []const u8) ![]const u8 {
    const with_discard = replaceOnce(a, step,
        \\fn discard(tmp: []const u8, created: fsx.Stat) void {
        \\    // stat or unlink can fail when the temporary name was removed
        \\    // already. That failure is not the call the caller is reporting.
        \\    const saved = sys.last_errno;
        \\    defer sys.last_errno = saved;
        \\    const cur = fsx.statAt(fsx.cwd, tmp, false) catch return;
        \\    if (cur.dev == created.dev and cur.ino == created.ino) sys.unlinkat(fsx.cwd, tmp, 0) catch {};
        \\}
    ,
        \\fn discard(dir: sys.fd_t, tmp: []const u8, created: fsx.Stat) void {
        \\    // stat or unlink can fail when the temporary name was removed
        \\    // already. That failure is not the call the caller is reporting.
        \\    const saved = sys.last_errno;
        \\    defer sys.last_errno = saved;
        \\    const cur = fsx.statAt(dir, tmp, false) catch return;
        \\    if (cur.dev == created.dev and cur.ino == created.ino) sys.unlinkat(dir, tmp, 0) catch {};
        \\}
    ) catch |e| return e;
    const with_call = replaceOnce(a, with_discard, "errdefer if (!renamed) discard(tmp, created);", "errdefer if (!renamed) discard(fsx.cwd, tmp, created);") catch |e| return e;
    return replaceOnce(a, with_call, "    const text = sys.describe(err);\n",
        \\    const text = if (err == error.Unexpected and sys.last_errno == .INTR)
        \\        sys.describeErrno(.INTR)
        \\    else
        \\        sys.describe(err);
        \\
    ) catch |e| return e;
}

fn replaceOnce(a: std.mem.Allocator, src: []const u8, old: []const u8, new: []const u8) ![]u8 {
    const at = std.mem.indexOf(u8, src, old) orelse return error.Missing;
    const out = try a.alloc(u8, src.len - old.len + new.len);
    @memcpy(out[0..at], src[0..at]);
    @memcpy(out[at..][0..new.len], new);
    @memcpy(out[at + new.len ..], src[at + old.len ..]);
    return out;
}

