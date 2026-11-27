//! Rewrite a generation install prefix to the stable prefix inside a staged
//! tree. installpath and userns record the generation directory; publication
//! renames that directory, so the bytes have to name the stable path first.
//! Text grows or shrinks. A binary's C string is shortened in place, which
//! keeps an ELF string table's following entries where they are. The bytes
//! are published by renaming a finished temporary file, so a crash does not
//! tear the original, a symbolic link is not followed, and a file that is
//! not owner-writable is not chmod'd. A later rewrite finishes a replacement
//! left in `.<inode>.ebsr-ready` and drops `.ebsr-link` and `.ebsr-symlink`
//! temporaries, so a killed hard-link publish can be repeated. The completion
//! stamp is left alone.
const std = @import("std");
const builtin = @import("builtin");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");

const sys = common.sys;
const fsx = common.fsx;
const oom = @import("oom.zig").oom;
const Allocator = std.mem.Allocator;

/// Refused, with the reason in the caller's `why`, or out of memory.
pub const Error = error{ Refused, OutOfMemory };

fn pathChar(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '+' or c == '-';
}

const skip_name = ".eb-staged-rebuild-complete";

/// A prefix to rewrite: `old`, the generation prefix the build recorded,
/// and `new`, the stable prefix.
pub const Pair = struct { old: []const u8, new: []const u8 };

fn partsOf(a: Allocator, path: []const u8) []const []const u8 {
    var list: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, path, '/');
    while (it.next()) |seg| if (seg.len > 0) list.append(a, seg) catch oom();
    return list.items;
}

/// prefix_pair: the generation prefix and the stable prefix of one install.
pub fn prefixPair(a: Allocator, staged: []const u8, live: []const u8, why: *[]const u8) Error!Pair {
    const staged_abs = admin.abspath(a, staged);
    const live_abs = admin.abspath(a, live);
    const sp = partsOf(a, staged_abs);
    const lp = partsOf(a, live_abs);
    var shared: usize = 0;
    while (shared < sp.len and shared < lp.len and std.mem.eql(u8, sp[sp.len - 1 - shared], lp[lp.len - 1 - shared])) shared += 1;
    if (shared == 0) {
        why.* = try a.print("staged path {s} and live path {s} share no directory suffix", .{ staged, live });
        return error.Refused;
    }
    const old = try std.fmt.allocPrint(a, "/{s}", .{try std.mem.join(a, "/", sp[0 .. sp.len - shared])});
    const new = try std.fmt.allocPrint(a, "/{s}", .{try std.mem.join(a, "/", lp[0 .. lp.len - shared])});
    if (old.len <= 1 or new.len <= 1) {
        why.* = "refusing to rewrite an empty install prefix";
        return error.Refused;
    }
    return .{ .old = old, .new = new };
}

