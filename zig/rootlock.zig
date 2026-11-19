//! The state-root lock, with the checks the kernel call does not make.
//! A symlink component is refused before the directory is created through
//! it. `.lock` is refused unless it is a regular file with one hard link,
//! so the pid write cannot truncate some other inode. Both lock files are
//! then owner-only: another user who can traverse the directory must not
//! be able to open them and take the flock.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");

const sys = common.sys;
const fsx = common.fsx;
const state = common.state;
const Allocator = std.mem.Allocator;

/// The lock's own errors, Unsafe for a symbolic link in the state root's
/// path, and Gate for a `.lock` that is not a regular file with one hard
/// link.
pub const Error = state.RootLock.AcquireError || error{ Unsafe, Gate };

fn isLink(path: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, path, false) catch return false;
    return st.isLink();
}

fn offendingComponent(path: []const u8) ?[]const u8 {
    var current = path;
    while (current.len > 0) {
        if (isLink(current)) return current;
        if (std.mem.eql(u8, current, "/")) break;
        const parent = std.fs.path.dirname(current) orelse break;
        if (parent.len == current.len) break;
        current = parent;
    }
    return null;
}

fn privateFile(a: Allocator, root: []const u8, name: []const u8) !bool {
    const path = try std.fs.path.join(a, &.{ root, name });
    defer a.free(path);
    const st = fsx.statAt(fsx.cwd, path, false) catch return true;
    return st.isReg() and st.nlink == 1;
}

fn tighten(fd: fsx.fd_t) bool {
    return linux.errno(linux.fchmod(fd, 0o600)) == .SUCCESS;
}

/// Lock `root` the way `FileLock.acquire` does.
/// `detail` receives the symlink component when the error is `Unsafe`.
pub fn acquire(a: Allocator, root: []const u8, timeout_ms: ?u32, detail: *[]const u8) Error!state.RootLock {
    if (offendingComponent(root)) |component| {
        detail.* = component;
        return error.Unsafe;
    }
    if (fsx.statAt(fsx.cwd, root, false)) |st| {
        // Keep regular-file roots on the existing Not a directory path and
        // reject other non-directory inode types early as "exists".
        if (!st.isDir() and !st.isReg()) return error.Exists;
    } else |err| switch (err) {
        error.NotFound => {},
        else => return err,
    }
    if (!(privateFile(a, root, ".lock") catch return error.OutOfMemory)) return error.Gate;
    if (!(privateFile(a, root, ".lock.posix") catch return error.OutOfMemory)) return error.NotPrivate;
    const lock = try state.RootLock.acquire(a, root, timeout_ms);
    if (!tighten(lock.gate) or !tighten(lock.posix)) {
        lock.release();
        return error.Unexpected;
    }
    return lock;
}

test "a hard-linked gate is not truncated and a symlink root is refused" {
    const a = std.testing.allocator;
    var pb: [64]u8 = undefined;
    const root = try std.fmt.bufPrint(&pb, "/tmp/rootlock-{d}", .{linux.getpid()});
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, root)).ptr, 0o700);
    defer fsx.deleteTree(a, fsx.cwd, root) catch {};
    const payload = try std.fmt.allocPrint(a, "{s}/payload", .{root});
    defer a.free(payload);
    {
        const fd = try sys.openat(fsx.cwd, payload, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, 0o600);
        try sys.writeAll(fd, "secret");
        sys.close(fd);
    }
    const gate = try std.fmt.allocPrint(a, "{s}/.lock", .{root});
    defer a.free(gate);
    {
        var zf: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        try std.testing.expect(linux.errno(linux.linkat(linux.AT.FDCWD, (try sys.z(&zf, payload)).ptr, linux.AT.FDCWD, (try sys.z(&zt, gate)).ptr, 0)) == .SUCCESS);
    }
    var detail: []const u8 = "";
    try std.testing.expectError(error.Gate, acquire(a, root, 0, &detail));
    const fd = try sys.openat(fsx.cwd, payload, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var buf: [16]u8 = undefined;
    const n = linux.read(fd, &buf, buf.len);
    try std.testing.expect(linux.errno(n) == .SUCCESS);
    try std.testing.expectEqualStrings("secret", buf[0..n]);

    const real = try std.fmt.allocPrint(a, "{s}/real", .{root});
    defer a.free(real);
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, real)).ptr, 0o700);
    const link = try std.fmt.allocPrint(a, "{s}/link", .{root});
    defer a.free(link);
    var zreal: [sys.path_max:0]u8 = undefined;
    var zlink: [sys.path_max:0]u8 = undefined;
    try std.testing.expect(linux.errno(linux.symlinkat((try sys.z(&zreal, real)).ptr, linux.AT.FDCWD, (try sys.z(&zlink, link)).ptr)) == .SUCCESS);
    try std.testing.expectError(error.Unsafe, acquire(a, link, 0, &detail));
    try std.testing.expectEqualStrings(link, detail);
    const created = try std.fmt.allocPrint(a, "{s}/.lock", .{real});
    defer a.free(created);
    try std.testing.expect(fsx.statAt(fsx.cwd, created, false) == error.NotFound);

    const fresh = try std.fmt.allocPrint(a, "{s}/fresh", .{root});
    defer a.free(fresh);
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, fresh)).ptr, 0o755);
    const held = try acquire(a, fresh, 0, &detail);
    const gate_path = try std.fmt.allocPrint(a, "{s}/.lock", .{fresh});
    defer a.free(gate_path);
    const st = try fsx.statAt(fsx.cwd, gate_path, false);
    try std.testing.expectEqual(@as(u32, 0o600), st.perm());
    held.release();

    const fifo = try std.fmt.allocPrint(a, "{s}/fifo", .{root});
    defer a.free(fifo);
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, fifo)).ptr, 0o755);
    const pipe = try std.fmt.allocPrint(a, "{s}/.lock", .{fifo});
    defer a.free(pipe);
    try std.testing.expect(linux.errno(linux.mknodat(linux.AT.FDCWD, (try sys.z(&zb, pipe)).ptr, linux.S.IFIFO | 0o644, 0)) == .SUCCESS);
    try std.testing.expectError(error.Gate, acquire(a, fifo, 0, &detail));
}