/// replace_prefix. The inserted text is not scanned again.
pub fn replacePrefix(a: Allocator, data: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!struct { bytes: []u8, count: usize } {
    if (std.mem.eql(u8, old, new)) return .{ .bytes = try a.dupe(u8, data), .count = 0 };
    if (old.len == 0) {
        why.* = "refusing to rewrite an empty install prefix";
        return error.Refused;
    }
    if (std.mem.findAny(u8, new, "\n\"\x00") != null) {
        why.* = "refusing to write a prefix that contains a quote, newline, or NUL";
        return error.Refused;
    }
    var out: std.ArrayList(u8) = .empty;
    var count: usize = 0;
    var index: usize = 0;
    while (index < data.len) {
        if (std.mem.startsWith(u8, data[index..], old)) {
            const after = index + old.len;
            const left_ok = index == 0 or !pathChar(data[index - 1]);
            const right_ok = after >= data.len or !pathChar(data[after]);
            if (left_ok and right_ok) {
                try out.appendSlice(a, new);
                index = after;
                count += 1;
                continue;
            }
        }
        try out.append(a, data[index]);
        index += 1;
    }
    return .{ .bytes = out.items, .count = count };
}

fn rewriteBinary(a: Allocator, data: []const u8, old: []const u8, new: []const u8, path: []const u8, why: *[]const u8) Error![]u8 {
    const buf = try a.dupe(u8, data);
    var begin: usize = 0;
    while (begin < buf.len) {
        const end = std.mem.indexOfScalarPos(u8, buf, begin, 0) orelse buf.len;
        const updated = try replacePrefix(a, buf[begin..end], old, new, why);
        if (updated.count > 0 and updated.bytes.len > (end - begin)) {
            why.* = try a.print("{s} records the generation path in a binary, and the stable prefix is longer than that string", .{path});
            return error.Refused;
        }
        if (updated.count > 0 and end == buf.len and updated.bytes.len != (end - begin)) {
            why.* = try a.print("{s} records the generation path in a binary that has no room to shorten it", .{path});
            return error.Refused;
        }
        if (updated.count > 0 and updated.bytes.len == (end - begin)) {
            @memcpy(buf[begin..end], updated.bytes);
        } else if (updated.count > 0 and updated.bytes.len < (end - begin)) {
            @memcpy(buf[begin .. begin + updated.bytes.len], updated.bytes);
            buf[begin + updated.bytes.len] = 0;
            @memset(buf[begin + updated.bytes.len + 1 .. end], 0);
        }
        if (end == buf.len) break;
        begin = end + 1;
    }
    return buf;
}

const atomic = common.atomic;
const faultmount = if (builtin.is_test) @import("faultmount.zig") else struct {};
const max_bytes: usize = 1 << 30;

fn chmodFd(fd: fsx.fd_t, mode: u32) linux.E {
    while (true) {
        const failed = linux.errno(atomic.ops.fchmod(fd, mode));
        if (failed != .INTR) return failed;
    }
}

fn xattrSet(fd: fsx.fd_t, name: [:0]const u8, value: []const u8) linux.E {
    while (true) {
        const failed = linux.errno(atomic.ops.fsetxattr(fd, name, value));
        if (failed != .INTR) return failed;
    }
}

fn xattrRemove(fd: fsx.fd_t, name: [:0]const u8) linux.E {
    while (true) {
        const failed = linux.errno(atomic.ops.fremovexattr(fd, name));
        if (failed != .INTR) return failed;
    }
}

fn fsyncFd(fd: fsx.fd_t) linux.E {
    while (true) {
        const failed = linux.errno(atomic.ops.fsync(fd));
        if (failed != .INTR) return failed;
    }
}

fn sameStat(x: fsx.Stat, y: fsx.Stat) bool {
    return x.dev == y.dev and x.ino == y.ino and x.uid == y.uid and x.gid == y.gid and x.mode == y.mode and x.ctime.sec == y.ctime.sec and x.ctime.nsec == y.ctime.nsec;
}

fn takeReadError(a: Allocator, inner: *atomic.Why, err: atomic.Error, fallback: []const u8, why: *[]const u8) Error {
    if (err == error.OutOfMemory) {
        inner.deinit(a);
        return error.OutOfMemory;
    }
    if (inner.msg.len > 0) {
        why.* = inner.msg;
        inner.msg = "";
        return error.Refused;
    }
    inner.deinit(a);
    why.* = fallback;
    return error.Refused;
}

/// One pass over the three access lists. `system.posix_acl_default` is a
/// directory default, which a mode change does not replace, so that one
/// name is left out of this call. `readAllAcls` skips an EINVAL from an
/// unlisted NFSv4 name when the POSIX access list is listed.
fn readFormsOnce(a: Allocator, fd: fsx.fd_t, directory: []const u8, why: *[]const u8) Error!atomic.Acls {
    const previous = atomic.skip_listed_attribute;
    atomic.skip_listed_attribute = "system.posix_acl_default";
    defer atomic.skip_listed_attribute = previous;
    var inner: atomic.Why = .{};
    const acls = atomic.readAllAcls(a, fd, &inner) catch |err| {
        if (err != error.OutOfMemory and err != error.Refused) {
            inner.deinit(a);
            why.* = main_mod.osError(a, err, directory);
            return error.Refused;
        }
        return takeReadError(a, &inner, err, "cannot read access metadata", why);
    };
    inner.deinit(a);
    return acls;
}

fn readDirectoryForms(a: Allocator, fd: fsx.fd_t, directory: []const u8, why: *[]const u8) Error!atomic.Acls {
    const before = fsx.fstat(fd) catch |err| return fail(a, why, err, directory);
    const acls = try readFormsOnce(a, fd, directory, why);
    errdefer acls.deinit(a);
    const again = try readFormsOnce(a, fd, directory, why);
    defer again.deinit(a);
    const after = fsx.fstat(fd) catch |err| return fail(a, why, err, directory);
    if (!acls.eql(again) or !sameStat(after, before)) {
        why.* = "access metadata changed during inspection";
        return error.Refused;
    }
    return acls;
}

/// The three access lists this repository preserves, read twice. A directory
/// default list is the one name that read skips; any other refusal stands.
fn readDirectoryAccess(a: Allocator, fd: fsx.fd_t, directory: []const u8, why: *[]const u8) Error!atomic.Acls {
    return readDirectoryForms(a, fd, directory, why);
}

fn readFileAcls(a: Allocator, fd: fsx.fd_t, why: *[]const u8) Error!atomic.Acls {
    var inner: atomic.Why = .{};
    if (atomic.readAcls(a, fd, &inner)) |acls| {
        inner.deinit(a);
        return acls;
    } else |err| return takeReadError(a, &inner, err, "cannot read access metadata", why);
}

fn refuseAccess(a: Allocator, failed: linux.E, path: []const u8, directory: bool, why: *[]const u8) Error {
    why.* = if (directory) main_mod.errnoPath(a, failed, path) else "cannot preserve access control of a rewritten file";
    return error.Refused;
}

/// Write `want` onto `fd`. A directory reports a failed set or remove with
/// the path; a rewritten file keeps its own refusal text.
fn restoreLists(a: Allocator, fd: fsx.fd_t, want: atomic.Acls, path: []const u8, directory: bool, why: *[]const u8) Error!void {
    const now = if (directory) try readDirectoryAccess(a, fd, path, why) else try readFileAcls(a, fd, why);
    defer now.deinit(a);
    if (!now.eql(want)) {
        for (want.present, 0..) |present, i| if (present) |value| {
            const failed = xattrSet(fd, atomic.acl_attributes[i], value);
            if (failed != .SUCCESS) return refuseAccess(a, failed, path, directory, why);
            break;
        };
    }
    for (0..atomic.acl_attributes.len) |_| {
        const present = if (directory) try readDirectoryAccess(a, fd, path, why) else try readFileAcls(a, fd, why);
        defer present.deinit(a);
        var extra: ?usize = null;
        for (present.present, want.present, 0..) |have, wanted, i| if (have != null and wanted == null) {
            extra = i;
            break;
        };
        const i = extra orelse break;
        const failed = xattrRemove(fd, atomic.acl_attributes[i]);
        if (failed != .SUCCESS) return refuseAccess(a, failed, path, directory, why);
    }
    const after = if (directory) try readDirectoryAccess(a, fd, path, why) else try readFileAcls(a, fd, why);
    defer after.deinit(a);
    if (!after.eql(want)) {
        why.* = if (directory) "the access list of a sealed directory did not round-trip" else "access metadata of a rewritten file did not round-trip";
        return error.Refused;
    }
}

/// A directory that was not owner-writable, opened and made so for one
/// update. `restore` writes the access lists, puts the mode back, and
/// writes the lists again: a mode change replaces them. The mode and lists
/// are journaled beside the directory before the write bit is added. The
/// directory is flushed only after that restore. Flushing it earlier would
/// make the write bit and the replaced list durable.
pub const DirectoryHold = struct {
    fd: fsx.fd_t,
    a: Allocator,
    mode: u32,
    acls: atomic.Acls,
    /// Path of the journal written before the write bit was added.
    seal: []const u8 = "",
    pending: bool = true,
    /// False once the descriptor, the lists, and the journal path are released.
    live: bool = true,

    pub fn restore(self: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
        if (!self.pending) return;
        // The lists go back while the write bit is still set. The mode
        // change that clears the bit replaces them, so they are written
        // again afterwards.
        try restoreLists(self.a, self.fd, self.acls, directory, true, why);
        const failed = chmodFd(self.fd, self.mode);
        if (failed != .SUCCESS) {
            why.* = main_mod.errnoPath(self.a, failed, directory);
            return error.Refused;
        }
        try restoreLists(self.a, self.fd, self.acls, directory, true, why);
        const st = fsx.fstat(self.fd) catch |err| return fail(self.a, why, err, directory);
        if (st.perm() != self.mode) {
            why.* = "the mode of a sealed directory did not round-trip";
            return error.Refused;
        }
        self.pending = false;
    }

    /// Drop the descriptor without reporting its close. Error paths use this
    /// so a failed update still releases the descriptor.
    fn abandon(self: *DirectoryHold) void {
        if (!self.live) return;
        self.live = false;
        self.acls.deinit(self.a);
        self.acls = .{};
        if (self.seal.len > 0) self.a.free(self.seal);
        self.seal = "";
        if (self.fd >= 0) sys.close(self.fd);
        self.fd = -1;
    }

    /// Release the descriptor and report a failed close. Linux frees the
    /// descriptor even when close fails, so the error is the result of the
    /// update, not a reason to close again. EINTR stays EINTR: `sys.describe`
    /// would print that `Unexpected` as EIO.
    fn deinit(self: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
        if (!self.live) return;
        self.live = false;
        const fd = self.fd;
        self.fd = -1;
        const acls = self.acls;
        self.acls = .{};
        const seal = self.seal;
        self.seal = "";
        defer {
            acls.deinit(self.a);
            if (seal.len > 0) self.a.free(seal);
        }
        if (fd >= 0) atomic.closeChecked(fd) catch |err| return fail(self.a, why, err, directory);
    }

    fn closeFd(self: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
        if (self.fd < 0) return;
        const fd = self.fd;
        self.fd = -1;
        atomic.closeChecked(fd) catch |err| return fail(self.a, why, err, directory);
    }

    /// Writes the mode and access lists back when they are still pending
    /// and releases the descriptor. The directory is flushed by the caller
    /// after this returns. A failed close is `error.Refused`.
    pub fn close(self: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
        errdefer self.abandon();
        if (self.pending) try self.restore(directory, why);
        try self.deinit(directory, why);
    }

    fn discardSeal(self: *DirectoryHold, why: *[]const u8) Error!void {
        if (self.seal.len == 0) return;
        const parent = std.fs.path.dirname(self.seal) orelse ".";
        const name = std.fs.path.basename(self.seal);
        try dropSealName(self.a, parent, name, why);
    }
};

/// Flush `directory` after its journal, when one exists, has been restored.
/// A raw flush would make the replaced access list durable. The journal is
/// applied even when a child journal is already gone.
pub fn flushSealed(a: Allocator, directory: []const u8, why: *[]const u8) Error!void {
    if (try loadSeal(a, directory, why)) |got| {
        got.acls.deinit(a);
        a.free(got.path);
        var held = (try holdDirectory(a, directory, why)) orelse return fail(a, why, error.AccessDenied, directory);
        return finishHold(&held, directory, why);
    }
    try flushDirectory(a, directory, why);
}

/// Restore the mode and access list, flush `directory`, and close it.
/// The journal is removed only after that close. A failed flush or a
/// failed close leaves the journal so a retry can restore again.
pub fn finishHold(hold: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
    errdefer hold.abandon();
    try hold.restore(directory, why);
    const synced = fsyncFd(hold.fd);
    if (synced != .SUCCESS) {
        why.* = main_mod.errnoPath(hold.a, synced, directory);
        return error.Refused;
    }
    try hold.closeFd(directory, why);
    try hold.discardSeal(why);
    try hold.deinit(directory, why);
}

/// Flush `directory` after `DirectoryHold.close` has restored it. A failed
/// flush or a failed close is `error.Refused`: the restored mode and access
/// list are not durable until both succeed. EINTR from the close is reported
/// as an interrupted call, not as EIO.
pub fn flushDirectory(a: Allocator, directory: []const u8, why: *[]const u8) Error!void {
    const fd = sys.openat(fsx.cwd, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, directory);
    const synced = fsyncFd(fd);
    if (synced != .SUCCESS) {
        sys.close(fd);
        why.* = main_mod.errnoPath(a, synced, directory);
        return error.Refused;
    }
    atomic.closeChecked(fd) catch |err| return fail(a, why, err, directory);
}

fn heldDirectory(defer_dir: ?[]const u8, parent: []const u8) bool {
    const held = defer_dir orelse return false;
    return std.mem.eql(u8, held, parent);
}

/// When `directory` is not owner-writable, journal its mode and access
/// lists, then make it owner-writable. A directory that is already
/// owner-writable is held too when that journal is present, so a retry
/// restores the recorded mode and lists instead of flushing the replaced
/// list. Null when it is already owner-writable and no journal is present.
pub fn holdDirectory(a: Allocator, directory: []const u8, why: *[]const u8) Error!?DirectoryHold {
    const fd = sys.openat(fsx.cwd, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, directory);
    return holdDirectoryFd(a, fd, directory, why);
}

fn holdDirectoryFd(a: Allocator, fd: sys.fd_t, directory: []const u8, why: *[]const u8) Error!?DirectoryHold {
    const info = fsx.fstat(fd) catch |err| {
        sys.close(fd);
        return fail(a, why, err, directory);
    };
    if (!info.isDir()) {
        sys.close(fd);
        why.* = "a sealed directory is not a directory";
        return error.Refused;
    }
    const mode = info.perm();
    // A journal is the original mode and lists. Apply it before a new hold
    // or a flush, whether or not the write bit is still set. The live lists
    // are not written over it until that restore has been flushed.
    const recorded = loadSeal(a, directory, why) catch |err| {
        sys.close(fd);
        return err;
    };
    if (recorded) |got| {
        if (mode & 0o200 == 0) {
            // The journal is already on disk. Flush the parent before the
            // mode change so a crash cannot keep the new mode and lose it.
            fsyncParentPath(a, directory, why) catch |err| {
                got.acls.deinit(a);
                a.free(got.path);
                sys.close(fd);
                return err;
            };
            const failed = chmodFd(fd, mode | 0o300);
            if (failed != .SUCCESS) {
                got.acls.deinit(a);
                a.free(got.path);
                sys.close(fd);
                why.* = main_mod.errnoPath(a, failed, directory);
                return error.Refused;
            }
        }
        return .{ .fd = fd, .a = a, .mode = got.mode, .acls = got.acls, .seal = got.path };
    }
    if (mode & 0o200 != 0) {
        sys.close(fd);
        return null;
    }
    const acls = readDirectoryAccess(a, fd, directory, why) catch |err| {
        sys.close(fd);
        return err;
    };
    const seal = persistSeal(a, directory, mode, acls, why) catch |err| {
        acls.deinit(a);
        sys.close(fd);
        return err;
    };
    fsyncParentPath(a, directory, why) catch |err| {
        acls.deinit(a);
        sys.close(fd);
        sys.unlinkat(fsx.cwd, seal, 0) catch {};
        a.free(seal);
        return err;
    };
    const failed = chmodFd(fd, mode | 0o300);
    if (failed != .SUCCESS) {
        acls.deinit(a);
        sys.close(fd);
        sys.unlinkat(fsx.cwd, seal, 0) catch {};
        a.free(seal);
        why.* = main_mod.errnoPath(a, failed, directory);
        return error.Refused;
    }
    return .{ .fd = fd, .a = a, .mode = mode, .acls = acls, .seal = seal };
}

/// Put the journal of `recorded` back onto `directory` (the same directory,
/// or the name it has after a rename) and flush that directory. The journal
/// stays, so the caller can flush the destination parent before the source
/// parent loses the journal name. Nothing happens when the journal is absent.
fn applyRecorded(a: Allocator, directory: []const u8, recorded: []const u8, why: *[]const u8) Error!void {
    const got = (try loadSeal(a, recorded, why)) orelse return;
    const fd = sys.openat(fsx.cwd, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| {
        got.acls.deinit(a);
        a.free(got.path);
        return fail(a, why, err, directory);
    };
    var hold: DirectoryHold = .{ .fd = fd, .a = a, .mode = got.mode, .acls = got.acls, .seal = got.path };
    errdefer hold.abandon();
    try hold.restore(directory, why);
    const synced = fsyncFd(hold.fd);
    if (synced != .SUCCESS) {
        why.* = main_mod.errnoPath(a, synced, directory);
        return error.Refused;
    }
    try hold.deinit(directory, why);
}

/// Put the journal of `recorded` back onto `directory` (the same directory,
/// or the name it has after a rename) and flush that directory. The journal
/// stays, so the caller can flush the destination parent before the source
/// parent loses the journal name. Nothing happens when the journal is absent.
pub fn restoreRecorded(a: Allocator, directory: []const u8, recorded: []const u8, why: *[]const u8) Error!void {
    try applyRecorded(a, directory, recorded, why);
}

/// Unlink nested seals while `directory` is still writable, then flush the
/// inode, the destination parent, and the source parent, in that order. A
/// parent flush restores that parent's journal first. The walk, the restore,
/// and the flush use the descriptor already held: closing the path and
/// opening it again can name a different directory.
pub fn finishRenamed(hold: *DirectoryHold, directory: []const u8, dest_parent: []const u8, why: *[]const u8) Error!void {
    errdefer hold.abandon();
    try releaseNestedFrom(hold, directory, why);
    try restoredFlush(hold, directory, why);
    try flushSealed(hold.a, dest_parent, why);
    try hold.discardSeal(why);
    try hold.deinit(directory, why);
}

/// The rename already happened. Unlink nested seals while `directory` is
/// still writable, restore the recorded mode and list, then flush the inode,
/// the destination parent, and the source parent. A missing child journal
/// still restores those parents. The directory is one descriptor from the
/// open through the flush.
pub fn commitRecorded(a: Allocator, directory: []const u8, recorded: []const u8, dest_parent: []const u8, why: *[]const u8) Error!void {
    const fd = sys.openat(fsx.cwd, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, directory);
    const got = loadSeal(a, recorded, why) catch |err| {
        sys.close(fd);
        return err;
    };
    var hold: DirectoryHold = .{ .fd = fd, .a = a, .mode = 0, .acls = .{}, .pending = false };
    if (got) |seal| {
        hold.mode = seal.mode;
        hold.acls = seal.acls;
        hold.seal = seal.path;
        hold.pending = true;
    }
    errdefer hold.abandon();
    try releaseNestedFrom(&hold, directory, why);
    try restoredFlush(&hold, directory, why);
    try flushSealed(a, dest_parent, why);
    try hold.deinit(directory, why);
    try discardRecorded(a, recorded, why);
}

/// Restore `hold` when a journal is pending, flush that descriptor, and
/// close it. The journal stays. A later open of `directory` has to name the
/// same inode: a close can make the path a different directory, and the
/// update refuses instead of dropping the journal.
fn restoredFlush(hold: *DirectoryHold, directory: []const u8, why: *[]const u8) Error!void {
    const before = fsx.fstat(hold.fd) catch |err| return fail(hold.a, why, err, directory);
    if (hold.pending) try hold.restore(directory, why);
    const synced = fsyncFd(hold.fd);
    if (synced != .SUCCESS) {
        why.* = main_mod.errnoPath(hold.a, synced, directory);
        return error.Refused;
    }
    const parent = std.fs.path.dirname(directory) orelse ".";
    const name = std.fs.path.basename(directory);
    const pfd = sys.openat(fsx.cwd, parent, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(hold.a, why, err, parent);
    defer sys.close(pfd);
    try hold.closeFd(directory, why);
    const now = statOnce(pfd, name) catch |err| return fail(hold.a, why, err, directory);
    if (now.dev != before.dev or now.ino != before.ino) {
        why.* = "a sealed directory changed before its journal was removed";
        return error.Refused;
    }
}

/// Unlink the journal of `recorded` and flush the directory that held it.
/// A missing journal still restores that parent before the flush.
pub fn discardRecorded(a: Allocator, recorded: []const u8, why: *[]const u8) Error!void {
    const path = sealPath(a, recorded) catch return error.OutOfMemory;
    defer a.free(path);
    const parent = std.fs.path.dirname(path) orelse ".";
    const name = std.fs.path.basename(path);
    try dropSealName(a, parent, name, why);
}

fn sealedBase(name: []const u8) ?[]const u8 {
    const suffix = ".ebsr-seal";
    if (name.len < suffix.len + 1 or name[0] != '.') return null;
    if (!std.mem.endsWith(u8, name, suffix)) return null;
    return name[1 .. name.len - suffix.len];
}

fn sealBaseEscapes(base: []const u8) bool {
    return base.len == 0 or std.mem.eql(u8, base, ".") or std.mem.eql(u8, base, "..") or std.mem.indexOfScalar(u8, base, '/') != null;
}

fn writeSealAt(a: Allocator, dir: sys.fd_t, name: []const u8, body: []const u8, why: *[]const u8) Error!void {
    atomic.atomicWriteAtOpts(a, dir, name, body, 0o600, .{
        .sync_parent = false,
        .check_close = true,
    }) catch |err| return fail(a, why, err, name);
}

const chain_limit: usize = 32;
const root_parent: usize = std.math.maxInt(usize);

/// One statx. `fsx.statAt` retries EINTR forever, so a refused stat has to
/// use this when the errno itself is the result.
fn statOnce(dir: sys.fd_t, name: []const u8) sys.Error!struct { dev: u64, ino: u64 } {
    var buf: [sys.path_max:0]u8 = undefined;
    var x: linux.Statx = undefined;
    const mask: linux.STATX = .{ .TYPE = true, .MODE = true, .INO = true };
    const rc = linux.statx(dir, (try sys.z(&buf, name)).ptr, linux.AT.SYMLINK_NOFOLLOW, mask, &x);
    if (try sys.result(rc)) |_| {
        return .{
            .dev = (@as(u64, x.dev_major) << 32) | x.dev_minor,
            .ino = x.ino,
        };
    }
    sys.last_errno = .INTR;
    return error.Unexpected;
}

/// Flush the parent of `directory` through a descriptor, before a mode or
/// list change. The journal entry has to be durable before that change.
fn fsyncParentPath(a: Allocator, directory: []const u8, why: *[]const u8) Error!void {
    const parent = std.fs.path.dirname(directory) orelse return;
    if (parent.len == 0) return;
    const pfd = sys.openat(fsx.cwd, parent, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, parent);
    const synced = fsyncFd(pfd);
    if (synced != .SUCCESS) {
        sys.close(pfd);
        why.* = main_mod.errnoPath(a, synced, parent);
        return error.Refused;
    }
    atomic.closeChecked(pfd) catch |err| return fail(a, why, err, parent);
}

/// Flush `parent` after its own journal, when it has one, has been restored.
fn finishParentFd(a: Allocator, pfd: sys.fd_t, parent: []const u8, why: *[]const u8) Error!void {
    const recorded = loadSeal(a, parent, why) catch |err| {
        sys.close(pfd);
        return err;
    };
    if (recorded) |got| {
        got.acls.deinit(a);
        a.free(got.path);
        var held = (try holdDirectoryFd(a, pfd, parent, why)) orelse return;
        return finishHold(&held, parent, why);
    }
    const synced = fsyncFd(pfd);
    if (synced != .SUCCESS) {
        sys.close(pfd);
        why.* = main_mod.errnoPath(a, synced, parent);
        return error.Refused;
    }
    atomic.closeChecked(pfd) catch |err| return fail(a, why, err, parent);
}

/// Unlink `name` through `parent`'s descriptor and flush that directory.
/// A sealed parent is held for the unlink and sealed again afterwards.
fn dropSealName(a: Allocator, parent: []const u8, name: []const u8, why: *[]const u8) Error!void {
    const pfd = sys.openat(fsx.cwd, parent, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, parent);
    sys.unlinkat(pfd, name, 0) catch |err| switch (err) {
        error.NotFound => return finishParentFd(a, pfd, parent, why),
        error.AccessDenied => {
            var held = (try holdDirectoryFd(a, pfd, parent, why)) orelse return fail(a, why, error.AccessDenied, parent);
            sys.unlinkat(held.fd, name, 0) catch |uerr| switch (uerr) {
                error.NotFound => {},
                else => {
                    held.abandon();
                    return fail(a, why, uerr, parent);
                },
            };
            return finishHold(&held, parent, why);
        },
        else => {
            sys.close(pfd);
            return fail(a, why, err, parent);
        },
    };
    return finishParentFd(a, pfd, parent, why);
}

/// A directory the walk opened and kept. The journal, the mode change, and
/// the list change all use this descriptor.
const ChainDir = struct {
    fd: sys.fd_t,
    path: []const u8,
    name: []const u8,
    parent: usize,
    mode: u32 = 0o755,
    acls: atomic.Acls = .{},
    own_acls: bool = false,
    dev: u64 = 0,
    ino: u64 = 0,
    journal: bool = false,
    /// Direct children whose journal is on disk now.
    children: usize = 0,
    /// The write bit was added and the recorded list is not back yet.
    write_open: bool = false,
    unsynced: bool = false,
    anchor: bool = false,
    keep_journal: bool = false,
    owned_fd: bool = true,
};

const Chain = struct {
    a: Allocator,
    why: *[]const u8,
    dirs: std.ArrayList(ChainDir) = .empty,
    outside: sys.fd_t = -1,
    outside_path: []const u8 = "",
    outside_mode: u32 = 0o755,
    outside_acls: atomic.Acls = .{},
    outside_own: bool = false,
    outside_seal: []const u8 = "",
    outside_write_open: bool = false,
    outside_unsynced: bool = false,
    hold: ?*DirectoryHold = null,

    /// Drop descriptors an error left open. A finished walk has already
    /// close-checked each one, so a close error on that path is returned.
    fn deinit(self: *Chain) void {
        var i = self.dirs.items.len;
        while (i > 0) {
            i -= 1;
            const dir = &self.dirs.items[i];
            if (dir.own_acls) dir.acls.deinit(self.a);
            dir.acls = .{};
            dir.own_acls = false;
            if (dir.owned_fd and dir.fd >= 0) sys.close(dir.fd);
            dir.fd = -1;
            if (dir.path.len > 0) self.a.free(dir.path);
            dir.path = "";
            if (dir.name.len > 0) self.a.free(dir.name);
            dir.name = "";
        }
        self.dirs.deinit(self.a);
        if (self.outside_own) self.outside_acls.deinit(self.a);
        self.outside_acls = .{};
        self.outside_own = false;
        if (self.outside_seal.len > 0) self.a.free(self.outside_seal);
        self.outside_seal = "";
        if (self.outside_path.len > 0) self.a.free(self.outside_path);
        self.outside_path = "";
        if (self.outside >= 0) sys.close(self.outside);
        self.outside = -1;
    }

    fn parentFd(self: *Chain, idx: usize) sys.fd_t {
        const p = self.dirs.items[idx].parent;
        if (p == root_parent) return self.outside;
        if (p >= self.dirs.items.len) return -1;
        return self.dirs.items[p].fd;
    }

    fn markUnsynced(self: *Chain, idx: usize) void {
        const p = self.dirs.items[idx].parent;
        if (p == root_parent) self.outside_unsynced = true else self.dirs.items[p].unsynced = true;
    }

    fn readSeal(self: *Chain, dir: sys.fd_t, name: []const u8) Error!?struct { mode: u32, acls: atomic.Acls } {
        if (dir < 0) return null;
        const fd = sys.openat(dir, name, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0) catch |err| switch (err) {
            error.NotFound => return null,
            else => return fail(self.a, self.why, err, name),
        };
        defer sys.close(fd);
        const text = fsx.readAll(self.a, fd, 1 << 20) catch |err| return fail(self.a, self.why, err, name);
        defer self.a.free(text);
        const parsed = try parseSeal(self.a, text, self.why);
        return .{ .mode = parsed.mode, .acls = parsed.acls };
    }

    fn known(self: *Chain, parent: usize, name: []const u8) bool {
        for (self.dirs.items) |dir| if (dir.parent == parent and std.mem.eql(u8, dir.name, name)) return true;
        return false;
    }

    fn addChild(self: *Chain, parent_idx: usize, base: []const u8, from_seal: bool) Error!?usize {
        const pfd = self.dirs.items[parent_idx].fd;
        const parent_path = self.dirs.items[parent_idx].path;
        const opened = sys.openat(pfd, base, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| switch (err) {
            error.NotDir, error.NotFound, error.SymLink => {
                if (from_seal) return null;
                return fail(self.a, self.why, err, parent_path);
            },
            else => return fail(self.a, self.why, err, parent_path),
        };
        return self.appendOpened(parent_idx, base, opened);
    }

    /// `opened` is a directory under `parent_idx`. The chain owns it from
    /// here, including when this returns an error.
    fn appendOpened(self: *Chain, parent_idx: usize, base: []const u8, opened: sys.fd_t) Error!?usize {
        const pfd = self.dirs.items[parent_idx].fd;
        const parent_path = self.dirs.items[parent_idx].path;
        const st = fsx.fstat(opened) catch |err| {
            sys.close(opened);
            return fail(self.a, self.why, err, parent_path);
        };
        if (!st.isDir()) {
            sys.close(opened);
            return null;
        }
        const seal_name = std.fmt.allocPrint(self.a, ".{s}.ebsr-seal", .{base}) catch {
            sys.close(opened);
            return error.OutOfMemory;
        };
        defer self.a.free(seal_name);
        var journal = false;
        var mode = st.perm();
        var acls: atomic.Acls = .{};
        var own = false;
        if (self.readSeal(pfd, seal_name)) |parsed| {
            if (parsed) |got| {
                journal = true;
                mode = got.mode;
                acls = got.acls;
                own = true;
            }
        } else |err| {
            sys.close(opened);
            return err;
        }
        const name = self.a.dupe(u8, base) catch {
            if (own) acls.deinit(self.a);
            sys.close(opened);
            return error.OutOfMemory;
        };
        const path = std.fs.path.join(self.a, &.{ parent_path, base }) catch {
            self.a.free(name);
            if (own) acls.deinit(self.a);
            sys.close(opened);
            return error.OutOfMemory;
        };
        const write_open = journal and (st.perm() & 0o200 != 0) and (mode & 0o200 == 0);
        const idx = self.dirs.items.len;
        self.dirs.append(self.a, .{
            .fd = opened,
            .path = path,
            .name = name,
            .parent = parent_idx,
            .mode = mode,
            .acls = acls,
            .own_acls = own,
            .dev = st.dev,
            .ino = st.ino,
            .journal = journal,
            .write_open = write_open,
        }) catch {
            self.a.free(path);
            self.a.free(name);
            if (own) acls.deinit(self.a);
            sys.close(opened);
            return error.OutOfMemory;
        };
        if (journal) self.dirs.items[parent_idx].children += 1;
        return idx;
    }

    /// True when `dir` holds a journal for a child directory, or a
    /// descendant does. `dir` stays open. A directory this scan opens is
    /// closed again, so the scan keeps no descriptor of its own and does
    /// not count toward the sealed-chain limit.
    fn bearsJournal(self: *Chain, dir: sys.fd_t, path: []const u8) Error!bool {
        var names: std.ArrayList([]u8) = .empty;
        defer {
            for (names.items) |name| self.a.free(name);
            names.deinit(self.a);
        }
        var it = fsx.DirIter.init(self.a, dir) catch |err| return fail(self.a, self.why, err, path);
        defer it.deinit(self.a);
        while (true) {
            const entry = it.next() catch |err| return fail(self.a, self.why, err, path);
            const item = entry orelse break;
            if (item.name.len == 0 or std.mem.indexOfScalar(u8, item.name, '/') != null) {
                self.why.* = "a seal name escapes its directory";
                return error.Refused;
            }
            try names.append(self.a, try self.a.dupe(u8, item.name));
        }
        for (names.items) |name| {
            const base = sealedBase(name) orelse continue;
            if (sealBaseEscapes(base)) {
                self.why.* = "a seal name escapes its directory";
                return error.Refused;
            }
            const st = fsx.statAt(dir, base, false) catch |err| switch (err) {
                error.NotFound => continue,
                else => return fail(self.a, self.why, err, path),
            };
            if (st.isDir()) return true;
        }
        for (names.items) |name| {
            if (sealedBase(name) != null) continue;
            const st = fsx.statAt(dir, name, false) catch |err| switch (err) {
                error.NotFound => continue,
                else => return fail(self.a, self.why, err, path),
            };
            if (!st.isDir()) continue;
            const opened = sys.openat(dir, name, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| switch (err) {
                error.NotDir, error.NotFound, error.SymLink => continue,
                else => return fail(self.a, self.why, err, path),
            };
            const hit = self.bearsJournal(opened, path) catch |err| {
                sys.close(opened);
                return err;
            };
            sys.close(opened);
            if (hit) return true;
        }
        return false;
    }

    fn walk(self: *Chain, idx: usize, depth: usize) Error!void {
        if (depth > chain_limit) {
            self.why.* = "a sealed directory chain is longer than 32";
            return error.Refused;
        }
        const fd = self.dirs.items[idx].fd;
        const path = self.dirs.items[idx].path;
        var names: std.ArrayList([]u8) = .empty;
        defer {
            for (names.items) |name| self.a.free(name);
            names.deinit(self.a);
        }
        var it = fsx.DirIter.init(self.a, fd) catch |err| return fail(self.a, self.why, err, path);
        defer it.deinit(self.a);
        while (true) {
            const entry = it.next() catch |err| return fail(self.a, self.why, err, path);
            const item = entry orelse break;
            if (item.name.len == 0 or std.mem.indexOfScalar(u8, item.name, '/') != null) {
                self.why.* = "a seal name escapes its directory";
                return error.Refused;
            }
            try names.append(self.a, try self.a.dupe(u8, item.name));
        }
        var nested: std.ArrayList(usize) = .empty;
        defer nested.deinit(self.a);
        for (names.items) |name| {
            const base = sealedBase(name) orelse continue;
            if (sealBaseEscapes(base)) {
                self.why.* = "a seal name escapes its directory";
                return error.Refused;
            }
            if (try self.addChild(idx, base, true)) |child| try nested.append(self.a, child);
        }
        for (names.items) |name| {
            if (sealedBase(name) != null) continue;
            const st = fsx.statAt(fd, name, false) catch |err| switch (err) {
                error.NotFound => continue,
                else => return fail(self.a, self.why, err, path),
            };
            if (!st.isDir()) continue;
            if (self.known(idx, name)) continue;
            // No journal here and none under it: close this descriptor and
            // leave the directory out of the chain. A hit keeps this same
            // descriptor, so a later close is still the first close.
            const opened = sys.openat(fd, name, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| switch (err) {
                error.NotDir, error.NotFound, error.SymLink => continue,
                else => return fail(self.a, self.why, err, path),
            };
            const hit = self.bearsJournal(opened, path) catch |err| {
                sys.close(opened);
                return err;
            };
            if (!hit) {
                sys.close(opened);
                continue;
            }
            if (try self.appendOpened(idx, name, opened)) |child| try nested.append(self.a, child);
        }
        for (nested.items) |child| try self.walk(child, depth + 1);
    }

    fn build(self: *Chain, root_fd: sys.fd_t, root: []const u8, hold: ?*DirectoryHold) Error!void {
        self.hold = hold;
        const parent = std.fs.path.dirname(root) orelse ".";
        self.outside = sys.openat(fsx.cwd, parent, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(self.a, self.why, err, parent);
        self.outside_path = self.a.dupe(u8, parent) catch return error.OutOfMemory;
        if (loadSeal(self.a, parent, self.why)) |recorded| {
            if (recorded) |got| {
                self.outside_mode = got.mode;
                self.outside_acls = got.acls;
                self.outside_own = true;
                self.outside_seal = got.path;
                const live = fsx.fstat(self.outside) catch |err| return fail(self.a, self.why, err, parent);
                if (live.perm() & 0o200 != 0 and got.mode & 0o200 == 0) self.outside_write_open = true;
            }
        } else |err| return err;
        const st = fsx.fstat(root_fd) catch |err| return fail(self.a, self.why, err, root);
        const name = self.a.dupe(u8, std.fs.path.basename(root)) catch return error.OutOfMemory;
        const path = self.a.dupe(u8, root) catch {
            self.a.free(name);
            return error.OutOfMemory;
        };
        var journal = false;
        var mode = st.perm();
        var acls: atomic.Acls = .{};
        var own = false;
        if (hold) |h| {
            journal = h.seal.len > 0;
            mode = h.mode;
            acls = h.acls;
        } else {
            const seal_name = std.fmt.allocPrint(self.a, ".{s}.ebsr-seal", .{name}) catch {
                self.a.free(path);
                self.a.free(name);
                return error.OutOfMemory;
            };
            defer self.a.free(seal_name);
            if (self.readSeal(self.outside, seal_name)) |parsed| {
                if (parsed) |got| {
                    journal = true;
                    mode = got.mode;
                    acls = got.acls;
                    own = true;
                }
            } else |err| {
                self.a.free(path);
                self.a.free(name);
                return err;
            }
        }
        const live_write = st.perm() & 0o200 != 0;
        const write_open = if (hold) |h| h.pending and live_write else journal and live_write and (mode & 0o200 == 0);
        self.dirs.append(self.a, .{
            .fd = root_fd,
            .path = path,
            .name = name,
            .parent = root_parent,
            .mode = mode,
            .acls = acls,
            .own_acls = own,
            .dev = st.dev,
            .ino = st.ino,
            .journal = journal,
            .write_open = write_open,
            .anchor = hold != null,
            .keep_journal = hold != null,
            .owned_fd = hold == null,
        }) catch {
            if (own) acls.deinit(self.a);
            self.a.free(path);
            self.a.free(name);
            return error.OutOfMemory;
        };
        try self.walk(0, 1);
    }

    fn needsWrite(self: *Chain, idx: usize) bool {
        const dir = &self.dirs.items[idx];
        if (dir.children > 0) return true;
        if (dir.anchor) return false;
        return dir.journal;
    }

    fn writable(self: *Chain, idx: usize) Error!bool {
        const st = fsx.fstat(self.dirs.items[idx].fd) catch |err| return fail(self.a, self.why, err, self.dirs.items[idx].path);
        return st.perm() & 0o200 != 0;
    }

    fn kidsOf(self: *Chain, idx: usize) Error!std.ArrayList(usize) {
        var kids: std.ArrayList(usize) = .empty;
        errdefer kids.deinit(self.a);
        for (self.dirs.items, 0..) |dir, i| if (dir.parent == idx) try kids.append(self.a, i);
        return kids;
    }

    fn recordLive(self: *Chain, idx: usize) Error!void {
        const st = fsx.fstat(self.dirs.items[idx].fd) catch |err| return fail(self.a, self.why, err, self.dirs.items[idx].path);
        const acls = try readDirectoryAccess(self.a, self.dirs.items[idx].fd, self.dirs.items[idx].path, self.why);
        self.dirs.items[idx].mode = st.perm();
        if (self.dirs.items[idx].anchor) {
            if (self.hold) |h| {
                h.acls.deinit(self.a);
                h.acls = acls;
                h.mode = st.perm();
                self.dirs.items[idx].acls = h.acls;
            } else self.dirs.items[idx].acls = acls;
            self.dirs.items[idx].own_acls = false;
            return;
        }
        if (self.dirs.items[idx].own_acls) self.dirs.items[idx].acls.deinit(self.a);
        self.dirs.items[idx].acls = acls;
        self.dirs.items[idx].own_acls = true;
    }

    fn outsideLiveWritable(self: *Chain) Error!bool {
        const path = if (self.outside_path.len > 0) self.outside_path else ".";
        if (self.outside < 0) return fail(self.a, self.why, error.AccessDenied, path);
        const st = fsx.fstat(self.outside) catch |err| return fail(self.a, self.why, err, path);
        return st.perm() & 0o200 != 0;
    }

    /// Journal a sealed outside directory and make that same descriptor
    /// writable. The journal is flushed to its parent before the mode
    /// change. The walk restores the recorded mode and list, flushes this
    /// descriptor, and close-checks it before the journal is removed.
    fn holdOutside(self: *Chain) Error!void {
        if (self.outside_write_open) return;
        const path = if (self.outside_path.len > 0) self.outside_path else return fail(self.a, self.why, error.AccessDenied, ".");
        if (self.outside < 0) return fail(self.a, self.why, error.AccessDenied, path);
        if (self.outside_seal.len > 0) {
            if (try self.outsideLiveWritable()) {
                if (self.outside_mode & 0o200 == 0) self.outside_write_open = true;
                return;
            }
            try fsyncParentPath(self.a, path, self.why);
            const failed = chmodFd(self.outside, self.outside_mode | 0o300);
            if (failed != .SUCCESS) {
                self.why.* = main_mod.errnoPath(self.a, failed, path);
                return error.Refused;
            }
            self.outside_write_open = true;
            return;
        }
        if (try self.outsideLiveWritable()) return;
        const st = fsx.fstat(self.outside) catch |err| return fail(self.a, self.why, err, path);
        const mode = st.perm();
        const acls = try readDirectoryAccess(self.a, self.outside, path, self.why);
        const seal = persistSeal(self.a, path, mode, acls, self.why) catch |err| {
            acls.deinit(self.a);
            return err;
        };
        fsyncParentPath(self.a, path, self.why) catch |err| {
            acls.deinit(self.a);
            sys.unlinkat(fsx.cwd, seal, 0) catch {};
            self.a.free(seal);
            return err;
        };
        const failed = chmodFd(self.outside, mode | 0o300);
        if (failed != .SUCCESS) {
            acls.deinit(self.a);
            sys.unlinkat(fsx.cwd, seal, 0) catch {};
            self.a.free(seal);
            self.why.* = main_mod.errnoPath(self.a, failed, path);
            return error.Refused;
        }
        self.outside_mode = mode;
        self.outside_acls = acls;
        self.outside_own = true;
        self.outside_seal = seal;
        self.outside_write_open = true;
    }

    /// Put the outside directory's recorded list and mode back. The flush
    /// that follows is the first one after the write bit was added.
    fn restoreOutside(self: *Chain) Error!void {
        const fd = self.outside;
        const path = self.outside_path;
        const mode = self.outside_mode;
        const acls = self.outside_acls;
        try restoreLists(self.a, fd, acls, path, true, self.why);
        const back = chmodFd(fd, mode);
        if (back != .SUCCESS) {
            self.why.* = main_mod.errnoPath(self.a, back, path);
            return error.Refused;
        }
        try restoreLists(self.a, fd, acls, path, true, self.why);
        const st = fsx.fstat(fd) catch |err| return fail(self.a, self.why, err, path);
        if (st.perm() != mode) {
            self.why.* = "the mode of a sealed directory did not round-trip";
            return error.Refused;
        }
        self.outside_write_open = false;
    }

    /// Flush and close the outside descriptor. A journalled directory is
    /// restored first, and its journal stays when the close fails.
    fn finishOutside(self: *Chain) Error!void {
        if (self.outside < 0) return;
        const path = if (self.outside_path.len > 0) self.outside_path else if (self.dirs.items.len == 0) "." else std.fs.path.dirname(self.dirs.items[0].path) orelse ".";
        if (self.outside_seal.len == 0) {
            const fd = self.outside;
            self.outside = -1;
            atomic.closeChecked(fd) catch |err| return fail(self.a, self.why, err, path);
            return;
        }
        try self.restoreOutside();
        const synced = fsyncFd(self.outside);
        if (synced != .SUCCESS) {
            const fd = self.outside;
            self.outside = -1;
            sys.close(fd);
            self.why.* = main_mod.errnoPath(self.a, synced, path);
            return error.Refused;
        }
        const fd = self.outside;
        self.outside = -1;
        atomic.closeChecked(fd) catch |err| return fail(self.a, self.why, err, path);
        const parent = std.fs.path.dirname(self.outside_seal) orelse ".";
        const name = std.fs.path.basename(self.outside_seal);
        try dropSealName(self.a, parent, name, self.why);
        self.a.free(self.outside_seal);
        self.outside_seal = "";
    }

    fn writeJournal(self: *Chain, idx: usize) Error!void {
        const body = formatSeal(self.a, self.dirs.items[idx].mode, self.dirs.items[idx].acls) catch return error.OutOfMemory;
        defer self.a.free(body);
        const name = std.fmt.allocPrint(self.a, ".{s}.ebsr-seal", .{self.dirs.items[idx].name}) catch return error.OutOfMemory;
        defer self.a.free(name);
        const pfd = self.parentFd(idx);
        if (pfd < 0) return fail(self.a, self.why, error.AccessDenied, self.dirs.items[idx].path);
        try writeSealAt(self.a, pfd, name, body, self.why);
    }

    /// The journal is in the parent. Flush that parent before the mode
    /// change. A parent whose own list is still the replaced one is restored
    /// first, so the flush does not persist that list.
    fn syncContaining(self: *Chain, idx: usize) Error!void {
        const parent = self.dirs.items[idx].parent;
        if (parent == root_parent) {
            if (!self.outside_unsynced or self.outside < 0) return;
            // A chmod made this directory writable and replaced its list.
            // Put the list back before the flush, then open the write bit
            // again when the root journal is still here.
            const opened = self.outside_write_open;
            if (opened) try self.restoreOutside();
            const path = if (self.outside_path.len > 0) self.outside_path else std.fs.path.dirname(self.dirs.items[idx].path) orelse ".";
            const synced = fsyncFd(self.outside);
            if (synced != .SUCCESS) {
                self.why.* = main_mod.errnoPath(self.a, synced, path);
                return error.Refused;
            }
            self.outside_unsynced = false;
            if (opened and self.dirs.items[idx].journal) {
                const failed = chmodFd(self.outside, self.outside_mode | 0o300);
                if (failed != .SUCCESS) {
                    self.why.* = main_mod.errnoPath(self.a, failed, path);
                    return error.Refused;
                }
                self.outside_write_open = true;
            }
            return;
        }
        if (!self.dirs.items[parent].unsynced) return;
        if (self.dirs.items[parent].write_open) {
            try self.syncContaining(parent);
            try self.restoreClean(parent);
            const synced = fsyncFd(self.dirs.items[parent].fd);
            if (synced != .SUCCESS) {
                self.why.* = main_mod.errnoPath(self.a, synced, self.dirs.items[parent].path);
                return error.Refused;
            }
            self.dirs.items[parent].unsynced = false;
            if (self.dirs.items[parent].children > 0 and !(try self.writable(parent))) {
                const failed = chmodFd(self.dirs.items[parent].fd, self.dirs.items[parent].mode | 0o300);
                if (failed != .SUCCESS) {
                    self.why.* = main_mod.errnoPath(self.a, failed, self.dirs.items[parent].path);
                    return error.Refused;
                }
                self.dirs.items[parent].write_open = true;
                if (self.dirs.items[parent].anchor) {
                    if (self.hold) |h| h.pending = true;
                }
            }
            return;
        }
        const synced = fsyncFd(self.dirs.items[parent].fd);
        if (synced != .SUCCESS) {
            self.why.* = main_mod.errnoPath(self.a, synced, self.dirs.items[parent].path);
            return error.Refused;
        }
        self.dirs.items[parent].unsynced = false;
    }

    fn ensureWritable(self: *Chain, idx: usize) Error!void {
        if (try self.writable(idx)) return;
        if (!self.dirs.items[idx].journal) {
            const parent = self.dirs.items[idx].parent;
            if (parent != root_parent and !(try self.writable(parent))) try self.ensureWritable(parent);
            if (parent == root_parent and !(try self.outsideLiveWritable())) try self.holdOutside();
            try self.recordLive(idx);
            try self.writeJournal(idx);
            self.dirs.items[idx].journal = true;
            if (parent != root_parent) self.dirs.items[parent].children += 1;
        }
        self.markUnsynced(idx);
        try self.syncContaining(idx);
        const failed = chmodFd(self.dirs.items[idx].fd, self.dirs.items[idx].mode | 0o300);
        if (failed != .SUCCESS) {
            self.why.* = main_mod.errnoPath(self.a, failed, self.dirs.items[idx].path);
            return error.Refused;
        }
        self.dirs.items[idx].write_open = true;
        if (self.dirs.items[idx].anchor) {
            if (self.hold) |h| h.pending = true;
        }
    }

    fn prepare(self: *Chain, idx: usize) Error!void {
        if (self.needsWrite(idx)) try self.ensureWritable(idx);
        var kids = try self.kidsOf(idx);
        defer kids.deinit(self.a);
        for (kids.items) |child| try self.prepare(child);
    }

    fn restoreOnce(self: *Chain, idx: usize) Error!void {
        const fd = self.dirs.items[idx].fd;
        const path = self.dirs.items[idx].path;
        const mode = self.dirs.items[idx].mode;
        const acls = self.dirs.items[idx].acls;
        try restoreLists(self.a, fd, acls, path, true, self.why);
        const back = chmodFd(fd, mode);
        if (back != .SUCCESS) {
            self.why.* = main_mod.errnoPath(self.a, back, path);
            return error.Refused;
        }
        try restoreLists(self.a, fd, acls, path, true, self.why);
        const st = fsx.fstat(fd) catch |err| return fail(self.a, self.why, err, path);
        if (st.perm() != mode) {
            self.why.* = "the mode of a sealed directory did not round-trip";
            return error.Refused;
        }
        self.dirs.items[idx].write_open = false;
        if (self.dirs.items[idx].anchor) {
            if (self.hold) |h| h.pending = false;
        }
    }

    /// Put the recorded list and mode back. One failed attempt is retried
    /// once: the first error is still the result, and the journal stays.
    fn restoreClean(self: *Chain, idx: usize) Error!void {
        self.restoreOnce(idx) catch |err| {
            self.restoreOnce(idx) catch {};
            return err;
        };
    }

    fn restoreAll(self: *Chain, idx: usize) Error!void {
        var kids = try self.kidsOf(idx);
        defer kids.deinit(self.a);
        for (kids.items) |child| try self.restoreAll(child);
        if (self.dirs.items[idx].anchor) return;
        if (!self.dirs.items[idx].journal and !self.dirs.items[idx].write_open) return;
        if (!(try self.writable(idx))) try self.ensureWritable(idx);
        try self.restoreClean(idx);
        const synced = fsyncFd(self.dirs.items[idx].fd);
        if (synced != .SUCCESS) {
            self.why.* = main_mod.errnoPath(self.a, synced, self.dirs.items[idx].path);
            return error.Refused;
        }
    }

    /// Close this entry's descriptor and report a failed close. Linux frees
    /// the descriptor even when close fails, so the error is the result.
    fn closeWalk(self: *Chain, idx: usize) Error!void {
        const dir = &self.dirs.items[idx];
        if (!dir.owned_fd or dir.fd < 0) return;
        const fd = dir.fd;
        dir.fd = -1;
        atomic.closeChecked(fd) catch |err| return fail(self.a, self.why, err, dir.path);
    }

    /// Close the descriptor `restoreAll` flushed before any journal under it
    /// is removed. A second descriptor is opened first and is the one the
    /// children use when the name still refers to this inode. A failed close
    /// or stat leaves the journals. The walk closes whichever descriptor it
    /// flushed last before it unlinks this directory's own journal.
    fn confirmWalk(self: *Chain, idx: usize) Error!bool {
        const dir = &self.dirs.items[idx];
        if (dir.anchor or !dir.owned_fd or dir.fd < 0 or dir.children == 0) return false;
        const pfd = self.parentFd(idx);
        const path = dir.path;
        if (pfd < 0) return fail(self.a, self.why, error.AccessDenied, path);
        const held = sys.openat(pfd, dir.name, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(self.a, self.why, err, path);
        const flushed = dir.fd;
        dir.fd = held;
        atomic.closeChecked(flushed) catch |err| return fail(self.a, self.why, err, path);
        const now = statOnce(pfd, dir.name) catch |err| return fail(self.a, self.why, err, path);
        if (now.dev != dir.dev or now.ino != dir.ino) {
            self.why.* = "a sealed directory changed before its journal was removed";
            return error.Refused;
        }
        return true;
    }

    fn unlinkSealFile(self: *Chain, idx: usize) Error!void {
        const name = std.fmt.allocPrint(self.a, ".{s}.ebsr-seal", .{self.dirs.items[idx].name}) catch return error.OutOfMemory;
        defer self.a.free(name);
        const pfd = self.parentFd(idx);
        if (pfd < 0) return fail(self.a, self.why, error.AccessDenied, self.dirs.items[idx].path);
        const parent = self.dirs.items[idx].parent;
        if (parent == root_parent and !(try self.outsideLiveWritable())) try self.holdOutside();
        sys.unlinkat(pfd, name, 0) catch |err| switch (err) {
            error.NotFound => {},
            else => return fail(self.a, self.why, err, self.dirs.items[idx].path),
        };
        // The write bit was added so this unlink could proceed. Put that
        // parent's list back before the flush, or the flush would record
        // the replaced list. A held directory stays writable for the
        // children that remain; its caller flushes it once its list is back.
        if (parent != root_parent and self.dirs.items[parent].write_open and !self.dirs.items[parent].anchor) {
            try self.restoreClean(parent);
        }
        const hold_open = parent != root_parent and self.dirs.items[parent].anchor and self.dirs.items[parent].write_open;
        if (parent == root_parent and self.outside_seal.len > 0) {
            // The list is still the one the chmod replaced. finishOutside
            // restores it, flushes this descriptor, and close-checks it
            // before the outside journal is removed.
            self.dirs.items[idx].journal = false;
            return;
        }
        if (!hold_open) {
            const path = if (parent == root_parent)
                std.fs.path.dirname(self.dirs.items[idx].path) orelse "."
            else
                self.dirs.items[parent].path;
            const synced = fsyncFd(pfd);
            if (synced != .SUCCESS) {
                if (parent == root_parent) {
                    const fd = self.outside;
                    self.outside = -1;
                    sys.close(fd);
                }
                self.why.* = main_mod.errnoPath(self.a, synced, path);
                return error.Refused;
            }
            if (parent == root_parent) {
                const fd = self.outside;
                self.outside = -1;
                atomic.closeChecked(fd) catch |err| return fail(self.a, self.why, err, path);
            }
        }
        self.dirs.items[idx].journal = false;
        if (parent != root_parent and self.dirs.items[parent].children > 0) self.dirs.items[parent].children -= 1;
    }

    fn unlinkRestored(self: *Chain, idx: usize) Error!void {
        _ = try self.confirmWalk(idx);
        var kids = try self.kidsOf(idx);
        defer kids.deinit(self.a);
        for (kids.items) |child| try self.unlinkRestored(child);
        if (self.dirs.items[idx].anchor) return;
        if (!self.dirs.items[idx].journal or self.dirs.items[idx].children != 0) return;
        // Close the descriptor that was flushed last. A directory with
        // children was flushed again on the descriptor opened for them,
        // after a mode change let a child journal be removed. That close
        // has to succeed before this journal is removed.
        try self.closeWalk(idx);
        const parent = self.dirs.items[idx].parent;
        if (parent != root_parent and !(try self.writable(parent))) try self.ensureWritable(parent);
        try self.unlinkSealFile(idx);
    }

    fn closeCheckedRest(self: *Chain) Error!void {
        var i = self.dirs.items.len;
        while (i > 0) {
            i -= 1;
            try self.closeWalk(i);
        }
        try self.finishOutside();
    }
};

fn runChain(a: Allocator, root_fd: sys.fd_t, root: []const u8, hold: ?*DirectoryHold, why: *[]const u8) Error!void {
    var chain: Chain = .{ .a = a, .why = why };
    defer chain.deinit();
    try chain.build(root_fd, root, hold);
    try chain.prepare(0);
    try chain.restoreAll(0);
    try chain.unlinkRestored(0);
    try chain.closeCheckedRest();
}

/// Restore every nested directory and unlink its journal while `root` can
/// still be written. Deeper journals go first. The walk uses `hold`'s
/// descriptor and does not close it. The parent itself is not flushed.
fn releaseNestedFrom(hold: *DirectoryHold, root: []const u8, why: *[]const u8) Error!void {
    try runChain(hold.a, hold.fd, root, hold, why);
}

/// Restore every journal under `root`, including one beside `root`.
/// Each directory is the descriptor opened before any close.
fn finishNestedSeals(a: Allocator, root: []const u8, why: *[]const u8) Error!void {
    const fd = sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, root);
    try runChain(a, fd, root, null, why);
}

/// Restore `root` when its journal is still beside it, then every journal
/// under it. Runs before a rewrite flushes anything.
fn finishSealed(a: Allocator, root: []const u8, why: *[]const u8) Error!void {
    try finishNestedSeals(a, root, why);
}

const Window = struct {
    hold: ?DirectoryHold = null,

    fn close(self: *Window, directory: []const u8, why: *[]const u8) Error!void {
        defer self.hold = null;
        if (self.hold) |*held| try held.close(directory, why);
    }

    /// Restore, flush, and drop the journal. The flush runs only after the
    /// mode and access list are back.
    fn finish(self: *Window, directory: []const u8, why: *[]const u8) Error!void {
        if (self.hold) |*held| {
            defer self.hold = null;
            try finishHold(held, directory, why);
        }
    }
};

fn openWritable(a: Allocator, directory: []const u8, why: *[]const u8) Error!Window {
    return .{ .hold = try holdDirectory(a, directory, why) };
}

const Seal = struct { mode: u32, acls: atomic.Acls, path: []const u8 };

fn sealPath(a: Allocator, directory: []const u8) ![]u8 {
    const base = std.fs.path.basename(directory);
    const parent = std.fs.path.dirname(directory) orelse ".";
    if (std.mem.eql(u8, parent, directory)) return a.print(".{s}.ebsr-seal", .{base});
    return a.print("{s}/.{s}.ebsr-seal", .{ parent, base });
}

fn formatSeal(a: Allocator, mode: u32, acls: atomic.Acls) ![]u8 {
    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(a);
    try out.print(a, "ebsr-seal 1\nmode {d}\n", .{mode});
    const hexdigits = "0123456789abcdef";
    for (acls.present, 0..) |present, i| {
        if (present) |value| {
            try out.print(a, "{d} ", .{i});
            for (value) |byte| {
                try out.append(a, hexdigits[byte >> 4]);
                try out.append(a, hexdigits[byte & 0xf]);
            }
            try out.append(a, '\n');
        } else try out.print(a, "{d} -\n", .{i});
    }
    return try out.toOwnedSlice(a);
}

fn parseSeal(a: Allocator, text: []const u8, why: *[]const u8) Error!struct { mode: u32, acls: atomic.Acls } {
    var acls: atomic.Acls = .{};
    errdefer acls.deinit(a);
    var lines = std.mem.splitScalar(u8, text, '\n');
    if (!std.mem.eql(u8, lines.next() orelse "", "ebsr-seal 1")) {
        why.* = "a sealed directory journal is unreadable";
        return error.Refused;
    }
    const mode_line = lines.next() orelse "";
    const mode_text = if (std.mem.startsWith(u8, mode_line, "mode ")) mode_line["mode ".len..] else "";
    const mode = std.fmt.parseInt(u32, std.mem.trim(u8, mode_text, " \r"), 10) catch {
        why.* = "a sealed directory journal is unreadable";
        return error.Refused;
    };
    var seen = [_]bool{ false, false, false };
    while (lines.next()) |line| {
        if (line.len == 0) continue;
        var parts = std.mem.splitScalar(u8, line, ' ');
        const index_text = parts.next() orelse "";
        // A missing token is not an empty list. "0" has no value; "0 " has
        // a present empty value, and "-" is an absent list.
        const hex = parts.next() orelse {
            why.* = "a sealed directory journal is unreadable";
            return error.Refused;
        };
        const index = std.fmt.parseInt(usize, index_text, 10) catch {
            why.* = "a sealed directory journal is unreadable";
            return error.Refused;
        };
        if (index >= atomic.acl_attributes.len or seen[index]) {
            why.* = "a sealed directory journal is unreadable";
            return error.Refused;
        }
        seen[index] = true;
        if (std.mem.eql(u8, hex, "-")) continue;
        if (hex.len % 2 != 0) {
            why.* = "a sealed directory journal is unreadable";
            return error.Refused;
        }
        const value = try a.alloc(u8, hex.len / 2);
        _ = std.fmt.hexToBytes(value, hex) catch {
            a.free(value);
            why.* = "a sealed directory journal is unreadable";
            return error.Refused;
        };
        acls.present[index] = value;
    }
    for (seen) |got| if (!got) {
        why.* = "a sealed directory journal is unreadable";
        return error.Refused;
    };
    return .{ .mode = mode, .acls = acls };
}

fn loadSeal(a: Allocator, directory: []const u8, why: *[]const u8) Error!?Seal {
    const path = sealPath(a, directory) catch return error.OutOfMemory;
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0) catch |err| switch (err) {
        error.NotFound => {
            a.free(path);
            return null;
        },
        else => {
            a.free(path);
            return fail(a, why, err, directory);
        },
    };
    defer sys.close(fd);
    const text = fsx.readAll(a, fd, 1 << 20) catch |err| {
        a.free(path);
        return fail(a, why, err, path);
    };
    defer a.free(text);
    const parsed = parseSeal(a, text, why) catch |err| {
        a.free(path);
        return err;
    };
    return .{ .mode = parsed.mode, .acls = parsed.acls, .path = path };
}

/// Replace `path` durably. The bytes are a small journal, not a file whose
/// lists have to be preserved. `sync_parent` is false when the parent is
/// held: flushing it here would make the replaced list durable. A failed
/// close refuses the update before the mode changes.
fn writeDurable(a: Allocator, path: []const u8, body: []const u8, sync_parent: bool, why: *[]const u8) Error!void {
    const parent = std.fs.path.dirname(path) orelse ".";
    const base = std.fs.path.basename(path);
    const dir = sys.openat(fsx.cwd, parent, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, parent);
    defer sys.close(dir);
    atomic.atomicWriteAtOpts(a, dir, base, body, 0o600, .{
        .sync_parent = sync_parent,
        .check_close = true,
    }) catch |err| return fail(a, why, err, path);
}

fn persistSeal(a: Allocator, directory: []const u8, mode: u32, acls: atomic.Acls, why: *[]const u8) Error![]const u8 {
    const path = sealPath(a, directory) catch return error.OutOfMemory;
    errdefer a.free(path);
    const parent = std.fs.path.dirname(directory) orelse ".";
    var parent_hold: ?DirectoryHold = null;
    if (!std.mem.eql(u8, parent, directory) and !std.mem.eql(u8, parent, "/") and parent.len != 0) {
        if (fsx.statAt(fsx.cwd, parent, false)) |st| {
            // A writable parent with a leftover journal is held too. Leaving
            // it out takes the sync path and flushes the replaced list.
            if (st.isDir()) parent_hold = try holdDirectory(a, parent, why);
        } else |_| {}
    }
    const body = formatSeal(a, mode, acls) catch |err| {
        if (parent_hold) |*held| finishHold(held, parent, why) catch {};
        return err;
    };
    defer a.free(body);
    // A held parent is flushed by finishHold, after its mode and lists are
    // back. Flushing it here would make the replaced list durable.
    writeDurable(a, path, body, parent_hold == null, why) catch |err| {
        if (parent_hold) |*held| finishHold(held, parent, why) catch {};
        return err;
    };
    if (parent_hold) |*held| try finishHold(held, parent, why);
    return path;
}

/// Replace `path` as `atomicWrite` does, and do not flush its parent. A held
/// directory is flushed once its mode and access lists are restored. A failed
/// close is a failed update, and a temporary file is removed only when it is
/// still the file this call created.
pub fn writeHeld(a: Allocator, path: []const u8, data: []const u8, step: []const u8, faults: atomic.Faults, why: *[]const u8) Error!void {
    var inner: atomic.Why = .{};
    atomic.atomicWrite(a, path, data, .{ .faults = faults, .step = step, .sync_parent = false, .check_close = true }, &inner) catch |err| {
        if (err == error.OutOfMemory) {
            inner.deinit(a);
            return error.OutOfMemory;
        }
        if (inner.msg.len > 0) {
            why.* = inner.msg;
            inner.msg = "";
            return error.Refused;
        }
        inner.deinit(a);
        return fail(a, why, err, path);
    };
    inner.deinit(a);
}

fn zpath(buf: *[sys.path_max:0]u8, path: []const u8) ![:0]const u8 {
    return sys.z(buf, path);
}

fn fail(a: Allocator, why: *[]const u8, err: anyerror, path: []const u8) Error {
    // closeChecked keeps EINTR in last_errno and returns Unexpected. describe
    // prints that pair as EIO, so the interrupted close is reported itself.
    if (sys.asError(err)) |mapped| {
        if (mapped == error.Unexpected and sys.last_errno == .INTR) {
            why.* = main_mod.errnoPath(a, .INTR, path);
            return error.Refused;
        }
    }
    why.* = main_mod.osError(a, err, path);
    return error.Refused;
}

fn readlinkExact(a: Allocator, path: []const u8) ![]u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const n = try sys.call(linux.readlinkat, .{ linux.AT.FDCWD, (try zpath(&zb, path)).ptr, &buf, buf.len });
    if (n == buf.len) return error.NameTooLong;
    return a.dupe(u8, buf[0..n]);
}

fn transformed(a: Allocator, data: []const u8, old: []const u8, new: []const u8, path: []const u8, why: *[]const u8) Error![]u8 {
    if (std.mem.indexOfScalar(u8, data, 0) != null) return rewriteBinary(a, data, old, new, path, why);
    return (try replacePrefix(a, data, old, new, why)).bytes;
}

fn punchIgnored() bool {
    // Python leaves the written zeros in place for these and continues.
    return switch (sys.last_errno) {
        .OPNOTSUPP, .NOSYS, .INVAL => true,
        else => false,
    };
}

fn refusePunch(a: Allocator, why: *[]const u8, partial: []const u8) Error {
    var out: std.ArrayList(u8) = .empty;
    const text = sys.errnoText(sys.last_errno) orelse "Unknown error";
    out.print(a, "[Errno {d}] fallocate(PUNCH_HOLE): {s}: ", .{ @backingInt(sys.last_errno), text }) catch return error.OutOfMemory;
    common.pytext.reprStr(&out, a, partial) catch return error.OutOfMemory;
    why.* = out.items;
    return error.Refused;
}

fn punchZeros(a: Allocator, fd: fsx.fd_t, data: []const u8, partial: []const u8, why: *[]const u8) Error!void {
    var index: usize = 0;
    while (index < data.len) {
        if (data[index] != 0) {
            index += 1;
            continue;
        }
        var end = index;
        while (end < data.len and data[end] == 0) end += 1;
        const start = (index + 4095) & ~@as(usize, 4095);
        const aligned = end & ~@as(usize, 4095);
        if (aligned > start) {
            while (true) {
                if (sys.result(linux.fallocate(fd, 3, @intCast(start), @intCast(aligned - start))) catch {
                    if (punchIgnored()) break;
                    return refusePunch(a, why, partial);
                }) |_| break;
            }
        }
        index = end;
    }
}

fn copyUserXattrs(a: Allocator, src: fsx.fd_t, dst: fsx.fd_t, source: []const u8, partial: []const u8, why: *[]const u8) Error!void {
    var names: [64 * 1024]u8 = undefined;
    const n = while (true) {
        if (sys.result(atomic.ops.flistxattr(src, &names)) catch |err| switch (err) {
            // Python treats ENOTSUP and EOPNOTSUPP as an empty list.
            error.Unsupported => return,
            else => return fail(a, why, err, source),
        }) |got| break got;
    };
    var it = std.mem.splitScalar(u8, names[0..n], 0);
    while (it.next()) |name| {
        if (name.len == 0) continue;
        if (!std.mem.startsWith(u8, name, "user.") and !std.mem.startsWith(u8, name, "trusted.")) continue;
        var named: [256:0]u8 = undefined;
        if (name.len >= named.len) continue;
        @memcpy(named[0..name.len], name);
        named[name.len] = 0;
        var value: [64 * 1024]u8 = undefined;
        const vn = while (true) {
            if (sys.result(atomic.ops.fgetxattr(src, named[0..name.len :0], &value)) catch |err| return fail(a, why, err, source)) |got| break got;
        };
        while (true) {
            if (sys.result(atomic.ops.fsetxattr(dst, named[0..name.len :0], value[0..vn])) catch |err| return fail(a, why, err, partial)) |_| break;
        }
    }
}

fn restoreAccess(a: Allocator, fd: fsx.fd_t, meta: atomic.Metadata, why: *[]const u8) Error!void {
    if (linux.errno(atomic.ops.fchown(fd, meta.info.uid, meta.info.gid)) != .SUCCESS) {
        why.* = "cannot preserve ownership of a rewritten file";
        return error.Refused;
    }
    const mode = meta.info.perm();
    if (linux.errno(atomic.ops.fchmod(fd, mode)) != .SUCCESS) {
        why.* = "cannot preserve the mode of a rewritten file";
        return error.Refused;
    }
    try restoreLists(a, fd, meta.acls, "rewritten file", false, why);
    const restored = fsx.fstat(fd) catch |err| return fail(a, why, err, "rewritten file");
    if (restored.uid != meta.info.uid or restored.gid != meta.info.gid or restored.perm() != mode) {
        why.* = "access metadata of a rewritten file did not round-trip";
        return error.Refused;
    }
}

/// The test artifact fails this unlink when EB_STAGED_REBUILD_FAIL_AT names
/// `prefix.ready.unlink`. A release build drops the check, so the env var
/// is not in that binary.
fn injectedReadyUnlink() bool {
    if (comptime !atomic.replaceable) return false;
    const fd = sys.openat(fsx.cwd, "/proc/self/environ", .{ .ACCMODE = .RDONLY }, 0) catch return false;
    defer sys.close(fd);
    var buf: [1 << 16]u8 = undefined;
    var got: usize = 0;
    while (got < buf.len) {
        const n = sys.read(fd, buf[got..]) catch break;
        if (n == 0) break;
        got += n;
    }
    const key = "EB_STAGED_REBUILD_FAIL_AT=";
    var it = std.mem.splitScalar(u8, buf[0..got], 0);
    while (it.next()) |entry| {
        if (!std.mem.startsWith(u8, entry, key)) continue;
        var names = std.mem.splitScalar(u8, entry[key.len..], ',');
        while (names.next()) |name| {
            if (std.mem.eql(u8, name, "prefix.ready.unlink")) return true;
        }
    }
    return false;
}

fn commitNames(a: Allocator, names: []const []const u8, ready: []const u8, defer_dir: ?[]const u8, why: *[]const u8) Error!void {
    if (names.len == 1) {
        const parent = std.fs.path.dirname(names[0]) orelse "/";
        sys.renameat(fsx.cwd, ready, fsx.cwd, names[0]) catch |err| return fail(a, why, err, names[0]);
        if (!heldDirectory(defer_dir, parent)) atomic.fsyncDir(parent) catch |err| return fail(a, why, err, parent);
        return;
    }
    for (names) |name| {
        const parent = std.fs.path.dirname(name) orelse ".";
        const temporary = try std.fmt.allocPrint(a, "{s}/.{s}.{d}.ebsr-link", .{ parent, std.fs.path.basename(name), linux.getpid() });
        const link = struct {
            fn go(from: []const u8, to: []const u8) !void {
                var zf: [sys.path_max:0]u8 = undefined;
                var zt: [sys.path_max:0]u8 = undefined;
                const left = try sys.z(&zf, from);
                const right = try sys.z(&zt, to);
                if (linux.errno(linux.linkat(linux.AT.FDCWD, left.ptr, linux.AT.FDCWD, right.ptr, 0)) != .SUCCESS) return error.Unexpected;
            }
        }.go;
        link(ready, temporary) catch |err| return fail(a, why, err, name);
        sys.renameat(fsx.cwd, temporary, fsx.cwd, name) catch |err| {
            sys.unlinkat(fsx.cwd, temporary, 0) catch {};
            return fail(a, why, err, name);
        };
        if (!heldDirectory(defer_dir, parent)) atomic.fsyncDir(parent) catch |err| return fail(a, why, err, parent);
    }
    const ready_parent = std.fs.path.dirname(ready) orelse "/";
    if (injectedReadyUnlink()) return fail(a, why, error.AccessDenied, ready);
    sys.unlinkat(fsx.cwd, ready, 0) catch |err| return fail(a, why, err, ready);
    if (!heldDirectory(defer_dir, ready_parent)) atomic.fsyncDir(ready_parent) catch |err| return fail(a, why, err, ready_parent);
}

fn publish(a: Allocator, names: []const []const u8, image: []const u8, why: *[]const u8) Error!void {
    const source = names[0];
    const info = fsx.statAt(fsx.cwd, source, false) catch |err| return fail(a, why, err, source);
    const parent = std.fs.path.dirname(source) orelse ".";
    const partial = try std.fmt.allocPrint(a, "{s}/.{d}.ebsr-partial", .{ parent, info.ino });
    const ready = try std.fmt.allocPrint(a, "{s}/.{d}.ebsr-ready", .{ parent, info.ino });
    const src = sys.openat(fsx.cwd, source, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true, .NONBLOCK = true }, 0) catch |err| return fail(a, why, err, source);
    defer sys.close(src);
    var inner: atomic.Why = .{};
    const meta = atomic.readMetadata(a, source, &inner) catch {
        why.* = if (inner.msg.len > 0) inner.msg else "cannot read access metadata";
        return error.Refused;
    };
    var window = try openWritable(a, parent, why);
    const sealing = window.hold != null;
    publishHeld(a, names, image, parent, partial, ready, src, meta, if (sealing) parent else null, why) catch |err| {
        // The ready name is still in this directory. Remove it before the
        // hold closes, or the restored mode makes the next rewrite refuse
        // to discard it.
        unlinkPrivate(a, parent, why) catch |uerr| {
            window.finish(parent, why) catch {};
            return uerr;
        };
        try window.finish(parent, why);
        return err;
    };
    try window.finish(parent, why);
}

fn publishHeld(
    a: Allocator,
    names: []const []const u8,
    image: []const u8,
    parent: []const u8,
    partial: []const u8,
    ready: []const u8,
    src: fsx.fd_t,
    meta: atomic.Metadata,
    defer_dir: ?[]const u8,
    why: *[]const u8,
) Error!void {
    const source = names[0];
    const fd = sys.openat(fsx.cwd, partial, .{ .ACCMODE = .RDWR, .CREAT = true, .EXCL = true, .NOFOLLOW = true }, 0o600) catch |err| return fail(a, why, err, partial);
    var failed = true;
    defer if (failed) {
        sys.close(fd);
        sys.unlinkat(fsx.cwd, partial, 0) catch {};
    };
    sys.writeAll(fd, image) catch |err| return fail(a, why, err, partial);
    try punchZeros(a, fd, image, partial, why);
    try copyUserXattrs(a, src, fd, source, partial, why);
    try restoreAccess(a, fd, meta, why);
    sys.fsync(fd) catch |err| return fail(a, why, err, partial);
    sys.close(fd);
    failed = false;
    sys.renameat(fsx.cwd, partial, fsx.cwd, ready) catch |err| return fail(a, why, err, ready);
    // The directory entry for the finished file has to be durable before
    // the published name starts pointing at it. Python flushes here. A
    // sealed directory is still owner-writable, so that flush waits until
    // the mode and access list are restored.
    if (!heldDirectory(defer_dir, parent)) {
        atomic.fsyncDir(parent) catch |err| {
            sys.unlinkat(fsx.cwd, ready, 0) catch {};
            return fail(a, why, err, parent);
        };
    }
    try commitNames(a, names, ready, defer_dir, why);
}

fn rewriteSymlink(a: Allocator, path: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    const target = readlinkExact(a, path) catch |err| return fail(a, why, err, path);
    const updated = try replacePrefix(a, target, old, new, why);
    if (updated.count == 0) return;
    const parent = std.fs.path.dirname(path) orelse ".";
    const temporary = try std.fmt.allocPrint(a, "{s}/.{s}.{d}.ebsr-symlink", .{ parent, std.fs.path.basename(path), linux.getpid() });
    var window = try openWritable(a, parent, why);
    const sealing = window.hold != null;
    const outcome = rewriteSymlinkHeld(a, path, parent, temporary, updated.bytes, if (sealing) parent else null, why);
    try window.finish(parent, why);
    return outcome;
}

fn rewriteSymlinkHeld(a: Allocator, path: []const u8, parent: []const u8, temporary: []const u8, bytes: []const u8, defer_dir: ?[]const u8, why: *[]const u8) Error!void {
    var zt: [sys.path_max:0]u8 = undefined;
    var zp: [sys.path_max:0]u8 = undefined;
    const left = zpath(&zt, bytes) catch |err| return fail(a, why, err, path);
    const right = zpath(&zp, temporary) catch |err| return fail(a, why, err, temporary);
    if (linux.errno(linux.symlinkat(left.ptr, linux.AT.FDCWD, right.ptr)) != .SUCCESS) {
        why.* = try a.print("cannot replace symbolic link {s}", .{path});
        return error.Refused;
    }
    sys.renameat(fsx.cwd, temporary, fsx.cwd, path) catch |err| {
        sys.unlinkat(fsx.cwd, temporary, 0) catch {};
        return fail(a, why, err, path);
    };
    if (!heldDirectory(defer_dir, parent)) atomic.fsyncDir(parent) catch |err| return fail(a, why, err, parent);
}

const private_suffixes = [_][]const u8{
    ".ebsr-partial",
    ".ebsr-ready",
    ".ebsr-symlink",
    ".ebsr-link",
};

fn ours(name: []const u8) bool {
    if (std.mem.eql(u8, name, skip_name)) return true;
    // The seal journal sits beside the directory. It is not a file to rewrite
    // and not a private name recoverTree is allowed to discard.
    if (std.mem.endsWith(u8, name, ".ebsr-seal")) return true;
    for (private_suffixes) |suffix| {
        if (std.mem.endsWith(u8, name, suffix)) return true;
    }
    return false;
}

fn isPrivateName(name: []const u8) bool {
    for (private_suffixes) |suffix| {
        if (std.mem.endsWith(u8, name, suffix)) return true;
    }
    return false;
}

fn unlinkPrivate(a: Allocator, directory: []const u8, why: *[]const u8) Error!void {
    var names: std.ArrayList([]const u8) = .empty;
    {
        const fd = fsx.openDir(fsx.cwd, directory, false) catch |err| return fail(a, why, err, directory);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, directory);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, directory);
            const item = entry orelse break;
            if (!isPrivateName(item.name)) continue;
            const child = try std.fs.path.join(a, &.{ directory, item.name });
            const kind = main_mod.lstatKind(child);
            if (std.mem.eql(u8, kind, "file") or std.mem.eql(u8, kind, "symlink")) try names.append(a, child);
        }
    }
    for (names.items) |path| try discardPath(a, path, why);
}

const Group = struct { dev: u64, ino: u64, nlink: u32, names: std.ArrayList([]const u8) };

fn consider(a: Allocator, groups: *std.ArrayList(Group), path: []const u8, why: *[]const u8) Error!void {
    const info = fsx.statAt(fsx.cwd, path, false) catch |err| return fail(a, why, err, path);
    if (!info.isReg()) return;
    for (groups.items) |*group| if (group.dev == info.dev and group.ino == info.ino) {
        try group.names.append(a, path);
        return;
    };
    var names: std.ArrayList([]const u8) = .empty;
    try names.append(a, path);
    try groups.append(a, .{ .dev = info.dev, .ino = info.ino, .nlink = info.nlink, .names = names });
}

fn collect(a: Allocator, root: []const u8, groups: *std.ArrayList(Group), links: *std.ArrayList([]const u8), why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    {
        // the descriptor and the iterator are closed before the walk
        // descends, so one is held at a time whatever the tree's shape
        const fd = fsx.openDir(fsx.cwd, root, false) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            if (ours(item.name)) continue;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            const kind = main_mod.lstatKind(child);
            if (std.mem.eql(u8, kind, "symlink")) {
                try links.append(a, child);
            } else if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
            } else if (std.mem.eql(u8, kind, "file")) {
                try consider(a, groups, child, why);
            }
        }
    }
    for (subdirs.items) |dir| try collect(a, dir, groups, links, why);
}

fn containsChunked(fd: fsx.fd_t, old: []const u8) !bool {
    if (old.len == 0 or old.len > 4096) return error.Unexpected;
    var carry: [4096]u8 = undefined;
    var carry_len: usize = 0;
    var buf: [64 * 1024]u8 = undefined;
    while (true) {
        const n = linux.read(fd, &buf, buf.len);
        if (linux.errno(n) != .SUCCESS) return error.Unexpected;
        if (n == 0) return false;
        if (std.mem.indexOf(u8, buf[0..n], old) != null) return true;
        if (carry_len > 0) {
            var window: [4096 + 256]u8 = undefined;
            const take = @min(old.len, n);
            @memcpy(window[0..carry_len], carry[0..carry_len]);
            @memcpy(window[carry_len .. carry_len + take], buf[0..take]);
            if (std.mem.indexOf(u8, window[0 .. carry_len + take], old) != null) return true;
        }
        const keep = @min(old.len - 1, n);
        if (keep > 0) @memcpy(carry[0..keep], buf[n - keep .. n]);
        carry_len = keep;
    }
}

fn discardPath(a: Allocator, path: []const u8, why: *[]const u8) Error!void {
    sys.unlinkat(fsx.cwd, path, 0) catch |err| switch (err) {
        error.NotFound => return,
        else => return fail(a, why, err, path),
    };
}

fn pathLess(_: void, left: []const u8, right: []const u8) bool {
    return std.mem.order(u8, left, right) == .lt;
}

/// Finish `.<inode>.ebsr-ready` left by a killed publish, then drop private
/// link temporaries. The inode in the name is the inode that had not yet
/// been replaced. A `.ebsr-link` is another name for the replacement, and
/// leaving it makes the link count disagree with the files in the tree.
fn recoverReady(a: Allocator, root: []const u8, ready: []const u8, defer_dir: ?[]const u8, why: *[]const u8) Error!void {
    const inode = readyInode(ready) orelse return;
    var names: std.ArrayList([]const u8) = .empty;
    try namesOnInode(a, root, inode, ready, &names, why);
    if (names.items.len == 0) {
        try discardPath(a, ready, why);
        return;
    }
    std.mem.sort([]const u8, names.items, {}, pathLess);
    try commitNames(a, names.items, ready, defer_dir, why);
}

fn recoverTree(a: Allocator, root: []const u8, why: *[]const u8) Error!void {
    var readies: std.ArrayList([]const u8) = .empty;
    try sweepReady(a, root, &readies, true, why);
    for (readies.items) |ready| {
        const parent = std.fs.path.dirname(ready) orelse ".";
        var window = try openWritable(a, parent, why);
        const defer_dir: ?[]const u8 = if (window.hold != null) parent else null;
        const outcome = recoverReady(a, root, ready, defer_dir, why);
        try window.finish(parent, why);
        try outcome;
    }
    var gone: std.ArrayList([]const u8) = .empty;
    try sweepReady(a, root, &gone, false, why);
}

fn readyInode(ready: []const u8) ?u64 {
    const base = std.fs.path.basename(ready);
    if (base.len < 2 or base[0] != '.') return null;
    const rest = base[1..];
    const dot = std.mem.indexOfScalar(u8, rest, '.') orelse return null;
    if (!std.mem.eql(u8, rest[dot + 1 ..], "ebsr-ready")) return null;
    return std.fmt.parseInt(u64, rest[0..dot], 10) catch null;
}

fn sweepReady(a: Allocator, root: []const u8, found: *std.ArrayList([]const u8), keep_ready: bool, why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    var drops: std.ArrayList([]const u8) = .empty;
    {
        // closed before the descent, one descriptor at a time
        const fd = fsx.openDir(fsx.cwd, root, false) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            const kind = main_mod.lstatKind(child);
            if (std.mem.eql(u8, kind, "symlink")) continue;
            if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
                continue;
            }
            if (keep_ready and std.mem.startsWith(u8, item.name, ".") and std.mem.endsWith(u8, item.name, ".ebsr-ready")) {
                try found.append(a, child);
            } else if (!std.mem.endsWith(u8, item.name, ".ebsr-ready") and isPrivateName(item.name)) {
                try drops.append(a, child);
            }
        }
    }
    if (drops.items.len > 0) {
        var window = try openWritable(a, root, why);
        var dropped: Error = undefined;
        var drop_failed = false;
        for (drops.items) |child| {
            discardPath(a, child, why) catch |err| {
                dropped = err;
                drop_failed = true;
                break;
            };
        }
        try window.finish(root, why);
        if (drop_failed) return dropped;
    }
    for (subdirs.items) |dir| try sweepReady(a, dir, found, keep_ready, why);
}

fn namesOnInode(a: Allocator, root: []const u8, inode: u64, ready: []const u8, names: *std.ArrayList([]const u8), why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    {
        // closed before the descent, one descriptor at a time
        const fd = fsx.openDir(fsx.cwd, root, false) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            if (ours(item.name)) continue;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            if (std.mem.eql(u8, child, ready)) continue;
            const kind = main_mod.lstatKind(child);
            if (std.mem.eql(u8, kind, "symlink")) continue;
            if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
                continue;
            }
            if (!std.mem.eql(u8, kind, "file")) continue;
            const info = fsx.statAt(fsx.cwd, child, false) catch continue;
            if (info.isReg() and info.ino == inode) try names.append(a, child);
        }
    }
    for (subdirs.items) |dir| try namesOnInode(a, dir, inode, ready, names, why);
}

fn rewriteTree(a: Allocator, root: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    // A file that no longer contains the old prefix is skipped below, and
    // that skip used to leave its directory's journal unrestored. Finish
    // every leftover seal before recoverTree or a publish flushes anything.
    try finishSealed(a, root, why);
    if (std.mem.eql(u8, old, new)) return;
    try recoverTree(a, root, why);
    var groups: std.ArrayList(Group) = .empty;
    var links: std.ArrayList([]const u8) = .empty;
    try collect(a, root, &groups, &links, why);
    for (groups.items) |group| {
        if (group.nlink != group.names.items.len) {
            why.* = try a.print("{s} has {d} hard links and this tree holds {d}; refusing to split them", .{ group.names.items[0], group.nlink, group.names.items.len });
            return error.Refused;
        }
        const fd = sys.openat(fsx.cwd, group.names.items[0], .{ .ACCMODE = .RDONLY, .NOFOLLOW = true, .NONBLOCK = true }, 0) catch |err| return fail(a, why, err, group.names.items[0]);
        const info = fsx.fstat(fd) catch |err| {
            sys.close(fd);
            return fail(a, why, err, group.names.items[0]);
        };
        if (info.size > max_bytes) {
            const found = containsChunked(fd, old) catch |err| {
                sys.close(fd);
                return fail(a, why, err, group.names.items[0]);
            };
            sys.close(fd);
            if (!found) continue;
            why.* = try a.print("{s} is larger than 1 GiB and records the generation path", .{group.names.items[0]});
            return error.Refused;
        }
        const data = fsx.readAll(a, fd, max_bytes) catch |err| {
            sys.close(fd);
            return fail(a, why, err, group.names.items[0]);
        };
        sys.close(fd);
        if (std.mem.indexOf(u8, data, old) == null) continue;
        const updated = try transformed(a, data, old, new, group.names.items[0], why);
        if (std.mem.eql(u8, updated, data)) continue;
        try publish(a, group.names.items, updated, why);
    }
    for (links.items) |link| if (std.mem.eql(u8, main_mod.lstatKind(link), "symlink")) try rewriteSymlink(a, link, old, new, why);
}

/// rewrite_install_prefix: files under `staged` name `live`'s prefix.
pub fn rewriteInstall(a: Allocator, staged: []const u8, live: []const u8, why: *[]const u8) Error!void {
    const pair = try prefixPair(a, staged, live, why);
    try rewriteTree(a, staged, pair.old, pair.new, why);
}

test "a generation prefix becomes the stable prefix, and a tight binary is refused" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var why: []const u8 = "";
    const pair = try prefixPair(a, "/tmp/ex/state/generations/run1/software/demo/1", "/tmp/ex/software/demo/1", &why);
    try std.testing.expectEqualStrings("/tmp/ex/state/generations/run1", pair.old);
    try std.testing.expectEqualStrings("/tmp/ex", pair.new);
    const text = "prefix=/tmp/ex/state/generations/run1/software/demo/1\n";
    const replaced = try replacePrefix(a, text, pair.old, pair.new, &why);
    try std.testing.expectEqualStrings("prefix=/tmp/ex/software/demo/1\n", replaced.bytes);
    // The stable path is not scanned again, so a second pass changes nothing.
    const again = try replacePrefix(a, replaced.bytes, pair.old, pair.new, &why);
    try std.testing.expectEqual(@as(usize, 0), again.count);

    var binary: std.ArrayList(u8) = .empty;
    try binary.appendSlice(a, "\x00");
    try binary.appendSlice(a, "/tmp/ex/state/generations/run1/software/demo/1/lib");
    try binary.appendSlice(a, "\x00next\x00");
    const shrunk = try rewriteBinary(a, binary.items, pair.old, pair.new, "libdemo.so", &why);
    try std.testing.expect(std.mem.indexOf(u8, shrunk, "/tmp/ex/software/demo/1/lib") != null);
    try std.testing.expect(std.mem.indexOf(u8, shrunk, "generations") == null);
    try std.testing.expect(std.mem.indexOf(u8, shrunk, "next") != null);

    const tight = try replacePrefix(a, "/short/lib", "/short", "/much/longer/stable", &why);
    try std.testing.expectEqualStrings("/much/longer/stable/lib", tight.bytes);
    const tight_bin = "/short/lib\x00";
    try std.testing.expectError(error.Refused, rewriteBinary(a, tight_bin, "/short", "/much/longer/stable", "libdemo.so", &why));
    // Same length, different bytes: the C string is replaced, the NUL stays.
    const same = try rewriteBinary(a, "/aaa/lib\x00next\x00", "/aaa", "/bbb", "libdemo.so", &why);
    try std.testing.expectEqualStrings("/bbb/lib\x00next\x00", same);
}

const ReadOnlyChmod = struct {
    var real: *const fn (fsx.fd_t, u32) usize = undefined;

    /// A directory chmod is the refusal under test. A journal file is not.
    fn call(fd: fsx.fd_t, mode: u32) usize {
        const st = fsx.fstat(fd) catch return real(fd, mode);
        if (st.isDir()) return @as(usize, @bitCast(@as(isize, -@as(isize, @backingInt(linux.E.ROFS)))));
        return real(fd, mode);
    }
};

test "a read-only directory names that directory" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [64]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-ro-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        var restore: [sys.path_max:0]u8 = undefined;
        if (sys.z(&restore, root)) |z| {
            _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755);
        } else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
    }
    const old = "/state/generations/run1";
    const file = try std.fmt.allocPrint(a, "{s}/tool", .{root});
    const body = try std.fmt.allocPrint(a, "prefix {s} suffix", .{old});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, body);
        sys.close(fd);
    }
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o555)) == .SUCCESS);
    defer atomic.ops.* = .{};
    ReadOnlyChmod.real = atomic.ops.fchmod;
    atomic.ops.fchmod = ReadOnlyChmod.call;
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, rewriteTree(a, root, old, "/stable", &why));
    try std.testing.expect(std.mem.indexOf(u8, why, "[Errno 30] Read-only file system: '") != null);
    try std.testing.expect(std.mem.indexOf(u8, why, root) != null);
    atomic.ops.* = .{};
    const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    const text = try fsx.readAll(a, fd, 256);
    try std.testing.expectEqualStrings(body, text);
}

test "a sealed directory keeps its access list across a rewrite" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-{d}", .{linux.getpid()});
    var store_buf: [80]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-store-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| {
            _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755);
        } else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, store)) |z| {
            _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0);
        } else |_| {}
    }
    const old = "/state/generations/run1";
    const file = try std.fmt.allocPrint(a, "{s}/tool", .{root});
    const body = try std.fmt.allocPrint(a, "prefix {s}/bin\n", .{old});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, body);
        sys.close(fd);
    }
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o555)) == .SUCCESS);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    const want = "site-nfs4-acl";
    {
        const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], want)) == .SUCCESS);
    }
    var why: []const u8 = "";
    try rewriteTree(a, root, old, "/stable", &why);
    const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings(want, buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
    const text = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(text);
    const got = try fsx.readAll(a, text, 256);
    try std.testing.expectEqualStrings("prefix /stable/bin\n", got);
}

const SealFlush = struct {
    var chmod: *const fn (fsx.fd_t, u32) usize = undefined;
    var setxattr: *const fn (fsx.fd_t, [:0]const u8, []const u8) usize = undefined;
    var fail_restored: bool = false;
    /// The next restoring chmod arms one failed setxattr, the gap between
    /// the mode and the list that follows it.
    var fail_after_mode: bool = false;
    var fail_next_acl: bool = false;
    var order: [16]u8 = undefined;
    var order_len: usize = 0;
    var watch: [8]struct { dev: u64, ino: u64 } = undefined;
    var watch_len: usize = 0;
    /// Read when a directory is made writable, so a retry can show the
    /// journal it is about to keep.
    var capture_path: ?[]const u8 = null;
    var captured: [1024]u8 = undefined;
    var captured_len: usize = 0;
    /// When set, the first fsync of the destination parent must see the
    /// journal still present, and the first fsync of the source parent
    /// must come later, after the journal is gone.
    var track: bool = false;
    var dest_dev: u64 = 0;
    var dest_ino: u64 = 0;
    var source_dev: u64 = 0;
    var source_ino: u64 = 0;
    var journal: []const u8 = "";
    var saw_dest: bool = false;
    var saw_source: bool = false;
    var dest_before_source: bool = false;
    var journal_at_dest: bool = false;
    var journal_at_source: bool = false;

    fn reset() void {
        fail_restored = false;
        fail_after_mode = false;
        fail_next_acl = false;
        order_len = 0;
        watch_len = 0;
        capture_path = null;
        captured_len = 0;
        track = false;
        dest_dev = 0;
        dest_ino = 0;
        source_dev = 0;
        source_ino = 0;
        journal = "";
        saw_dest = false;
        saw_source = false;
        dest_before_source = false;
        journal_at_dest = false;
        journal_at_source = false;
    }

    fn text() []const u8 {
        return order[0..order_len];
    }

    fn note(mark: u8) void {
        if (order_len < order.len) {
            order[order_len] = mark;
            order_len += 1;
        }
    }

    fn arm() void {
        chmod = atomic.ops.fchmod;
        setxattr = atomic.ops.fsetxattr;
        atomic.ops.fchmod = onChmod;
        atomic.ops.fsetxattr = onSet;
        atomic.ops.fsync = onFsync;
    }

    /// The hold's open file description, distinguished from a later open of
    /// the same directory. O_NONBLOCK is a status flag of that description.
    fn markHold(fd: fsx.fd_t) void {
        const bit: usize = 1 << @bitOffsetOf(linux.O, "NONBLOCK");
        const cur = linux.fcntl(fd, linux.F.GETFL, 0);
        if (linux.errno(cur) != .SUCCESS) return;
        _ = linux.fcntl(fd, linux.F.SETFL, cur | bit);
    }

    fn marked(fd: fsx.fd_t) bool {
        const bit: usize = 1 << @bitOffsetOf(linux.O, "NONBLOCK");
        const cur = linux.fcntl(fd, linux.F.GETFL, 0);
        if (linux.errno(cur) != .SUCCESS) return false;
        return cur & bit != 0;
    }

    fn onChmod(fd: fsx.fd_t, mode: u32) usize {
        const rc = chmod(fd, mode);
        const st = fsx.fstat(fd) catch return rc;
        if (!st.isDir()) return rc;
        if (mode & 0o200 != 0) {
            markHold(fd);
            note('W');
            if (watch_len < watch.len) {
                watch[watch_len] = .{ .dev = st.dev, .ino = st.ino };
                watch_len += 1;
            }
            if (capture_path != null) readCapture();
        } else if (marked(fd)) {
            note('M');
            if (fail_after_mode) fail_next_acl = true;
        }
        return rc;
    }

    fn aclAttribute(name: []const u8) bool {
        for (atomic.acl_attributes) |acl| if (std.mem.eql(u8, name, acl)) return true;
        return false;
    }

    fn eio() usize {
        return @as(usize, @bitCast(@as(isize, -@as(isize, @backingInt(linux.E.IO)))));
    }

    fn readCapture() void {
        const path = capture_path orelse return;
        const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0) catch return;
        defer sys.close(fd);
        const n = sys.read(fd, &captured) catch return;
        captured_len = n;
    }

    fn onSet(fd: fsx.fd_t, name: [:0]const u8, value: []const u8) usize {
        if (fail_next_acl and aclAttribute(name)) {
            fail_next_acl = false;
            return eio();
        }
        const rc = setxattr(fd, name, value);
        if (linux.errno(rc) == .SUCCESS and marked(fd) and std.mem.eql(u8, name, atomic.acl_attributes[1]) and std.mem.eql(u8, value, "site-nfs4-acl")) note('A');
        return rc;
    }

    fn restoredAcl(fd: fsx.fd_t) bool {
        var buf: [64]u8 = undefined;
        const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
        return linux.errno(n) == .SUCCESS and std.mem.eql(u8, buf[0..n], "site-nfs4-acl");
    }

    fn watched(st: fsx.Stat) bool {
        var i: usize = 0;
        while (i < watch_len) : (i += 1) {
            if (watch[i].dev == st.dev and watch[i].ino == st.ino) return true;
        }
        return false;
    }

    fn journalExists() bool {
        return std.mem.eql(u8, main_mod.lstatKind(journal), "file");
    }

    fn noteParents(st: fsx.Stat) void {
        if (!saw_dest and st.dev == dest_dev and st.ino == dest_ino) {
            saw_dest = true;
            journal_at_dest = journalExists();
        } else if (saw_dest and !saw_source and st.dev == source_dev and st.ino == source_ino) {
            saw_source = true;
            dest_before_source = true;
            journal_at_source = journalExists();
        }
    }

    fn onFsync(fd: fsx.fd_t) usize {
        const st = fsx.fstat(fd) catch return linux.fsync(fd);
        if (track and st.isDir()) noteParents(st);
        if (st.isDir() and watched(st)) {
            // F is a flush after the mode and the list are both back. B is
            // a flush of that directory while the replaced list can still
            // be what a reader observes.
            const back = st.perm() & 0o200 == 0 and restoredAcl(fd) and std.mem.indexOfScalar(u8, text(), 'M') != null and std.mem.indexOfScalar(u8, text(), 'A') != null;
            if (back) {
                note('F');
                if (fail_restored) return eio();
            } else note('B');
        } else if (st.isDir() and marked(fd)) note('H');
        return linux.fsync(fd);
    }
};

fn sealedRewrite(a: Allocator, root: []const u8, store: []const u8, why: *[]const u8) !void {
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    SealFlush.arm();
    const old = "/state/generations/run1";
    const file = try std.fmt.allocPrint(a, "{s}/tool", .{root});
    const body = try std.fmt.allocPrint(a, "prefix {s}/bin\n", .{old});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, body);
        sys.close(fd);
    }
    var zb: [sys.path_max:0]u8 = undefined;
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o555)) == .SUCCESS);
    const want = "site-nfs4-acl";
    {
        const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], want)) == .SUCCESS);
    }
    try rewriteTree(a, root, old, "/stable", why);
}

test "a sealed directory is flushed after its access list is restored" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-sync-{d}", .{linux.getpid()});
    var store_buf: [80]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-sync-store-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| {
            _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755);
        } else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, store)) |z| {
            _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0);
        } else |_| {}
    }
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    SealFlush.reset();
    var why: []const u8 = "";
    try sealedRewrite(a, root, store, &why);
    const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings("site-nfs4-acl", buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
    // W makes the directory writable, A writes the list, M restores the
    // mode, A writes the list again, F flushes the directory after that.
    // B would be a flush while the list is still the replaced one.
    try std.testing.expectEqualStrings("WAMAF", SealFlush.text());
    try std.testing.expect(std.mem.indexOfScalar(u8, SealFlush.text(), 'B') == null);
}

test "a failed flush of a restored sealed directory refuses the rewrite" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-sync-fail-{d}", .{linux.getpid()});
    var store_buf: [88]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-sync-fail-store-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| {
            _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755);
        } else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, store)) |z| {
            _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0);
        } else |_| {}
    }
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    SealFlush.reset();
    SealFlush.fail_restored = true;
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, sealedRewrite(a, root, store, &why));
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
    try std.testing.expect(std.mem.indexOf(u8, why, root) != null);
}

test "a killed list restore retries from the original journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-kill-{d}", .{linux.getpid()});
    var store_buf: [80]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-kill-store-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(root)});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755) else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, store)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
        if (sys.z(&zb, seal)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
    }
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    SealFlush.reset();
    SealFlush.arm();
    defer SealFlush.capture_path = null;
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o555)) == .SUCCESS);
    {
        const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    }
    SealFlush.fail_after_mode = true;
    var why: []const u8 = "";
    var held = (try holdDirectory(a, root, &why)).?;
    try std.testing.expectError(error.Refused, finishHold(&held, root, &why));
    SealFlush.fail_after_mode = false;
    SealFlush.capture_path = seal;
    var again = (try holdDirectory(a, root, &why)).?;
    try std.testing.expect(std.mem.indexOf(u8, SealFlush.captured[0..SealFlush.captured_len], "736974652d6e6673342d61636c") != null);
    try finishHold(&again, root, &why);
    const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings("site-nfs4-acl", buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(seal), "missing"));
}

test "a sealed parent is flushed only after its access list is restored" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const outer = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-nest-{d}", .{linux.getpid()});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{outer});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-nest-store-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, outer)).ptr, 0o755);
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, inner)).ptr, 0o755);
    defer {
        if (sys.z(&zb, outer)) |z| _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755) else |_| {}
        if (sys.z(&zb, inner)) |z| _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755) else |_| {}
        fsx.deleteTree(a, fsx.cwd, outer) catch {};
        if (sys.z(&zb, store)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
    }
    const file = try std.fmt.allocPrint(a, "{s}/tool", .{inner});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, "prefix /state/generations/run1/bin\n");
        sys.close(fd);
    }
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    SealFlush.reset();
    SealFlush.arm();
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, outer)).ptr, 0o555)) == .SUCCESS);
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, inner)).ptr, 0o555)) == .SUCCESS);
    for ([_][]const u8{ outer, inner }) |dir| {
        const fd = try sys.openat(fsx.cwd, dir, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    }
    var why: []const u8 = "";
    try rewriteTree(a, outer, "/state/generations/run1", "/stable", &why);
    const traced = SealFlush.text();
    try std.testing.expect(std.mem.indexOf(u8, traced, "WAMAF") != null);
    try std.testing.expect(std.mem.indexOfScalar(u8, traced, 'B') == null);
}

const original_journal = "ebsr-seal 1\nmode 365\n0 -\n1 736974652d6e6673342d61636c\n2 -\n";

fn nfs4(store: []const u8) void {
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
}

fn putFile(path: []const u8, body: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
    try sys.writeAll(fd, body);
    sys.close(fd);
}

fn putBytes(path: []const u8, body: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .NOFOLLOW = true }, 0o644);
    try sys.writeAll(fd, body);
    sys.close(fd);
}

fn mkdirAt(path: []const u8) !void {
    var zb: [sys.path_max:0]u8 = undefined;
    try std.testing.expect(linux.errno(linux.mkdirat(fsx.cwd, (try sys.z(&zb, path)).ptr, 0o755)) == .SUCCESS);
}

fn modeAt(path: []const u8, mode: u32) void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return;
    _ = linux.fchmodat(fsx.cwd, z.ptr, mode);
}

fn unlinkAt(path: []const u8) void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return;
    _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0);
}

/// The live list is the one a mode change wrote. The journal still holds
/// the original list and mode 0555.
fn replacedList(dir: []const u8, journal_path: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, dir, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    try std.testing.expect(linux.errno(atomic.ops.fchmod(fd, 0o755)) == .SUCCESS);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expect(!std.mem.eql(u8, buf[0..n], "site-nfs4-acl"));
    try putBytes(journal_path, original_journal);
}

fn expectSite(dir: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, dir, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings("site-nfs4-acl", buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
}

test "a resumed nested tree restores a writable outer list" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const outer = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-resume-{d}", .{linux.getpid()});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{outer});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-resume-store-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(outer)});
    defer {
        modeAt(inner, 0o755);
        modeAt(outer, 0o755);
        fsx.deleteTree(a, fsx.cwd, outer) catch {};
        unlinkAt(store);
        unlinkAt(seal);
    }
    try mkdirAt(outer);
    try mkdirAt(inner);
    try putFile(try std.fmt.allocPrint(a, "{s}/tool", .{inner}), "prefix /state/generations/run1/bin\n");
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try replacedList(outer, seal);
    modeAt(inner, 0o555);
    var why: []const u8 = "";
    try rewriteTree(a, outer, "/state/generations/run1", "/stable", &why);
    try expectSite(outer);
}

test "a retry that rewrites only a child restores the outer list" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const outer = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-child-{d}", .{linux.getpid()});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{outer});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-child-store-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(outer)});
    defer {
        modeAt(inner, 0o755);
        modeAt(outer, 0o755);
        fsx.deleteTree(a, fsx.cwd, outer) catch {};
        unlinkAt(store);
        unlinkAt(seal);
    }
    try mkdirAt(outer);
    try mkdirAt(inner);
    try putFile(try std.fmt.allocPrint(a, "{s}/done", .{outer}), "prefix /stable/bin\n");
    try putFile(try std.fmt.allocPrint(a, "{s}/tool", .{inner}), "prefix /state/generations/run1/bin\n");
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try replacedList(outer, seal);
    var why: []const u8 = "";
    try rewriteTree(a, outer, "/state/generations/run1", "/stable", &why);
    try expectSite(outer);
}

test "a nested leftover seal is restored when its files are already rewritten" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-left-{d}", .{linux.getpid()});
    const sub = try std.fmt.allocPrint(a, "{s}/sub", .{root});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-left-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.sub.ebsr-seal", .{root});
    defer {
        modeAt(sub, 0o755);
        modeAt(root, 0o755);
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        unlinkAt(store);
    }
    try mkdirAt(root);
    try mkdirAt(sub);
    try putFile(try std.fmt.allocPrint(a, "{s}/tool", .{sub}), "prefix /stable/bin\n");
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try replacedList(sub, seal);
    var why: []const u8 = "";
    try rewriteTree(a, root, "/state/generations/run1", "/stable", &why);
    try expectSite(sub);
}

fn armParents(dest: []const u8, source: []const u8, journal: []const u8) !void {
    const dest_st = try fsx.statAt(fsx.cwd, dest, false);
    const source_st = try fsx.statAt(fsx.cwd, source, false);
    SealFlush.reset();
    SealFlush.arm();
    SealFlush.track = true;
    SealFlush.dest_dev = dest_st.dev;
    SealFlush.dest_ino = dest_st.ino;
    SealFlush.source_dev = source_st.dev;
    SealFlush.source_ino = source_st.ino;
    SealFlush.journal = journal;
}

fn expectDestFirst() !void {
    try std.testing.expect(SealFlush.saw_dest);
    try std.testing.expect(SealFlush.saw_source);
    try std.testing.expect(SealFlush.dest_before_source);
    try std.testing.expect(SealFlush.journal_at_dest);
    try std.testing.expect(!SealFlush.journal_at_source);
}

test "a renamed sealed directory flushes the destination parent before the source parent" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const src = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-ren-src-{d}", .{linux.getpid()});
    var db: [96]u8 = undefined;
    const dst = try std.fmt.bufPrint(&db, "/tmp/prefix-acl-ren-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-ren-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{src});
    defer {
        modeAt(tree, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
        unlinkAt(store);
        unlinkAt(seal);
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    modeAt(tree, 0o555);
    {
        const fd = try sys.openat(fsx.cwd, tree, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    }
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try armParents(dst, src, seal);
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    try expectDestFirst();
    try expectSite(moved);
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(seal), "missing"));
}

test "a retry flushes the destination parent before it drops the source journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const src = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-retry-src-{d}", .{linux.getpid()});
    var db: [96]u8 = undefined;
    const dst = try std.fmt.bufPrint(&db, "/tmp/prefix-acl-retry-dst-{d}", .{linux.getpid()});
    const recorded = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-retry-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{src});
    defer {
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
        unlinkAt(store);
        unlinkAt(seal);
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(moved);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try replacedList(moved, seal);
    try armParents(dst, src, seal);
    var why: []const u8 = "";
    try commitRecorded(a, moved, recorded, dst, &why);
    try expectDestFirst();
    try expectSite(moved);
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(seal), "missing"));
}

test "a sealed directory journal with a missing value token is refused" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-token-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(root)});
    defer {
        modeAt(root, 0o755);
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        unlinkAt(seal);
    }
    try mkdirAt(root);
    try putBytes(seal, "ebsr-seal 1\nmode 493\n0\n1 -\n2 -\n");
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, holdDirectory(a, root, &why));
    try std.testing.expectEqualStrings("a sealed directory journal is unreadable", why);
}

test "a sealed directory journal with an empty value is an empty list" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-empty-{d}", .{linux.getpid()});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-acl-empty-store-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(root)});
    defer {
        modeAt(root, 0o755);
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        unlinkAt(store);
        unlinkAt(seal);
    }
    try mkdirAt(root);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try putBytes(seal, "ebsr-seal 1\nmode 365\n0 -\n1 \n2 -\n");
    var why: []const u8 = "";
    var held = (try holdDirectory(a, root, &why)).?;
    try finishHold(&held, root, &why);
    const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqual(@as(usize, 0), n);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
}

test "a seal named ....ebsr-seal is refused" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [96]u8 = undefined;
    const outer = try std.fmt.bufPrint(&pb, "/tmp/prefix-seal-dot-{d}", .{linux.getpid()});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{outer});
    const trap = try std.fmt.allocPrint(a, "{s}/....ebsr-seal", .{inner});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-seal-dot-store-{d}", .{linux.getpid()});
    defer {
        modeAt(inner, 0o755);
        modeAt(outer, 0o755);
        fsx.deleteTree(a, fsx.cwd, outer) catch {};
        unlinkAt(store);
    }
    try mkdirAt(outer);
    try mkdirAt(inner);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const keep = "keep-parent";
    {
        const fd = try sys.openat(fsx.cwd, outer, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], keep)) == .SUCCESS);
    }
    try putBytes(trap, original_journal);
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, finishNestedSeals(a, inner, &why));
    try std.testing.expectEqualStrings("a seal name escapes its directory", why);
    const fd = try sys.openat(fsx.cwd, outer, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings(keep, buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.fstat(fd)).perm());
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(trap), "file"));
}

test "a sealed directory journal that omits a form is refused" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-acl-form-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(root)});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, seal)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
    }
    {
        const fd = try sys.openat(fsx.cwd, seal, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true }, 0o644);
        try sys.writeAll(fd, "ebsr-seal 1\nmode 493\n0 -\n2 -\n");
        sys.close(fd);
    }
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, holdDirectory(a, root, &why));
    try std.testing.expectEqualStrings("a sealed directory journal is unreadable", why);
}

const RetryTrace = struct {
    var setxattr: *const fn (fsx.fd_t, [:0]const u8, []const u8) usize = undefined;
    var fsync_fn: *const fn (fsx.fd_t) usize = undefined;
    var marks: [8]u8 = undefined;
    var len: usize = 0;

    fn reset() void {
        len = 0;
    }

    fn text() []const u8 {
        return marks[0..len];
    }

    fn note(mark: u8) void {
        if (len < marks.len) {
            marks[len] = mark;
            len += 1;
        }
    }

    fn arm() void {
        setxattr = atomic.ops.fsetxattr;
        fsync_fn = atomic.ops.fsync;
        atomic.ops.fsetxattr = onSet;
        atomic.ops.fsync = onFsync;
    }

    fn onSet(fd: fsx.fd_t, name: [:0]const u8, value: []const u8) usize {
        const rc = setxattr(fd, name, value);
        const st = fsx.fstat(fd) catch return rc;
        if (st.isDir() and std.mem.eql(u8, name, atomic.acl_attributes[1]) and std.mem.eql(u8, value, "site-nfs4-acl")) note('A');
        return rc;
    }

    fn onFsync(fd: fsx.fd_t) usize {
        const st = fsx.fstat(fd) catch return fsync_fn(fd);
        if (st.isDir()) {
            var buf: [64]u8 = undefined;
            const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
            if (linux.errno(n) == .SUCCESS and std.mem.eql(u8, buf[0..n], "site-nfs4-acl")) note('F');
        }
        return fsync_fn(fd);
    }
};

test "a retry restores a replaced access list before the directory is flushed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-retry-{d}", .{linux.getpid()});
    var store_buf: [80]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-retry-store-{d}", .{linux.getpid()});
    var seal_buf: [96]u8 = undefined;
    const seal = try std.fmt.bufPrint(&seal_buf, "/tmp/.{s}.ebsr-seal", .{std.fs.path.basename(root)});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| {
            _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755);
        } else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
        if (sys.z(&zb, store)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
        if (sys.z(&zb, seal)) |z| _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0) else |_| {}
    }
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            return null;
        }
    };
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    const want = "site-nfs4-acl";
    {
        const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], want)) == .SUCCESS);
        try std.testing.expect(linux.errno(atomic.ops.fchmod(fd, 0o755)) == .SUCCESS);
    }
    const journal = "ebsr-seal 1\nmode 365\n0 -\n1 736974652d6e6673342d61636c\n2 -\n";
    {
        const fd = try sys.openat(fsx.cwd, seal, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .NOFOLLOW = true }, 0o644);
        try sys.writeAll(fd, journal);
        sys.close(fd);
    }
    RetryTrace.reset();
    RetryTrace.arm();
    const old = "/state/generations/run1";
    const file = try std.fmt.allocPrint(a, "{s}/tool", .{root});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, "prefix /state/generations/run1/bin\n");
        sys.close(fd);
    }
    var why: []const u8 = "";
    try rewriteTree(a, root, old, "/stable", &why);
    const traced = RetryTrace.text();
    const at_a = std.mem.indexOfScalar(u8, traced, 'A') orelse return error.TestExpectedEqual;
    const at_f = std.mem.indexOfScalar(u8, traced, 'F') orelse return error.TestExpectedEqual;
    try std.testing.expect(at_a < at_f);
    const fd = try sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [64]u8 = undefined;
    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings(want, buf[0..n]);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.fstat(fd)).perm());
}

test "a second rewrite succeeds after a sealed publish fails" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-retry-pub-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    const sub = try std.fmt.allocPrint(a, "{s}/sub", .{root});
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, sub)).ptr, 0o755);
    defer {
        if (sys.z(&zb, root)) |z| _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755) else |_| {}
        if (sys.z(&zb, sub)) |z| _ = linux.fchmodat(fsx.cwd, z.ptr, 0o755) else |_| {}
        fsx.deleteTree(a, fsx.cwd, root) catch {};
    }
    const old = "/state/generations/run1";
    const body = "prefix /state/generations/run1/bin\n";
    const updated = "prefix /stable/bin\n";
    const file = try std.fmt.allocPrint(a, "{s}/a", .{root});
    const link = try std.fmt.allocPrint(a, "{s}/z", .{sub});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, body);
        sys.close(fd);
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, (try sys.z(&zf, file)).ptr, linux.AT.FDCWD, (try sys.z(&zt, link)).ptr, 0)) == .SUCCESS);
    }
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o555)) == .SUCCESS);
    try std.testing.expect(linux.errno(linux.fchmodat(fsx.cwd, (try sys.z(&zb, sub)).ptr, 0o555)) == .SUCCESS);
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, rewriteTree(a, root, old, "/stable", &why));
    const first = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    const first_text = try fsx.readAll(a, first, 256);
    sys.close(first);
    const second = try sys.openat(fsx.cwd, link, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    const second_text = try fsx.readAll(a, second, 256);
    sys.close(second);
    try std.testing.expect(std.mem.eql(u8, first_text, updated) != std.mem.eql(u8, second_text, updated));
    try rewriteTree(a, root, old, "/stable", &why);
    const done = try sys.openat(fsx.cwd, link, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(done);
    try std.testing.expectEqualStrings(updated, try fsx.readAll(a, done, 256));
}

test "an interrupted hard-link publish is finished on the next rewrite" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [64]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-resume-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer fsx.deleteTree(a, fsx.cwd, root) catch {};
    const bindir = try std.fmt.allocPrint(a, "{s}/bin", .{root});
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, bindir)).ptr, 0o755);
    const old = "/state/generations/run1";
    const body = try std.fmt.allocPrint(a, "#!{s}/bin/python\n", .{old});
    const updated = "#!/stable/bin/python\n";
    const file = try std.fmt.allocPrint(a, "{s}/python", .{bindir});
    const alias = try std.fmt.allocPrint(a, "{s}/python3", .{bindir});
    const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
    try sys.writeAll(fd, body);
    sys.close(fd);
    {
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, (try sys.z(&zf, file)).ptr, linux.AT.FDCWD, (try sys.z(&zt, alias)).ptr, 0)) == .SUCCESS);
    }
    const ino = (try fsx.statAt(fsx.cwd, file, false)).ino;
    const ready = try std.fmt.allocPrint(a, "{s}/.{d}.ebsr-ready", .{ bindir, ino });
    {
        const rfd = try sys.openat(fsx.cwd, ready, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(rfd, updated);
        sys.close(rfd);
    }
    sys.unlinkat(fsx.cwd, file, 0) catch {};
    {
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, (try sys.z(&zf, ready)).ptr, linux.AT.FDCWD, (try sys.z(&zt, file)).ptr, 0)) == .SUCCESS);
    }
    const leftover = try std.fmt.allocPrint(a, "{s}/.python3.999.ebsr-link", .{bindir});
    {
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, (try sys.z(&zf, ready)).ptr, linux.AT.FDCWD, (try sys.z(&zt, leftover)).ptr, 0)) == .SUCCESS);
    }
    var why: []const u8 = "";
    try rewriteTree(a, root, old, "/stable", &why);
    const got = try fsx.statAt(fsx.cwd, file, false);
    const alias_st = try fsx.statAt(fsx.cwd, alias, false);
    try std.testing.expectEqual(got.ino, alias_st.ino);
    try std.testing.expect(got.nlink == 2);
    const rfd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(rfd);
    const text = try fsx.readAll(a, rfd, 1024);
    try std.testing.expectEqualStrings(updated, text);
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(leftover), "missing"));
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(ready), "missing"));
    try rewriteTree(a, root, old, "/stable", &why);
    const again = try fsx.statAt(fsx.cwd, file, false);
    try std.testing.expectEqual(got.ino, again.ino);
}

test "a rewritten tree keeps mode, holes, hard links and replaces a symlink by rename" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [64]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/prefix-zig-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o755);
    defer fsx.deleteTree(a, fsx.cwd, root) catch {};
    const file = try std.fmt.allocPrint(a, "{s}/demo", .{root});
    const old = "/state/generations/run1";
    const body = try std.fmt.allocPrint(a, "#!{s}/bin/python\n", .{old});
    {
        const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o644);
        try sys.writeAll(fd, body);
        try std.testing.expect(linux.errno(atomic.ops.fchmod(fd, 0o444)) == .SUCCESS);
        sys.close(fd);
    }
    const alias = try std.fmt.allocPrint(a, "{s}/python3", .{root});
    {
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        const left = try sys.z(&zf, file);
        const right = try sys.z(&zt, alias);
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, left.ptr, linux.AT.FDCWD, right.ptr, 0)) == .SUCCESS);
    }
    const link = try std.fmt.allocPrint(a, "{s}/alias", .{root});
    {
        const target = try std.fmt.allocPrint(a, "{s}/bin/python", .{old});
        var zt: [sys.path_max:0]u8 = undefined;
        var zp: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.symlinkat((try sys.z(&zt, target)).ptr, linux.AT.FDCWD, (try sys.z(&zp, link)).ptr)) == .SUCCESS);
    }
    const sparse = try std.fmt.allocPrint(a, "{s}/sparse", .{root});
    {
        const sfd = try sys.openat(fsx.cwd, sparse, .{ .ACCMODE = .RDWR, .CREAT = true, .EXCL = true }, 0o644);
        const blob = try std.fmt.allocPrint(a, "{s}/lib\x00next\x00", .{old});
        try sys.writeAll(sfd, blob);
        try std.testing.expect(linux.errno(linux.lseek(sfd, 1024 * 1024, linux.SEEK.SET)) == .SUCCESS);
        try sys.writeAll(sfd, "END");
        sys.close(sfd);
    }
    var why: []const u8 = "";
    try rewriteTree(a, root, old, "/stable", &why);
    const fd = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    const got = try fsx.readAll(a, fd, 1024);
    try std.testing.expectEqualStrings("#!/stable/bin/python\n", got);
    const st = try fsx.fstat(fd);
    try std.testing.expectEqual(@as(u32, 0o444), st.perm());
    const alias_st = try fsx.statAt(fsx.cwd, alias, false);
    try std.testing.expectEqual(st.ino, alias_st.ino);
    try std.testing.expect(st.nlink == 2);
    const linked = try readlinkExact(a, link);
    try std.testing.expectEqualStrings("/stable/bin/python", linked);
    const sfd = try sys.openat(fsx.cwd, sparse, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(sfd);
    var stx: linux.Statx = undefined;
    try std.testing.expect(linux.errno(linux.statx(sfd, "", linux.AT.EMPTY_PATH, .{ .BLOCKS = true, .SIZE = true }, &stx)) == .SUCCESS);
    try std.testing.expect(stx.blocks < 128);
    var head: [64]u8 = undefined;
    const n = linux.read(sfd, &head, head.len);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expect(std.mem.startsWith(u8, head[0..n], "/stable/lib\x00"));
}

test "collect crosses a deep tree under a tight descriptor limit" {
    const gpa = std.testing.allocator;
    const seed: u64 = 0x5EEDC0;
    var prng = std.Random.DefaultPrng.init(seed);
    const rand = prng.random();
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(gpa, "{s}/.zig-cache/ebsr-prefixdeep-{d}", .{ here, linux.getpid() });
    defer gpa.free(base);
    try common.atomic.makedirs(base);

    var names: std.ArrayList(u8) = .empty;
    defer names.deinit(gpa);
    var zb: [sys.path_max:0]u8 = undefined;
    var fd = try fsx.openDir(fsx.cwd, base, false);
    defer sys.close(fd);
    var depth: usize = 0;
    while (depth < 1300) : (depth += 1) {
        const c: u8 = 'a' + @as(u8, @intCast(rand.uintLessThan(u8, 26)));
        try names.append(gpa, c);
        const nm = names.items[names.items.len - 1 ..];
        _ = try sys.call(linux.mkdirat, .{ fd, (try sys.z(&zb, nm)).ptr, @as(linux.mode_t, 0o755) });
        const sub = try fsx.openDir(fd, nm, false);
        sys.close(fd);
        fd = sub;
    }
    const keep = try sys.openat(fd, "keep.bin", .{ .ACCMODE = .WRONLY, .CREAT = true }, 0o644);
    try sys.writeAll(keep, "deep\n");
    sys.close(keep);

    var old: linux.rlimit = undefined;
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.prlimit(0, .NOFILE, null, &old)));
    const lim: linux.rlimit = .{ .cur = 1024, .max = old.max };
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.prlimit(0, .NOFILE, &lim, null)));
    defer _ = linux.prlimit(0, .NOFILE, &old, null);

    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var groups: std.ArrayList(Group) = .empty;
    var links: std.ArrayList([]const u8) = .empty;
    var why: []const u8 = "";
    collect(a, base, &groups, &links, &why) catch |err| {
        std.debug.print("collect walk seed: {d}: gave {s} ({s}) errno E{s}\n", .{ seed, @errorName(err), why, @tagName(sys.last_errno) });
        return err;
    };
    var found = false;
    for (groups.items) |g| {
        for (g.names.items) |n| if (std.mem.endsWith(u8, n, "/keep.bin")) {
            found = true;
        };
    }
    if (!found) return error.TestUnexpectedResult;

    var full: std.ArrayList(u8) = .empty;
    defer full.deinit(gpa);
    try full.appendSlice(gpa, base);
    for (names.items) |ch| {
        try full.append(gpa, '/');
        try full.append(gpa, ch);
    }
    try full.appendSlice(gpa, "/keep.bin");
    var fz: [sys.path_max:0]u8 = undefined;
    _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, full.items), 0);
    full.shrinkRetainingCapacity(full.items.len - "/keep.bin".len);
    while (full.items.len > base.len) {
        _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, full.items), linux.AT.REMOVEDIR);
        full.shrinkRetainingCapacity(full.items.len - 2);
    }
    _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, base), linux.AT.REMOVEDIR);
}

const TempSwap = struct {
    var saved: *const fn (fsx.fd_t, u32) usize = undefined;
    var path: [sys.path_max]u8 = undefined;
    var path_len: usize = 0;
    var once: bool = false;

    fn arm() void {
        saved = atomic.ops.fchmod;
        atomic.ops.fchmod = onChmod;
        once = true;
        path_len = 0;
    }

    fn eio() usize {
        return @bitCast(-@as(isize, @backingInt(linux.E.IO)));
    }

    fn onChmod(fd: fsx.fd_t, mode: u32) usize {
        if (once) {
            var buf: [sys.path_max]u8 = undefined;
            if (fsx.realpathFd(fd, &buf)) |got| {
                if (std.mem.indexOf(u8, got, ".tmp") != null) {
                    once = false;
                    @memcpy(path[0..got.len], got);
                    path_len = got.len;
                    const name = path[0..path_len];
                    sys.unlinkat(fsx.cwd, name, 0) catch return eio();
                    const created = sys.openat(fsx.cwd, name, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true }, 0o644) catch return eio();
                    sys.writeAll(created, "replacement\n") catch {
                        sys.close(created);
                        return eio();
                    };
                    sys.close(created);
                    return eio();
                }
            } else |_| {}
        }
        return saved(fd, mode);
    }
};

test "a replaced temporary name survives a failed seal write" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var pb: [80]u8 = undefined;
    const dir_path = try std.fmt.bufPrint(&pb, "/tmp/prefix-temp-swap-{d}", .{linux.getpid()});
    try mkdirAt(dir_path);
    defer fsx.deleteTree(a, fsx.cwd, dir_path) catch {};
    const dir = try sys.openat(fsx.cwd, dir_path, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(dir);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    TempSwap.arm();
    try std.testing.expectError(error.Unexpected, atomic.atomicWriteAtOpts(a, dir, "note", "body\n", 0o600, .{ .sync_parent = false, .check_close = true }));
    try std.testing.expect(TempSwap.path_len > 0);
    const kept = try sys.openat(fsx.cwd, TempSwap.path[0..TempSwap.path_len], .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(kept);
    try std.testing.expectEqualStrings("replacement\n", try fsx.readAll(a, kept, 64));
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(try std.fmt.allocPrint(a, "{s}/note", .{dir_path})), "missing"));
}

fn refusedSealClose(a: Allocator, label: []const u8, flush: []const u8) ![]const u8 {
    var fused = try faultmount.mountExtra(a, label, "", &.{flush});
    defer fused.stop();
    const dir = try std.fmt.allocPrint(a, "{s}/sealed", .{fused.mnt});
    try mkdirAt(dir);
    modeAt(dir, 0o555);
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    var why: []const u8 = "";
    if (holdDirectory(a, dir, &why)) |maybe| {
        if (maybe) |hold| {
            var h = hold;
            h.abandon();
        }
        return error.TestUnexpectedResult;
    } else |err| {
        try std.testing.expectEqual(error.Refused, err);
        try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, dir, false)).perm());
        return why;
    }
}

test "a failed close of a seal file refuses the directory update" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const why = try refusedSealClose(arena_state.allocator(), "close-eio", "FAULTFS_FLUSH=eio");
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
}

test "a seal close interrupted by a signal reports that errno" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const why = try refusedSealClose(arena_state.allocator(), "close-eintr", "FAULTFS_FLUSH=eintr");
    try std.testing.expect(std.mem.indexOf(u8, why, "Interrupted system call") != null);
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") == null);
}

const RenameOrder = struct {
    var saved: *const fn (fsx.fd_t) usize = undefined;
    var paths: [24][sys.path_max]u8 = undefined;
    var lens: [24]usize = undefined;
    var len: usize = 0;

    fn reset() void {
        len = 0;
    }

    fn arm() void {
        saved = atomic.ops.fsync;
        atomic.ops.fsync = onFsync;
        len = 0;
    }

    fn onFsync(fd: fsx.fd_t) usize {
        if (len < paths.len) {
            if (fsx.realpathFd(fd, &paths[len])) |got| {
                lens[len] = got.len;
                len += 1;
            } else |_| {}
        }
        return saved(fd);
    }

    fn find(path: []const u8) ?usize {
        var i: usize = 0;
        while (i < len) : (i += 1) {
            if (std.mem.eql(u8, paths[i][0..lens[i]], path)) return i;
        }
        return null;
    }

    fn sealBefore(at: usize) bool {
        var i: usize = 0;
        while (i < at and i < len) : (i += 1) {
            if (std.mem.indexOf(u8, paths[i][0..lens[i]], "ebsr-seal") != null) return true;
        }
        return false;
    }
};

test "a leftover nested seal is unlinked before the renamed directory is flushed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var sb: [96]u8 = undefined;
    const src = try std.fmt.bufPrint(&sb, "/tmp/prefix-nest-ord-src-{d}", .{linux.getpid()});
    var db: [96]u8 = undefined;
    const dst = try std.fmt.bufPrint(&db, "/tmp/prefix-nest-ord-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const sub = try std.fmt.allocPrint(a, "{s}/sub", .{tree});
    const moved_sub = try std.fmt.allocPrint(a, "{s}/sub", .{moved});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-nest-ord-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.sub.ebsr-seal", .{tree});
    const src_seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{src});
    const dst_seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{dst});
    defer {
        modeAt(sub, 0o755);
        modeAt(moved_sub, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
        unlinkAt(store);
        unlinkAt(seal);
        unlinkAt(src_seal);
        unlinkAt(dst_seal);
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(sub);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try replacedList(sub, seal);
    modeAt(tree, 0o555);
    {
        const fd = try sys.openat(fsx.cwd, tree, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    }
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    RenameOrder.reset();
    RenameOrder.arm();
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    var moved_buf: [sys.path_max]u8 = undefined;
    var dst_buf: [sys.path_max]u8 = undefined;
    var src_buf: [sys.path_max]u8 = undefined;
    const moved_real = try fsx.realpath(fsx.cwd, moved, &moved_buf);
    const dst_real = try fsx.realpath(fsx.cwd, dst, &dst_buf);
    const src_real = try fsx.realpath(fsx.cwd, src, &src_buf);
    const at_tree = RenameOrder.find(moved_real) orelse return error.TestExpectedEqual;
    const at_dst = RenameOrder.find(dst_real) orelse return error.TestExpectedEqual;
    const at_src = RenameOrder.find(src_real) orelse return error.TestExpectedEqual;
    try std.testing.expect(!RenameOrder.sealBefore(at_tree));
    try std.testing.expect(at_tree < at_dst);
    try std.testing.expect(at_dst < at_src);
    try expectSite(moved_sub);
    try expectSite(moved);
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(dst_seal), "missing"));
}

test "a seal target stays the directory that was opened" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var fused = try faultmount.mountExtra(a, "seal-fd", "", &.{"FAULTFS_SWAP_AFTER_CLOSE=/inner/sub"});
    defer fused.stop();
    // Build the tree on the backing store. A create through the mount would
    // close /inner/sub and arm the swap before the seal is finished.
    const back_inner = try std.fmt.allocPrint(a, "{s}/back/inner", .{fused.work});
    const back_sub = try std.fmt.allocPrint(a, "{s}/sub", .{back_inner});
    const back_decoy = try std.fmt.allocPrint(a, "{s}/sub.decoy", .{back_inner});
    const back_seal = try std.fmt.allocPrint(a, "{s}/.sub.ebsr-seal", .{back_inner});
    try mkdirAt(back_inner);
    try mkdirAt(back_sub);
    try mkdirAt(back_decoy);
    modeAt(back_sub, 0o755);
    modeAt(back_decoy, 0o755);
    try putBytes(back_seal, "ebsr-seal 1\nmode 365\n0 -\n1 -\n2 -\n");
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{fused.mnt});
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    var why: []const u8 = "";
    try finishNestedSeals(a, inner, &why);
    const real = try std.fmt.allocPrint(a, "{s}/back/inner/sub", .{fused.work});
    const real_decoy = try std.fmt.allocPrint(a, "{s}/back/inner/sub.decoy", .{fused.work});
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, real, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, real_decoy, false)).perm());
}

const FirstTreeFlush = struct {
    var saved: *const fn (fsx.fd_t) usize = undefined;
    var dev: u64 = 0;
    var ino: u64 = 0;
    var seen: bool = false;
    var perm: u32 = 0;
    var acl_ok: bool = false;

    fn arm(st: fsx.Stat) void {
        saved = atomic.ops.fsync;
        atomic.ops.fsync = onFsync;
        dev = st.dev;
        ino = st.ino;
        seen = false;
        perm = 0;
        acl_ok = false;
    }

    fn onFsync(fd: fsx.fd_t) usize {
        if (!seen) {
            if (fsx.fstat(fd)) |st| {
                if (st.dev == dev and st.ino == ino) {
                    seen = true;
                    perm = st.perm();
                    var buf: [64]u8 = undefined;
                    const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
                    acl_ok = linux.errno(n) == .SUCCESS and std.mem.eql(u8, buf[0..n], "site-nfs4-acl");
                }
            } else |_| {}
        }
        return saved(fd);
    }
};

test "a two-level leftover seal restores the renamed directory before its first flush" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var sb: [96]u8 = undefined;
    const src = try std.fmt.bufPrint(&sb, "/tmp/prefix-two-src-{d}", .{linux.getpid()});
    var db: [96]u8 = undefined;
    const dst = try std.fmt.bufPrint(&db, "/tmp/prefix-two-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-two-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid});
    const src_seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{src});
    defer {
        modeAt(leaf, 0o755);
        modeAt(mid, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
        unlinkAt(store);
        unlinkAt(seal);
        unlinkAt(src_seal);
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try putBytes(seal, original_journal);
    modeAt(mid, 0o555);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    modeAt(tree, 0o555);
    {
        const fd = try sys.openat(fsx.cwd, tree, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
    }
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    const tree_st = try fsx.fstat(held.fd);
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    FirstTreeFlush.arm(tree_st);
    try finishRenamed(&held, moved, dst, &why);
    try std.testing.expect(FirstTreeFlush.seen);
    try std.testing.expectEqual(@as(u32, 0o555), FirstTreeFlush.perm);
    try std.testing.expect(FirstTreeFlush.acl_ok);
    try expectSite(moved);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, moved_mid, false)).perm());
}

fn expectClosedPathRefuses(a: Allocator, label: []const u8, retry: bool) !void {
    var fused = try faultmount.mountExtra(a, label, "", &.{"FAULTFS_SWAP_AFTER_CLOSE=/dst/tree"});
    defer fused.stop();
    const back_src = try std.fmt.allocPrint(a, "{s}/back/src", .{fused.work});
    const back_dst = try std.fmt.allocPrint(a, "{s}/back/dst", .{fused.work});
    const back_decoy = try std.fmt.allocPrint(a, "{s}/tree.decoy", .{back_dst});
    const back_seal = try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{back_src});
    try mkdirAt(back_src);
    try mkdirAt(back_dst);
    try mkdirAt(back_decoy);
    modeAt(back_decoy, 0o755);
    const mnt_src = try std.fmt.allocPrint(a, "{s}/src", .{fused.mnt});
    const mnt_dst = try std.fmt.allocPrint(a, "{s}/dst", .{fused.mnt});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_dst});
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    var why: []const u8 = "";
    const real = if (retry) try std.fmt.allocPrint(a, "{s}/tree", .{back_dst}) else try std.fmt.allocPrint(a, "{s}/tree", .{back_dst});
    if (retry) {
        try mkdirAt(real);
        modeAt(real, 0o755);
        try putBytes(back_seal, "ebsr-seal 1\nmode 365\n0 -\n1 -\n2 -\n");
        try std.testing.expectError(error.Refused, commitRecorded(a, moved, tree, mnt_dst, &why));
    } else {
        const back_tree = try std.fmt.allocPrint(a, "{s}/tree", .{back_src});
        try mkdirAt(back_tree);
        modeAt(back_tree, 0o555);
        var held = (try holdDirectory(a, tree, &why)).?;
        try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
        try std.testing.expectError(error.Refused, finishRenamed(&held, moved, mnt_dst, &why));
    }
    try std.testing.expect(std.mem.indexOf(u8, why, "a sealed directory changed before its journal was removed") != null);
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, real, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, back_decoy, false)).perm());
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(back_seal), "file"));
    modeAt(real, 0o755);
}

test "a renamed directory refuses a path that was closed before its list was flushed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    try expectClosedPathRefuses(arena_state.allocator(), "swap-ren", false);
}

test "a recorded rename refuses a path that was closed before its list was flushed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    try expectClosedPathRefuses(arena_state.allocator(), "swap-retry", true);
}

fn rewrittenHasDiscard(a: std.mem.Allocator, src: []const u8) !void {
    const out = try @import("atomic_edit.zig").rewrite(a, src);
    defer if (out.ptr != src.ptr) a.free(out);
    try std.testing.expect(std.mem.indexOf(u8, out, "pub fn closeChecked(") != null);
    try std.testing.expect(std.mem.indexOf(u8, out, "fn discard(dir:") != null);
}

test "an earlier rewrite of the shared package still gains directory discard" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try rewrittenHasDiscard(a, @embedFile("testdata/atomic-510ac5f4.zig"));
    try rewrittenHasDiscard(a, @embedFile("testdata/atomic-r10.zig"));
    try rewrittenHasDiscard(a, @embedFile("testdata/atomic-v0.14.0.zig"));
}

fn rewrittenClosesTemp(a: std.mem.Allocator, src: []const u8) !void {
    const out = try @import("atomic_edit.zig").rewrite(a, src);
    defer if (out.ptr != src.ptr) a.free(out);
    const write_at = std.mem.indexOf(u8, out, "pub fn atomicWrite(") orelse return error.TestExpectedEqual;
    const rename_at = std.mem.indexOfPos(u8, out, write_at, "renameTemp(") orelse return error.TestExpectedEqual;
    const body = out[write_at..rename_at];
    const at = std.mem.indexOf(u8, out, "pub fn atomicWriteAtOpts(") orelse return error.TestExpectedEqual;
    const at_rename = std.mem.indexOfPos(u8, out, at, "ops.renameat") orelse return error.TestExpectedEqual;
    const at_body = out[at..at_rename];
    const write_ok = std.mem.indexOf(u8, body, "opts.check_close") != null and std.mem.indexOf(u8, body, "closeChecked(") != null and std.mem.indexOf(u8, body, "closeTemp(") != null;
    const at_ok = std.mem.indexOf(u8, at_body, "closeTemp(") != null and std.mem.indexOf(u8, at_body, "closeChecked(") != null and std.mem.indexOf(u8, at_body, "closeDiscarded(") != null and std.mem.indexOf(u8, at_body, "sys.close(") == null;
    if (!write_ok or !at_ok) std.debug.print("held close before rename: write={} at={}\n", .{ write_ok, at_ok });
    try std.testing.expect(write_ok);
    try std.testing.expect(at_ok);
}

test "a later package closes the temporary file before the rename" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try rewrittenClosesTemp(a, @embedFile("testdata/atomic-v0.14.0.zig"));
    const older = try @import("atomic_edit.zig").rewrite(a, @embedFile("testdata/atomic-510ac5f4.zig"));
    defer if (older.ptr != @embedFile("testdata/atomic-510ac5f4.zig").ptr) a.free(older);
    const write_at = std.mem.indexOf(u8, older, "pub fn atomicWrite(") orelse return error.TestExpectedEqual;
    const rename_at = std.mem.indexOfPos(u8, older, write_at, "renameTemp(") orelse return error.TestExpectedEqual;
    const body = older[write_at..rename_at];
    try std.testing.expect(std.mem.indexOf(u8, body, "opts.check_close") != null);
    try std.testing.expect(std.mem.indexOf(u8, body, "closeChecked(") != null);
}

const leaf_journal = "ebsr-seal 1\nmode 493\n0 -\n1 -\n2 -\n";

fn sealKept(path: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, path, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    try std.testing.expect(linux.errno(atomic.ops.fchmod(fd, 0o555)) == .SUCCESS);
    try std.testing.expect(linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[1], "site-nfs4-acl")) == .SUCCESS);
}

fn expectStoreAcl(a: Allocator, store: []const u8, ino: u64) !void {
    const fd = try sys.openat(fsx.cwd, store, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    const text = try fsx.readAll(a, fd, 1 << 20);
    defer a.free(text);
    var hex: [64]u8 = undefined;
    const h = std.fmt.bufPrint(&hex, "{x}", .{"site-nfs4-acl"}) catch unreachable;
    var needle: [96]u8 = undefined;
    const want = std.fmt.bufPrint(&needle, ":{d} system.nfs4_acl {s}", .{ ino, h }) catch unreachable;
    try std.testing.expect(std.mem.indexOf(u8, text, want) != null);
}

/// A close of `swap` makes later lookups of that path use the decoy. The
/// real directory must come back sealed, with its list, and the update
/// must be refused.
fn expectSwappedSeal(a: Allocator, label: []const u8, intermediate: bool) !void {
    const swap = if (intermediate) "/dst/tree/mid" else "/dst/tree/mid";
    var fused = try faultmount.mountExtra(a, label, "", &.{try std.fmt.allocPrint(a, "FAULTFS_SWAP_AFTER_CLOSE={s}", .{swap})});
    defer fused.stop();
    const back_src = try std.fmt.allocPrint(a, "{s}/back/src", .{fused.work});
    const back_dst = try std.fmt.allocPrint(a, "{s}/back/dst", .{fused.work});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{back_src});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const holder = if (intermediate) try std.fmt.allocPrint(a, "{s}/inner", .{mid}) else mid;
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{holder});
    const seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{holder});
    const decoy_root = try std.fmt.allocPrint(a, "{s}/mid.decoy", .{tree});
    const decoy_holder = if (intermediate) try std.fmt.allocPrint(a, "{s}/inner", .{decoy_root}) else decoy_root;
    try mkdirAt(back_src);
    try mkdirAt(back_dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    if (intermediate) try mkdirAt(holder);
    try mkdirAt(leaf);
    try putBytes(seal, leaf_journal);
    try mkdirAt(decoy_root);
    if (intermediate) try mkdirAt(decoy_holder);
    try mkdirAt(try std.fmt.allocPrint(a, "{s}/leaf", .{decoy_holder}));
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{decoy_holder}), leaf_journal);
    modeAt(holder, 0o555);
    modeAt(mid, 0o555);
    modeAt(decoy_holder, 0o755);
    modeAt(decoy_root, 0o755);
    const mnt_src = try std.fmt.allocPrint(a, "{s}/src", .{fused.mnt});
    const mnt_tree = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_src});
    const mnt_dst = try std.fmt.allocPrint(a, "{s}/dst", .{fused.mnt});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_dst});
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const mnt_mid = try std.fmt.allocPrint(a, "{s}/mid", .{mnt_tree});
    const mnt_holder = if (intermediate) try std.fmt.allocPrint(a, "{s}/inner", .{mnt_mid}) else mnt_mid;
    try sealKept(mnt_holder);
    if (intermediate) try sealKept(mnt_mid);
    modeAt(try std.fmt.allocPrint(a, "{s}/tree", .{back_src}), 0o555);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, mnt_tree, &why)).?;
    try sys.renameat(fsx.cwd, mnt_tree, fsx.cwd, moved);
    try std.testing.expectError(error.Refused, finishRenamed(&held, moved, mnt_dst, &why));
    const real_holder = try std.fmt.allocPrint(a, "{s}/tree/{s}", .{ back_dst, if (intermediate) "mid/inner" else "mid" });
    const real_mid = try std.fmt.allocPrint(a, "{s}/tree/mid", .{back_dst});
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, real_holder, false)).perm());
    try expectStoreAcl(a, store, (try fsx.statAt(fsx.cwd, real_holder, false)).ino);
    if (intermediate) {
        try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, real_mid, false)).perm());
        try expectStoreAcl(a, store, (try fsx.statAt(fsx.cwd, real_mid, false)).ino);
    }
    const moved_decoy = if (intermediate)
        try std.fmt.allocPrint(a, "{s}/tree/mid.decoy/inner", .{back_dst})
    else
        try std.fmt.allocPrint(a, "{s}/tree/mid.decoy", .{back_dst});
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, moved_decoy, false)).perm());
    modeAt(real_holder, 0o755);
    modeAt(real_mid, 0o755);
}

test "a sealed journal parent refuses a path swapped after it was closed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    try expectSwappedSeal(arena_state.allocator(), "swap-parent", false);
}

test "a sealed intermediate directory refuses a path swapped after it was closed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    try expectSwappedSeal(arena_state.allocator(), "swap-mid", true);
}

var restore_fault: [sys.path_max + 64]u8 = undefined;
var restore_fault_len: usize = 0;

test "a failed restore of a sealed parent keeps its list and its journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var sb: [96]u8 = undefined;
    const src = try std.fmt.bufPrint(&sb, "/tmp/prefix-restore-src-{d}", .{linux.getpid()});
    var db: [96]u8 = undefined;
    const dst = try std.fmt.bufPrint(&db, "/tmp/prefix-restore-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-restore-store-{d}", .{linux.getpid()});
    const seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid});
    defer {
        modeAt(mid, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
        unlinkAt(store);
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try putBytes(seal, leaf_journal);
    modeAt(mid, 0o555);
    modeAt(tree, 0o555);
    var moved_real: [sys.path_max]u8 = undefined;
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    const Env = struct {
        store: []const u8,
        pub fn get(self: @This(), name: []const u8) ?[]const u8 {
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
            if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_FAULT")) return restore_fault[0..restore_fault_len];
            return null;
        }
    };
    nfs4(store);
    try sealKept(mid);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    const real = try fsx.realpath(fsx.cwd, moved_mid, &moved_real);
    const fault = std.fmt.bufPrint(&restore_fault, "op=setxattr,path={s},nth=1,errno=IO", .{real}) catch return error.NameTooLong;
    restore_fault_len = fault.len;
    @import("aclmodel.zig").installFromEnv(Env{ .store = store });
    try std.testing.expectError(error.Refused, finishRenamed(&held, moved, dst, &why));
    try expectSite(moved_mid);
    try std.testing.expect(std.mem.eql(u8, main_mod.lstatKind(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{moved_mid})), "file"));
}

fn fileAt(path: []const u8) bool {
    return std.mem.eql(u8, main_mod.lstatKind(path), "file");
}

const FaultEnv = struct {
    store: []const u8,
    pub fn get(self: @This(), name: []const u8) ?[]const u8 {
        if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_MODEL")) return "nfs4-generic";
        if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_STORE")) return self.store;
        if (std.mem.eql(u8, name, "EB_STAGED_REBUILD_ACL_FAULT")) return restore_fault[0..restore_fault_len];
        return null;
    }
};

// An injected setxattr error must leave the recorded list and the child
// journal in place, and a second sealed branch under the same ancestor
// must still be able to finish.
test "a nested seal error keeps the recorded list and the child journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-nest-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);

    const src = try std.fmt.allocPrint(a, "/tmp/prefix-nest-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-nest-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{mid});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{inner});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const moved_inner = try std.fmt.allocPrint(a, "{s}/inner", .{moved_mid});
    const moved_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{moved_inner});
    const leaf_seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{moved_inner});
    defer {
        modeAt(leaf, 0o755);
        modeAt(inner, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved_leaf, 0o755);
        modeAt(moved_inner, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(inner);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{inner}), original_journal);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.inner.ebsr-seal", .{mid}), original_journal);
    try sealKept(leaf);
    try sealKept(inner);
    try sealKept(mid);
    try sealKept(tree);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    var moved_real: [sys.path_max]u8 = undefined;
    const real = try fsx.realpath(fsx.cwd, moved_inner, &moved_real);
    const fault = std.fmt.bufPrint(&restore_fault, "op=setxattr,path={s},nth=3,errno=IO", .{real}) catch return error.NameTooLong;
    restore_fault_len = fault.len;
    @import("aclmodel.zig").installFromEnv(FaultEnv{ .store = store });
    try std.testing.expectError(error.Refused, finishRenamed(&held, moved, dst, &why));
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
    try expectSite(moved_inner);
    try std.testing.expect(fileAt(leaf_seal));

    const src2 = try std.fmt.allocPrint(a, "/tmp/prefix-branch-src-{d}", .{linux.getpid()});
    const dst2 = try std.fmt.allocPrint(a, "/tmp/prefix-branch-dst-{d}", .{linux.getpid()});
    const tree2 = try std.fmt.allocPrint(a, "{s}/tree", .{src2});
    const moved2 = try std.fmt.allocPrint(a, "{s}/tree", .{dst2});
    const mid2 = try std.fmt.allocPrint(a, "{s}/mid", .{tree2});
    const left = try std.fmt.allocPrint(a, "{s}/left", .{mid2});
    const right = try std.fmt.allocPrint(a, "{s}/right", .{mid2});
    const left_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{left});
    const right_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{right});
    const moved2_left = try std.fmt.allocPrint(a, "{s}/mid/left/leaf", .{moved2});
    const moved2_right = try std.fmt.allocPrint(a, "{s}/mid/right/leaf", .{moved2});
    const moved2_left_dir = try std.fmt.allocPrint(a, "{s}/mid/left", .{moved2});
    const moved2_right_dir = try std.fmt.allocPrint(a, "{s}/mid/right", .{moved2});
    const moved2_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved2});
    defer {
        modeAt(left_leaf, 0o755);
        modeAt(right_leaf, 0o755);
        modeAt(left, 0o755);
        modeAt(right, 0o755);
        modeAt(mid2, 0o755);
        modeAt(tree2, 0o755);
        modeAt(moved2_left, 0o755);
        modeAt(moved2_right, 0o755);
        modeAt(moved2_left_dir, 0o755);
        modeAt(moved2_right_dir, 0o755);
        modeAt(moved2_mid, 0o755);
        modeAt(moved2, 0o755);
        fsx.deleteTree(a, fsx.cwd, src2) catch {};
        fsx.deleteTree(a, fsx.cwd, dst2) catch {};
    }
    try mkdirAt(src2);
    try mkdirAt(dst2);
    try mkdirAt(tree2);
    try mkdirAt(mid2);
    try mkdirAt(left);
    try mkdirAt(right);
    try mkdirAt(left_leaf);
    try mkdirAt(right_leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{left}), original_journal);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{right}), original_journal);
    try sealKept(left_leaf);
    try sealKept(right_leaf);
    try sealKept(left);
    try sealKept(right);
    try sealKept(mid2);
    try sealKept(tree2);
    const quiet = std.fmt.bufPrint(&restore_fault, "op=setxattr,path=/no/such/fault,nth=1,errno=IO", .{}) catch return error.NameTooLong;
    restore_fault_len = quiet.len;
    @import("aclmodel.zig").installFromEnv(FaultEnv{ .store = store });
    why = "";
    var held2 = (try holdDirectory(a, tree2, &why)).?;
    try sys.renameat(fsx.cwd, tree2, fsx.cwd, moved2);
    try finishRenamed(&held2, moved2, dst2, &why);
    try expectSite(moved2_left);
    try expectSite(moved2_right);
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{try std.fmt.allocPrint(a, "{s}/mid/left", .{moved2})})));
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{try std.fmt.allocPrint(a, "{s}/mid/right", .{moved2})})));
}

test "a three-level release leaves no journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-deep-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-deep-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-deep-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{mid});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{inner});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const moved_inner = try std.fmt.allocPrint(a, "{s}/inner", .{moved_mid});
    const moved_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{moved_inner});
    const stale = try std.fmt.allocPrint(a, "{s}/.inner.ebsr-seal", .{moved_mid});
    defer {
        modeAt(leaf, 0o755);
        modeAt(inner, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved_leaf, 0o755);
        modeAt(moved_inner, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(inner);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{inner}), original_journal);
    try sealKept(leaf);
    try sealKept(inner);
    try sealKept(mid);
    try sealKept(tree);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    try std.testing.expect(!fileAt(stale));
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{moved})));
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{moved_inner})));
    modeAt(moved_inner, 0o755);
    try std.testing.expect((try holdDirectory(a, moved_inner, &why)) == null);
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, moved_inner, false)).perm());
}

test "a planted journal is restored instead of rewritten from the live list" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-plant-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-plant-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-plant-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    defer {
        modeAt(leaf, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid}), leaf_journal);
    try sealKept(mid);
    {
        const fd = try sys.openat(fsx.cwd, mid, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
        defer sys.close(fd);
        try std.testing.expect(linux.errno(atomic.ops.fchmod(fd, 0o555)) == .SUCCESS);
    }
    try putBytes(try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{tree}), original_journal);
    try sealKept(tree);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    try expectSite(moved_mid);
}

/// The directory was chmod'd and the journal was already on disk when the
/// process stopped. A later flush of a child has to put that record back
/// before it syncs the directory.
const RecordedSync = struct {
    var saved: *const fn (sys.fd_t) usize = undefined;
    var dev: u64 = 0;
    var ino: u64 = 0;
    var saw: bool = false;

    fn arm(st: fsx.Stat) void {
        saved = atomic.ops.fsync;
        atomic.ops.fsync = onFsync;
        dev = st.dev;
        ino = st.ino;
        saw = false;
    }

    fn onFsync(fd: sys.fd_t) usize {
        const st = fsx.fstat(fd) catch return saved(fd);
        if (st.isDir() and st.dev == dev and st.ino == ino and st.perm() & 0o200 == 0) {
            var buf: [64]u8 = undefined;
            const n = atomic.ops.fgetxattr(fd, atomic.acl_attributes[1], &buf);
            if (linux.errno(n) == .SUCCESS and std.mem.eql(u8, buf[0..n], "site-nfs4-acl")) saw = true;
        }
        return saved(fd);
    }
};

test "a crash after makeWritable restores the on-disk journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-crash-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-crash-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-crash-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{mid});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{inner});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const moved_inner = try std.fmt.allocPrint(a, "{s}/inner", .{moved_mid});
    const moved_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{moved_inner});
    defer {
        modeAt(leaf, 0o755);
        modeAt(inner, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved_leaf, 0o755);
        modeAt(moved_inner, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(inner);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{inner}), original_journal);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.inner.ebsr-seal", .{mid}), original_journal);
    try sealKept(inner);
    try sealKept(leaf);
    try replacedList(mid, try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{tree}));
    try sealKept(tree);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    var moved_real: [sys.path_max]u8 = undefined;
    const real = try fsx.realpath(fsx.cwd, moved_inner, &moved_real);
    const fault = std.fmt.bufPrint(&restore_fault, "op=setxattr,path={s},nth=1,errno=IO", .{real}) catch return error.NameTooLong;
    restore_fault_len = fault.len;
    @import("aclmodel.zig").installFromEnv(FaultEnv{ .store = store });
    const st = try fsx.statAt(fsx.cwd, moved_mid, false);
    RecordedSync.arm(st);
    try std.testing.expectError(error.Refused, finishRenamed(&held, moved, dst, &why));
    try std.testing.expect(RecordedSync.saw);
    try std.testing.expect(fileAt(try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{moved})));
}

test "a sealed directory is not reopened after its parent is closed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var fused = try faultmount.mountExtra(a, "reopen", "", &.{"FAULTFS_SWAP_AFTER_CLOSE=/inner"});
    defer fused.stop();
    const back = try std.fmt.allocPrint(a, "{s}/back", .{fused.work});
    const back_inner = try std.fmt.allocPrint(a, "{s}/inner", .{back});
    const back_sub = try std.fmt.allocPrint(a, "{s}/sub", .{back_inner});
    const decoy = try std.fmt.allocPrint(a, "{s}/inner.decoy", .{back});
    const decoy_sub = try std.fmt.allocPrint(a, "{s}/sub", .{decoy});
    try mkdirAt(back_inner);
    try mkdirAt(back_sub);
    try mkdirAt(decoy);
    try mkdirAt(decoy_sub);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.inner.ebsr-seal", .{back}), original_journal);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.sub.ebsr-seal", .{back_inner}), original_journal);
    modeAt(back_inner, 0o555);
    modeAt(back_sub, 0o755);
    modeAt(decoy, 0o755);
    modeAt(decoy_sub, 0o755);
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{fused.mnt});
    var why: []const u8 = "";
    finishSealed(a, inner, &why) catch {};
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, back_sub, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, decoy_sub, false)).perm());
}

test "a stat error on a sealed directory reports its errno" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var fused = try faultmount.mountExtra(a, "stat-err", "", &.{"FAULTFS_STAT_AFTER_CLOSE=/dst/tree/mid"});
    defer fused.stop();
    const back_src = try std.fmt.allocPrint(a, "{s}/back/src", .{fused.work});
    const back_dst = try std.fmt.allocPrint(a, "{s}/back/dst", .{fused.work});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{back_src});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    try mkdirAt(back_src);
    try mkdirAt(back_dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid}), original_journal);
    modeAt(mid, 0o555);
    modeAt(tree, 0o555);
    const mnt_src = try std.fmt.allocPrint(a, "{s}/src", .{fused.mnt});
    const mnt_tree = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_src});
    const mnt_dst = try std.fmt.allocPrint(a, "{s}/dst", .{fused.mnt});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{mnt_dst});
    const store = try std.fmt.allocPrint(a, "{s}/acl-store", .{fused.work});
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    try sealKept(try std.fmt.allocPrint(a, "{s}/mid", .{mnt_tree}));
    var why: []const u8 = "";
    var held = (try holdDirectory(a, mnt_tree, &why)).?;
    try sys.renameat(fsx.cwd, mnt_tree, fsx.cwd, moved);
    try std.testing.expectError(error.Refused, finishRenamed(&held, moved, mnt_dst, &why));
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
    try std.testing.expect(std.mem.indexOf(u8, why, "changed before its journal") == null);
    const real_seal = try std.fmt.allocPrint(a, "{s}/dst/tree/mid/.leaf.ebsr-seal", .{try std.fmt.allocPrint(a, "{s}/back", .{fused.work})});
    try std.testing.expect(fileAt(real_seal));

    const src = try std.fmt.allocPrint(a, "/tmp/prefix-long-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-long-dst-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved_root = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    defer {
        modeAt(root, 0o755);
        modeAt(moved_root, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(root);
    var dir = root;
    var depth: usize = 0;
    while (depth < 40) : (depth += 1) {
        dir = try std.fmt.allocPrint(a, "{s}/d", .{dir});
        try mkdirAt(dir);
    }
    const bottom = try std.fmt.allocPrint(a, "{s}/leaf", .{dir});
    try mkdirAt(bottom);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{dir}), leaf_journal);
    try sealKept(root);
    why = "";
    var deep = (try holdDirectory(a, root, &why)).?;
    try sys.renameat(fsx.cwd, root, fsx.cwd, moved_root);
    try std.testing.expectError(error.Refused, finishRenamed(&deep, moved_root, dst, &why));
    try std.testing.expect(std.mem.indexOf(u8, why, "32") != null or std.mem.indexOf(u8, why, "longer") != null);
}

/// The journal is written into the parent. That parent is flushed before
/// the child's mode gains the write bit.
const ParentFlush = struct {
    var saved_fsync: *const fn (sys.fd_t) usize = undefined;
    var saved_chmod: *const fn (sys.fd_t, u32) usize = undefined;
    var tree_dev: u64 = 0;
    var tree_ino: u64 = 0;
    var mid_dev: u64 = 0;
    var mid_ino: u64 = 0;
    var journal: []const u8 = "";
    var chmod_seen: bool = false;
    var synced: bool = false;

    fn arm(tree: fsx.Stat, mid: fsx.Stat, journal_path: []const u8) void {
        saved_fsync = atomic.ops.fsync;
        saved_chmod = atomic.ops.fchmod;
        atomic.ops.fsync = onFsync;
        atomic.ops.fchmod = onChmod;
        tree_dev = tree.dev;
        tree_ino = tree.ino;
        mid_dev = mid.dev;
        mid_ino = mid.ino;
        journal = journal_path;
        chmod_seen = false;
        synced = false;
    }

    fn onFsync(fd: sys.fd_t) usize {
        const st = fsx.fstat(fd) catch return saved_fsync(fd);
        if (st.isDir() and st.dev == tree_dev and st.ino == tree_ino and !chmod_seen and fileAt(journal)) synced = true;
        return saved_fsync(fd);
    }

    fn onChmod(fd: sys.fd_t, mode: u32) usize {
        const st = fsx.fstat(fd) catch return saved_chmod(fd, mode);
        if (st.isDir() and st.dev == mid_dev and st.ino == mid_ino and mode & 0o200 != 0) chmod_seen = true;
        return saved_chmod(fd, mode);
    }
};

test "a journal is flushed to its parent before the mode change" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-psync-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-psync-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-psync-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const journal = try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{moved});
    defer {
        modeAt(leaf, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid}), original_journal);
    try sealKept(leaf);
    try sealKept(mid);
    try sealKept(tree);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    ParentFlush.arm(try fsx.statAt(fsx.cwd, moved, false), try fsx.statAt(fsx.cwd, moved_mid, false), journal);
    try finishRenamed(&held, moved, dst, &why);
    try std.testing.expect(ParentFlush.synced);
    try std.testing.expect(ParentFlush.chmod_seen);
}

const TraceRegs = extern struct {
    r15: u64,
    r14: u64,
    r13: u64,
    r12: u64,
    rbp: u64,
    rbx: u64,
    r11: u64,
    r10: u64,
    r9: u64,
    r8: u64,
    rax: u64,
    rcx: u64,
    rdx: u64,
    rsi: u64,
    rdi: u64,
    orig_rax: u64,
    rip: u64,
    cs: u64,
    eflags: u64,
    rsp: u64,
    ss: u64,
    fs_base: u64,
    gs_base: u64,
    ds: u64,
    es: u64,
    fs: u64,
    gs: u64,
};

fn fdPathOf(pid: linux.pid_t, fdnum: u64, buf: []u8) ?[]const u8 {
    var linkbuf: [80]u8 = undefined;
    const link = std.fmt.bufPrint(&linkbuf, "/proc/{d}/fd/{d}", .{ pid, fdnum }) catch return null;
    var z: [80:0]u8 = undefined;
    if (link.len >= z.len) return null;
    @memcpy(z[0..link.len], link);
    z[link.len] = 0;
    const n = linux.readlinkat(linux.AT.FDCWD, z[0..link.len :0].ptr, buf.ptr, buf.len);
    if (linux.errno(n) != .SUCCESS or n == 0 or n == buf.len) return null;
    return buf[0..n];
}

fn pathIsDir(path: []const u8) bool {
    var zbuf: [sys.path_max:0]u8 = undefined;
    if (path.len >= zbuf.len) return false;
    @memcpy(zbuf[0..path.len], path);
    zbuf[path.len] = 0;
    var x: linux.Statx = undefined;
    const mask: linux.STATX = .{ .TYPE = true };
    const rc = linux.statx(linux.AT.FDCWD, zbuf[0..path.len :0].ptr, linux.AT.SYMLINK_NOFOLLOW, mask, &x);
    if (linux.errno(rc) != .SUCCESS) return false;
    return linux.S.ISDIR(x.mode);
}

fn dupCount(pid: linux.pid_t, want: []const u8) usize {
    var dirbuf: [64]u8 = undefined;
    const dirpath = std.fmt.bufPrint(&dirbuf, "/proc/{d}/fd", .{pid}) catch return 1;
    var z: [64:0]u8 = undefined;
    if (dirpath.len >= z.len) return 1;
    @memcpy(z[0..dirpath.len], dirpath);
    z[dirpath.len] = 0;
    const raw = linux.openat(linux.AT.FDCWD, z[0..dirpath.len :0].ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(raw) != .SUCCESS) return 1;
    const dfd: linux.fd_t = @intCast(raw);
    defer _ = linux.close(dfd);
    var dents: [4096]u8 align(8) = undefined;
    var count: usize = 0;
    while (true) {
        const n = linux.getdents64(dfd, &dents, dents.len);
        if (linux.errno(n) != .SUCCESS or n == 0) break;
        var off: usize = 0;
        while (off < n) {
            const ent: *align(1) const linux.dirent64 = @ptrCast(dents[off..].ptr);
            if (ent.reclen == 0) return count;
            const name_ptr: [*:0]const u8 = @ptrCast(dents[off + @offsetOf(linux.dirent64, "name") ..].ptr);
            const name = std.mem.span(name_ptr);
            if (!(name.len == 1 and name[0] == '.') and !(name.len == 2 and name[0] == '.' and name[1] == '.')) {
                var linkbuf: [96]u8 = undefined;
                const link = std.fmt.bufPrint(&linkbuf, "/proc/{d}/fd/{s}", .{ pid, name }) catch {
                    off += ent.reclen;
                    continue;
                };
                var lz: [96:0]u8 = undefined;
                if (link.len < lz.len) {
                    @memcpy(lz[0..link.len], link);
                    lz[link.len] = 0;
                    var got: [sys.path_max]u8 = undefined;
                    const nn = linux.readlinkat(linux.AT.FDCWD, lz[0..link.len :0].ptr, &got, got.len);
                    if (linux.errno(nn) == .SUCCESS and std.mem.eql(u8, got[0..nn], want)) count += 1;
                }
            }
            off += ent.reclen;
        }
    }
    return count;
}

fn watchedDir(path: []const u8, root: []const u8) bool {
    if (path.len < root.len or !std.mem.startsWith(u8, path, root)) return false;
    if (std.mem.indexOf(u8, path, ".ebsr") != null) return false;
    if (std.mem.indexOf(u8, path, ".tmp") != null) return false;
    return pathIsDir(path);
}

// The first close error is the one reported. A later close must not
// replace that errno.
test "the first interrupted close is not reported as an io error" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var store_buf: [96]u8 = undefined;
    const store = try std.fmt.bufPrint(&store_buf, "/tmp/prefix-eintr-store-{d}", .{linux.getpid()});
    defer unlinkAt(store);
    const previous = atomic.ops.*;
    defer atomic.ops.* = previous;
    nfs4(store);
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-eintr-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-eintr-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{mid});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{inner});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const moved_inner = try std.fmt.allocPrint(a, "{s}/inner", .{moved_mid});
    const moved_leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{moved_inner});
    defer {
        modeAt(leaf, 0o755);
        modeAt(inner, 0o755);
        modeAt(mid, 0o755);
        modeAt(tree, 0o755);
        modeAt(moved, 0o755);
        modeAt(moved_mid, 0o755);
        modeAt(moved_inner, 0o755);
        modeAt(moved_leaf, 0o755);
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(inner);
    try mkdirAt(leaf);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{inner}), original_journal);
    try sealKept(tree);
    try sealKept(mid);
    try sealKept(inner);
    try sealKept(leaf);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);

    var pipes: [2]i32 = undefined;
    try std.testing.expect(linux.errno(linux.pipe2(&pipes, .{ .CLOEXEC = true })) == .SUCCESS);
    const pid_raw = linux.fork();
    try std.testing.expect(linux.errno(pid_raw) == .SUCCESS);
    if (pid_raw == 0) {
        _ = linux.close(pipes[0]);
        _ = linux.ptrace(linux.PTRACE.TRACEME, 0, 0, 0, 0);
        _ = linux.kill(linux.getpid(), linux.SIG.STOP);
        const op = finishRenamed(&held, moved, dst, &why);
        const text: []const u8 = if (op) |_| "OK" else |_| why;
        _ = linux.write(pipes[1], text.ptr, text.len);
        linux.exit(0);
    }
    const pid: linux.pid_t = @intCast(pid_raw);
    _ = linux.close(pipes[1]);
    var status: i32 = 0;
    _ = linux.waitpid(pid, &status, 0);
    _ = linux.ptrace(linux.PTRACE.SETOPTIONS, pid, 0, linux.PTRACE.O.TRACESYSGOOD, 0);
    _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    var entering = true;
    var stops: usize = 0;
    var phase: u8 = 0;
    var eintr_fd: u64 = 0;
    var mark: u8 = 0;
    while (stops < 80000) {
        stops += 1;
        const wr = linux.waitpid(pid, &status, 0);
        if (linux.errno(wr) != .SUCCESS) break;
        const st: u32 = @bitCast(status);
        if (linux.W.IFEXITED(st) or linux.W.IFSIGNALED(st)) break;
        if (!linux.W.IFSTOPPED(st)) break;
        const stop_sig = linux.W.EXITSTATUS(st);
        if (stop_sig != (@intFromEnum(linux.SIG.TRAP) | 0x80)) {
            _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
            continue;
        }
        var regs: TraceRegs = undefined;
        _ = linux.ptrace(linux.PTRACE.GETREGS, pid, 0, @intFromPtr(&regs), 0);
        const is_close = regs.orig_rax == @intFromEnum(linux.SYS.close);
        if (entering) {
            mark = 0;
            if (phase == 1 and !(is_close and regs.rdi == eintr_fd)) phase = 2;
            if (is_close) {
                var pbuf: [sys.path_max]u8 = undefined;
                if (fdPathOf(pid, regs.rdi, &pbuf)) |path| {
                    if (watchedDir(path, moved)) {
                        const dups = dupCount(pid, path);
                        if (phase == 1 and regs.rdi == eintr_fd) {
                            phase = 0;
                        } else if (phase == 0 and dups >= 2) {
                            mark = 1;
                            phase = 1;
                            eintr_fd = regs.rdi;
                        } else if (phase == 2 and dups >= 2) {
                            mark = 2;
                            phase = 3;
                        }
                    }
                }
            }
        } else if (mark != 0) {
            const errno: i64 = if (mark == 1) -4 else -5;
            regs.rax = @bitCast(errno);
            _ = linux.ptrace(linux.PTRACE.SETREGS, pid, 0, @intFromPtr(&regs), 0);
            mark = 0;
        }
        entering = !entering;
        _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    }
    if (!linux.W.IFEXITED(@bitCast(status))) {
        _ = linux.kill(pid, linux.SIG.KILL);
        _ = linux.waitpid(pid, &status, 0);
        std.debug.print("close trace did not finish, stops={d}\n", .{stops});
        return error.Unexpected;
    }
    var msg: [600]u8 = undefined;
    const nraw = linux.read(pipes[0], &msg, msg.len);
    try std.testing.expect(linux.errno(nraw) == .SUCCESS);
    const text = msg[0..nraw];
    if (std.mem.indexOf(u8, text, "Interrupted system call") == null) std.debug.print("close why: {s}\n", .{text});
    try std.testing.expect(std.mem.indexOf(u8, text, "Interrupted system call") != null);
    try std.testing.expect(std.mem.indexOf(u8, text, "Input/output error") == null);
}

const empty_seal = "ebsr-seal 1\nmode 365\n0 -\n1 -\n2 -\n";

fn modes755(paths: []const []const u8) void {
    for (paths) |path| modeAt(path, 0o755);
}

fn assertNoSealsUnder(a: Allocator, directory: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var it = try fsx.DirIter.init(a, fd);
    defer it.deinit(a);
    while (try it.next()) |item| {
        if (std.mem.indexOf(u8, item.name, "ebsr-seal") != null) {
            std.debug.print("seal remains: {s}/{s}\n", .{ directory, item.name });
            return error.TestExpectedEqual;
        }
        const is_dir = if (item.type == linux.DT.DIR)
            true
        else if (item.type != linux.DT.UNKNOWN)
            false
        else
            (fsx.statAt(fd, item.name, false) catch continue).isDir();
        if (!is_dir) continue;
        const child = try std.fs.path.join(a, &.{ directory, item.name });
        try assertNoSealsUnder(a, child);
    }
}

fn assertNoSeals(a: Allocator, directory: []const u8) !void {
    const parent = std.fs.path.dirname(directory) orelse ".";
    const beside = try std.fmt.allocPrint(a, "{s}/.{s}.ebsr-seal", .{ parent, std.fs.path.basename(directory) });
    if (fileAt(beside)) {
        std.debug.print("seal remains: {s}\n", .{beside});
        return error.TestExpectedEqual;
    }
    try assertNoSealsUnder(a, directory);
}

fn armNfs(a: Allocator, label: []const u8) !struct { previous: @TypeOf(atomic.ops.*), store: []const u8 } {
    var store_buf: [96]u8 = undefined;
    const store = try a.dupe(u8, try std.fmt.bufPrint(&store_buf, "/tmp/prefix-{s}-store-{d}", .{ label, linux.getpid() }));
    const previous = atomic.ops.*;
    nfs4(store);
    return .{ .previous = previous, .store = store };
}

test "a sealed parent and sealed child with no journal leave no seal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "bare-pair");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-bare-pair-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{root});
    defer {
        modes755(&.{ mid, root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(mid);
    modeAt(mid, 0o555);
    modeAt(root, 0o555);
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    try assertNoSeals(a, root);

    const src = try std.fmt.allocPrint(a, "/tmp/prefix-bare-ren-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-bare-ren-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const tree_mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    defer {
        modes755(&.{ tree_mid, tree, moved_mid, moved, src, dst });
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(tree_mid);
    modeAt(tree_mid, 0o555);
    modeAt(tree, 0o555);
    why = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    try assertNoSeals(a, moved);
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{src})));
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.tree.ebsr-seal", .{dst})));
}

test "a three-level chain with a journal only at the bottom leaves no seal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "bottom");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-bottom-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{root});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    const other = try std.fmt.allocPrint(a, "{s}/other", .{mid});
    defer {
        modes755(&.{ other, leaf, mid, root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    try mkdirAt(other);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{mid}), empty_seal);
    modeAt(leaf, 0o555);
    modeAt(other, 0o555);
    modeAt(mid, 0o555);
    modeAt(root, 0o555);
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    try assertNoSeals(a, root);
}

test "a renamed tree with one sealed child and one bare child leaves no mid journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "bare-sib");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const src = try std.fmt.allocPrint(a, "/tmp/prefix-bare-sib-src-{d}", .{linux.getpid()});
    const dst = try std.fmt.allocPrint(a, "/tmp/prefix-bare-sib-dst-{d}", .{linux.getpid()});
    const tree = try std.fmt.allocPrint(a, "{s}/tree", .{src});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{tree});
    const inner = try std.fmt.allocPrint(a, "{s}/inner", .{mid});
    const other = try std.fmt.allocPrint(a, "{s}/other", .{mid});
    const moved = try std.fmt.allocPrint(a, "{s}/tree", .{dst});
    const moved_mid = try std.fmt.allocPrint(a, "{s}/mid", .{moved});
    const moved_inner = try std.fmt.allocPrint(a, "{s}/inner", .{moved_mid});
    const moved_other = try std.fmt.allocPrint(a, "{s}/other", .{moved_mid});
    defer {
        modes755(&.{ other, inner, mid, tree, moved_other, moved_inner, moved_mid, moved, src, dst });
        fsx.deleteTree(a, fsx.cwd, src) catch {};
        fsx.deleteTree(a, fsx.cwd, dst) catch {};
    }
    try mkdirAt(src);
    try mkdirAt(dst);
    try mkdirAt(tree);
    try mkdirAt(mid);
    try mkdirAt(inner);
    try mkdirAt(other);
    try putBytes(try std.fmt.allocPrint(a, "{s}/.inner.ebsr-seal", .{mid}), empty_seal);
    modeAt(inner, 0o555);
    modeAt(other, 0o555);
    modeAt(mid, 0o555);
    modeAt(tree, 0o555);
    var why: []const u8 = "";
    var held = (try holdDirectory(a, tree, &why)).?;
    try sys.renameat(fsx.cwd, tree, fsx.cwd, moved);
    try finishRenamed(&held, moved, dst, &why);
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{moved})));
    try assertNoSeals(a, moved);
}

test "a sealed tree with no journal leaves none behind" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "nosj");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-nosj-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{root});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{mid});
    defer {
        modes755(&.{ leaf, mid, root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(mid);
    try mkdirAt(leaf);
    modeAt(leaf, 0o555);
    modeAt(mid, 0o555);
    modeAt(root, 0o555);
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.root.ebsr-seal", .{base})));
    try std.testing.expect(!fileAt(try std.fmt.allocPrint(a, "{s}/.mid.ebsr-seal", .{root})));
    try assertNoSeals(a, root);
}

test "a mode set after a release is not restored by the next finish" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "modekeep");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-modekeep-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const mid = try std.fmt.allocPrint(a, "{s}/mid", .{root});
    defer {
        modes755(&.{ mid, root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(mid);
    modeAt(mid, 0o555);
    modeAt(root, 0o555);
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    modeAt(root, 0o755);
    try finishSealed(a, root, &why);
    try std.testing.expectEqual(@as(u32, 0o755), (try fsx.statAt(fsx.cwd, root, false)).perm());
}

test "a close error on a sealed directory keeps its journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "close-eio");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-close-eio-{d}", .{linux.getpid()});
    const dir = try std.fmt.allocPrint(a, "{s}/dir", .{base});
    const seal = try std.fmt.allocPrint(a, "{s}/.dir.ebsr-seal", .{base});
    defer {
        modes755(&.{ dir, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(dir);
    try putBytes(seal, empty_seal);
    modeAt(dir, 0o555);
    var why: []const u8 = "";
    var pipes: [2]i32 = undefined;
    try std.testing.expect(linux.errno(linux.pipe2(&pipes, .{ .CLOEXEC = true })) == .SUCCESS);
    const pid_raw = linux.fork();
    try std.testing.expect(linux.errno(pid_raw) == .SUCCESS);
    if (pid_raw == 0) {
        _ = linux.close(pipes[0]);
        _ = linux.ptrace(linux.PTRACE.TRACEME, 0, 0, 0, 0);
        _ = linux.kill(linux.getpid(), linux.SIG.STOP);
        const op = finishSealed(a, dir, &why);
        const text: []const u8 = if (op) |_| "OK" else |_| why;
        _ = linux.write(pipes[1], text.ptr, text.len);
        linux.exit(0);
    }
    const pid: linux.pid_t = @intCast(pid_raw);
    _ = linux.close(pipes[1]);
    var status: i32 = 0;
    _ = linux.waitpid(pid, &status, 0);
    _ = linux.ptrace(linux.PTRACE.SETOPTIONS, pid, 0, linux.PTRACE.O.TRACESYSGOOD, 0);
    _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    var entering = true;
    var stops: usize = 0;
    var mark: u8 = 0;
    const token = try std.fmt.allocPrint(a, "prefix-close-eio-{d}/dir", .{linux.getpid()});
    while (stops < 80000) {
        stops += 1;
        const wr = linux.waitpid(pid, &status, 0);
        if (linux.errno(wr) != .SUCCESS) break;
        const st: u32 = @bitCast(status);
        if (linux.W.IFEXITED(st) or linux.W.IFSIGNALED(st)) break;
        if (!linux.W.IFSTOPPED(st)) break;
        const stop_sig = linux.W.EXITSTATUS(st);
        if (stop_sig != (@intFromEnum(linux.SIG.TRAP) | 0x80)) {
            _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
            continue;
        }
        var regs: TraceRegs = undefined;
        _ = linux.ptrace(linux.PTRACE.GETREGS, pid, 0, @intFromPtr(&regs), 0);
        const is_close = regs.orig_rax == @intFromEnum(linux.SYS.close);
        if (entering) {
            mark = 0;
            if (is_close) {
                var pbuf: [sys.path_max]u8 = undefined;
                if (fdPathOf(pid, regs.rdi, &pbuf)) |path| {
                    if (std.mem.endsWith(u8, path, token) and dupCount(pid, path) == 1) mark = 1;
                }
            }
        } else if (mark != 0) {
            regs.rax = @bitCast(@as(i64, -5));
            _ = linux.ptrace(linux.PTRACE.SETREGS, pid, 0, @intFromPtr(&regs), 0);
            mark = 0;
        }
        entering = !entering;
        _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    }
    if (!linux.W.IFEXITED(@bitCast(status))) {
        _ = linux.kill(pid, linux.SIG.KILL);
        _ = linux.waitpid(pid, &status, 0);
        std.debug.print("close injection did not finish, stops={d}\n", .{stops});
        return error.Unexpected;
    }
    var msg: [600]u8 = undefined;
    const nraw = linux.read(pipes[0], &msg, msg.len);
    try std.testing.expect(linux.errno(nraw) == .SUCCESS);
    const text = msg[0..nraw];
    if (std.mem.indexOf(u8, text, "Input/output error") == null) std.debug.print("close eio why: {s}\n", .{text});
    try std.testing.expect(std.mem.indexOf(u8, text, "Input/output error") != null);
    try std.testing.expect(fileAt(seal));
}

/// Fail the flush of one directory once `seal` is no longer a file.
const SealGoneFsync = struct {
    var saved: *const fn (fsx.fd_t) usize = undefined;
    var dev: u64 = 0;
    var ino: u64 = 0;
    var seal: []const u8 = "";

    fn arm(st: fsx.Stat, seal_path: []const u8) void {
        saved = atomic.ops.fsync;
        atomic.ops.fsync = onFsync;
        dev = st.dev;
        ino = st.ino;
        seal = seal_path;
    }

    fn eio() usize {
        return @as(usize, @bitCast(@as(isize, -@as(isize, @backingInt(linux.E.IO)))));
    }

    fn onFsync(fd: fsx.fd_t) usize {
        if (fsx.fstat(fd)) |st| {
            if (st.dev == dev and st.ino == ino and !fileAt(seal)) return eio();
        } else |_| {}
        return saved(fd);
    }
};

test "a close error on a re-chmodded parent keeps its journal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "reclose");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-reclose-{d}", .{linux.getpid()});
    const dir = try std.fmt.allocPrint(a, "{s}/dir", .{base});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{dir});
    const seal = try std.fmt.allocPrint(a, "{s}/.dir.ebsr-seal", .{base});
    const leaf_seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{dir});
    defer {
        modes755(&.{ leaf, dir, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(dir);
    try mkdirAt(leaf);
    try putBytes(seal, empty_seal);
    try putBytes(leaf_seal, empty_seal);
    modeAt(leaf, 0o555);
    modeAt(dir, 0o555);
    var why: []const u8 = "";
    var pipes: [2]i32 = undefined;
    try std.testing.expect(linux.errno(linux.pipe2(&pipes, .{ .CLOEXEC = true })) == .SUCCESS);
    const pid_raw = linux.fork();
    try std.testing.expect(linux.errno(pid_raw) == .SUCCESS);
    if (pid_raw == 0) {
        _ = linux.close(pipes[0]);
        _ = linux.ptrace(linux.PTRACE.TRACEME, 0, 0, 0, 0);
        _ = linux.kill(linux.getpid(), linux.SIG.STOP);
        const op = finishSealed(a, dir, &why);
        const text: []const u8 = if (op) |_| "OK" else |_| why;
        _ = linux.write(pipes[1], text.ptr, text.len);
        linux.exit(0);
    }
    const pid: linux.pid_t = @intCast(pid_raw);
    _ = linux.close(pipes[1]);
    var status: i32 = 0;
    _ = linux.waitpid(pid, &status, 0);
    _ = linux.ptrace(linux.PTRACE.SETOPTIONS, pid, 0, linux.PTRACE.O.TRACESYSGOOD, 0);
    _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    var entering = true;
    var stops: usize = 0;
    var mark: u8 = 0;
    const token = try std.fmt.allocPrint(a, "prefix-reclose-{d}/dir", .{linux.getpid()});
    while (stops < 80000) {
        stops += 1;
        const wr = linux.waitpid(pid, &status, 0);
        if (linux.errno(wr) != .SUCCESS) break;
        const st: u32 = @bitCast(status);
        if (linux.W.IFEXITED(st) or linux.W.IFSIGNALED(st)) break;
        if (!linux.W.IFSTOPPED(st)) break;
        const stop_sig = linux.W.EXITSTATUS(st);
        if (stop_sig != (@intFromEnum(linux.SIG.TRAP) | 0x80)) {
            _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
            continue;
        }
        var regs: TraceRegs = undefined;
        _ = linux.ptrace(linux.PTRACE.GETREGS, pid, 0, @intFromPtr(&regs), 0);
        const is_close = regs.orig_rax == @intFromEnum(linux.SYS.close);
        if (entering) {
            mark = 0;
            if (is_close) {
                var pbuf: [sys.path_max]u8 = undefined;
                if (fdPathOf(pid, regs.rdi, &pbuf)) |path| {
                    if (std.mem.endsWith(u8, path, token) and dupCount(pid, path) == 1 and !fileAt(leaf_seal)) mark = 1;
                }
            }
        } else if (mark != 0) {
            regs.rax = @bitCast(@as(i64, -5));
            _ = linux.ptrace(linux.PTRACE.SETREGS, pid, 0, @intFromPtr(&regs), 0);
            mark = 0;
        }
        entering = !entering;
        _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    }
    if (!linux.W.IFEXITED(@bitCast(status))) {
        _ = linux.kill(pid, linux.SIG.KILL);
        _ = linux.waitpid(pid, &status, 0);
        std.debug.print("reclose injection did not finish, stops={d}\n", .{stops});
        return error.Unexpected;
    }
    var msg: [600]u8 = undefined;
    const nraw = linux.read(pipes[0], &msg, msg.len);
    try std.testing.expect(linux.errno(nraw) == .SUCCESS);
    const text = msg[0..nraw];
    if (std.mem.indexOf(u8, text, "Input/output error") == null) std.debug.print("reclose why: {s}\n", .{text});
    try std.testing.expect(std.mem.indexOf(u8, text, "Input/output error") != null);
    try std.testing.expect(fileAt(seal));
}

test "a failed flush of the outside directory after the root unlink is returned" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "outsync");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-outsync-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const seal = try std.fmt.allocPrint(a, "{s}/.root.ebsr-seal", .{base});
    defer {
        modes755(&.{ root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try putBytes(seal, empty_seal);
    modeAt(root, 0o555);
    const outside = try fsx.statAt(fsx.cwd, base, false);
    SealGoneFsync.arm(outside, seal);
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, finishSealed(a, root, &why));
    if (std.mem.indexOf(u8, why, "Input/output error") == null) std.debug.print("outside why: {s}\n", .{why});
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
}

test "a failed flush of a writable parent after a child unlink is returned" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "wparent");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const base = try std.fmt.allocPrint(a, "/tmp/prefix-wparent-{d}", .{linux.getpid()});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{root});
    const leaf_seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{root});
    defer {
        modes755(&.{ leaf, root, base });
        fsx.deleteTree(a, fsx.cwd, base) catch {};
    }
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(leaf);
    try putBytes(leaf_seal, empty_seal);
    modeAt(leaf, 0o555);
    const parent = try fsx.statAt(fsx.cwd, root, false);
    try std.testing.expect(parent.perm() & 0o200 != 0);
    SealGoneFsync.arm(parent, leaf_seal);
    var why: []const u8 = "";
    try std.testing.expectError(error.Refused, finishSealed(a, root, &why));
    if (std.mem.indexOf(u8, why, "Input/output error") == null) std.debug.print("wparent why: {s}\n", .{why});
    try std.testing.expect(std.mem.indexOf(u8, why, "Input/output error") != null);
}

test "a journal-only descendant under a sealed outside is released" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "outleaf");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const grand = try std.fmt.allocPrint(a, "/tmp/prefix-outleaf-{d}", .{linux.getpid()});
    const base = try std.fmt.allocPrint(a, "{s}/base", .{grand});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{root});
    const leaf_seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{root});
    const outside_seal = try std.fmt.allocPrint(a, "{s}/.base.ebsr-seal", .{grand});
    defer {
        modes755(&.{ leaf, root, base, grand });
        fsx.deleteTree(a, fsx.cwd, grand) catch {};
    }
    try mkdirAt(grand);
    try mkdirAt(base);
    try mkdirAt(root);
    try mkdirAt(leaf);
    try putBytes(leaf_seal, empty_seal);
    modeAt(leaf, 0o555);
    modeAt(root, 0o555);
    modeAt(base, 0o555);
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    try std.testing.expect(!fileAt(leaf_seal));
    try std.testing.expect(!fileAt(outside_seal));
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, base, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, root, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, leaf, false)).perm());
}

test "a planted root journal under a sealed outside is removed and a failed close retries" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "outseal");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const grand = try std.fmt.allocPrint(a, "/tmp/prefix-outseal-{d}", .{linux.getpid()});
    const base = try std.fmt.allocPrint(a, "{s}/base", .{grand});
    const root = try std.fmt.allocPrint(a, "{s}/root", .{base});
    const seal = try std.fmt.allocPrint(a, "{s}/.root.ebsr-seal", .{base});
    const outside_seal = try std.fmt.allocPrint(a, "{s}/.base.ebsr-seal", .{grand});
    defer {
        modes755(&.{ root, base, grand });
        fsx.deleteTree(a, fsx.cwd, grand) catch {};
    }
    try mkdirAt(grand);
    try mkdirAt(base);
    try mkdirAt(root);
    try putBytes(seal, empty_seal);
    modeAt(root, 0o555);
    modeAt(base, 0o555);
    var pipes: [2]i32 = undefined;
    try std.testing.expect(linux.errno(linux.pipe2(&pipes, .{ .CLOEXEC = true })) == .SUCCESS);
    const pid_raw = linux.fork();
    try std.testing.expect(linux.errno(pid_raw) == .SUCCESS);
    if (pid_raw == 0) {
        _ = linux.close(pipes[0]);
        _ = linux.ptrace(linux.PTRACE.TRACEME, 0, 0, 0, 0);
        _ = linux.kill(linux.getpid(), linux.SIG.STOP);
        var why: []const u8 = "";
        const op = finishSealed(a, root, &why);
        const text: []const u8 = if (op) |_| "OK" else |_| why;
        _ = linux.write(pipes[1], text.ptr, text.len);
        linux.exit(0);
    }
    const pid: linux.pid_t = @intCast(pid_raw);
    _ = linux.close(pipes[1]);
    var status: i32 = 0;
    _ = linux.waitpid(pid, &status, 0);
    _ = linux.ptrace(linux.PTRACE.SETOPTIONS, pid, 0, linux.PTRACE.O.TRACESYSGOOD, 0);
    _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    var entering = true;
    var stops: usize = 0;
    var mark: u8 = 0;
    var injected = false;
    const token = try std.fmt.allocPrint(a, "prefix-outseal-{d}/base", .{linux.getpid()});
    while (stops < 80000) {
        stops += 1;
        const wr = linux.waitpid(pid, &status, 0);
        if (linux.errno(wr) != .SUCCESS) break;
        const st: u32 = @bitCast(status);
        if (linux.W.IFEXITED(st) or linux.W.IFSIGNALED(st)) break;
        if (!linux.W.IFSTOPPED(st)) break;
        const stop_sig = linux.W.EXITSTATUS(st);
        if (stop_sig != (@intFromEnum(linux.SIG.TRAP) | 0x80)) {
            _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
            continue;
        }
        var regs: TraceRegs = undefined;
        _ = linux.ptrace(linux.PTRACE.GETREGS, pid, 0, @intFromPtr(&regs), 0);
        const is_close = regs.orig_rax == @intFromEnum(linux.SYS.close);
        if (entering) {
            mark = 0;
            if (!injected and is_close) {
                var pbuf: [sys.path_max]u8 = undefined;
                if (fdPathOf(pid, regs.rdi, &pbuf)) |path| {
                    if (std.mem.endsWith(u8, path, token) and dupCount(pid, path) == 1 and !fileAt(seal) and fileAt(outside_seal)) mark = 1;
                }
            }
        } else if (mark != 0) {
            regs.rax = @bitCast(@as(i64, -5));
            _ = linux.ptrace(linux.PTRACE.SETREGS, pid, 0, @intFromPtr(&regs), 0);
            injected = true;
            mark = 0;
        }
        entering = !entering;
        _ = linux.ptrace(linux.PTRACE.SYSCALL, pid, 0, 0, 0);
    }
    if (!linux.W.IFEXITED(@bitCast(status))) {
        _ = linux.kill(pid, linux.SIG.KILL);
        _ = linux.waitpid(pid, &status, 0);
        std.debug.print("outside close injection did not finish, stops={d}\n", .{stops});
        return error.Unexpected;
    }
    var msg: [600]u8 = undefined;
    const nraw = linux.read(pipes[0], &msg, msg.len);
    try std.testing.expect(linux.errno(nraw) == .SUCCESS);
    const text = msg[0..nraw];
    if (std.mem.indexOf(u8, text, "Input/output error") == null) std.debug.print("outside close why: {s}\n", .{text});
    try std.testing.expect(std.mem.indexOf(u8, text, "Input/output error") != null);
    try std.testing.expect(fileAt(outside_seal));
    try std.testing.expect(!fileAt(seal));
    var why: []const u8 = "";
    try finishSealed(a, root, &why);
    try std.testing.expect(!fileAt(seal));
    try std.testing.expect(!fileAt(outside_seal));
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, base, false)).perm());
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, root, false)).perm());
}

test "a wide unsealed tree finishes under a 1024 descriptor limit" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const nfs = try armNfs(a, "wide");
    defer {
        atomic.ops.* = nfs.previous;
        unlinkAt(nfs.store);
    }
    const root = try std.fmt.allocPrint(a, "/tmp/prefix-wide-{d}", .{linux.getpid()});
    const held = try std.fmt.allocPrint(a, "{s}/d0000", .{root});
    const leaf = try std.fmt.allocPrint(a, "{s}/leaf", .{held});
    const leaf_seal = try std.fmt.allocPrint(a, "{s}/.leaf.ebsr-seal", .{held});
    defer {
        modes755(&.{ leaf, held, root });
        fsx.deleteTree(a, fsx.cwd, root) catch {};
    }
    try mkdirAt(root);
    var n: usize = 0;
    while (n < 1600) : (n += 1) {
        var buf: [96]u8 = undefined;
        const dir = std.fmt.bufPrint(&buf, "{s}/d{d:0>4}", .{ root, n }) catch unreachable;
        try mkdirAt(dir);
    }
    try mkdirAt(leaf);
    try putBytes(leaf_seal, empty_seal);
    modeAt(leaf, 0o555);
    var old: linux.rlimit = undefined;
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.prlimit(0, .NOFILE, null, &old)));
    const lim: linux.rlimit = .{ .cur = 1024, .max = old.max };
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.prlimit(0, .NOFILE, &lim, null)));
    defer _ = linux.prlimit(0, .NOFILE, &old, null);
    var why: []const u8 = "";
    rewriteTree(a, root, "a", "a", &why) catch |err| {
        std.debug.print("wide why: {s} ({s})\n", .{ @errorName(err), why });
        return err;
    };
    try std.testing.expect(!fileAt(leaf_seal));
    try std.testing.expectEqual(@as(u32, 0o555), (try fsx.statAt(fsx.cwd, leaf, false)).perm());
}

test "a forty-deep unsealed tree is not refused" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const root = try std.fmt.allocPrint(a, "/tmp/prefix-deep40-{d}", .{linux.getpid()});
    defer fsx.deleteTree(a, fsx.cwd, root) catch {};
    try mkdirAt(root);
    var dir = root;
    var depth: usize = 1;
    while (depth < 40) : (depth += 1) {
        const next = try std.fmt.allocPrint(a, "{s}/d", .{dir});
        try mkdirAt(next);
        dir = next;
    }
    var why: []const u8 = "";
    rewriteTree(a, root, "a", "a", &why) catch |err| {
        std.debug.print("deep why: {s} ({s})\n", .{ @errorName(err), why });
        return err;
    };
}
