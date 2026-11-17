//! What on this node still uses a tree that gc is about to remove, found in
//! /proc in the order inuse.py finds it: the mounts of this mount
//! namespace, then each process by pid, with the mounts of its namespace
//! when no earlier process shared them, its executable, working directory,
//! root directory, open files and mapped files. Only links and text are
//! read, so the files found are never touched.
const std = @import("std");
const common = @import("zig_common");
const oom = @import("oom.zig").oom;

const linux = std.os.linux;
const sys = common.sys;
const fsx = common.fsx;
const Allocator = std.mem.Allocator;

/// One line of a mountinfo file: the device, the directory of that device
/// that is mounted, and the mount point, unescaped.
pub const Mount = struct { device: []const u8, root: []const u8, point: []const u8 };

/// The device of the mount that holds a tree, and the tree's path on that
/// device.
pub const Source = struct { device: []const u8, path: []const u8 };

const links = [_]struct { name: []const u8, what: []const u8 }{
    .{ .name = "exe", .what = "runs a program in it" },
    .{ .name = "cwd", .what = "has its working directory in it" },
    .{ .name = "root", .what = "has its root directory in it" },
};

fn octal(b: u8) bool {
    return b >= '0' and b <= '7';
}

fn unescape(a: Allocator, field: []const u8) []const u8 {
    if (std.mem.findScalar(u8, field, '\\') == null) return field;
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < field.len) {
        if (field[i] == '\\' and i + 3 < field.len and octal(field[i + 1]) and octal(field[i + 2]) and octal(field[i + 3])) {
            const value = (@as(u16, field[i + 1] - '0') << 6) | (@as(u16, field[i + 2] - '0') << 3) | (field[i + 3] - '0');
            out.append(a, @truncate(value)) catch oom();
            i += 4;
        } else {
            out.append(a, field[i]) catch oom();
            i += 1;
        }
    }
    return out.items;
}

/// The device, root and mount point of each line of a mountinfo file.
pub fn mounts(a: Allocator, table: []const u8) []const Mount {
    var found: std.ArrayList(Mount) = .empty;
    var lines = std.mem.splitScalar(u8, table, '\n');
    while (lines.next()) |line| {
        var fields: [5][]const u8 = undefined;
        var n: usize = 0;
        var it = std.mem.splitScalar(u8, line, ' ');
        while (n < fields.len) : (n += 1) fields[n] = it.next() orelse break;
        if (n < fields.len) continue;
        found.append(a, .{ .device = fields[2], .root = unescape(a, fields[3]), .point = unescape(a, fields[4]) }) catch oom();
    }
    return found.items;
}

fn within(path: []const u8, top: []const u8) bool {
    if (std.mem.eql(u8, path, top)) return true;
    const t = std.mem.trimEnd(u8, top, "/");
    return path.len > t.len and std.mem.startsWith(u8, path, t) and path[t.len] == '/';
}

/// The device of the mount that holds `tree`, and the tree's path on that
/// device.
pub fn source(a: Allocator, tree: []const u8, table: []const Mount) ?Source {
    var held: ?Mount = null;
    for (table) |m| {
        if (within(tree, m.point) and (held == null or m.point.len >= held.?.point.len)) held = m;
    }
    const h = held orelse return null;
    const rest = tree[std.mem.trimEnd(u8, h.point, "/").len..];
    const path = std.mem.concat(a, u8, &.{ std.mem.trimEnd(u8, h.root, "/"), rest }) catch oom();
    return .{ .device = h.device, .path = if (path.len == 0) "/" else path };
}

fn readFile(a: Allocator, path: []const u8) ?[]const u8 {
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0) catch return null;
    defer sys.close(fd);
    return fsx.readAll(a, fd, 1 << 30) catch null;
}

fn readlink(a: Allocator, path: []const u8) ?[]const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = sys.z(&zb, path) catch return null;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch return null;
    return a.dupe(u8, buf[0..n]) catch oom();
}

/// The numeric names in `directory`, ascending.
fn numbers(a: Allocator, directory: []const u8) []const u64 {
    const fd = fsx.openDir(fsx.cwd, directory, true) catch return &.{};
    defer sys.close(fd);
    var found: std.ArrayList(u64) = .empty;
    var it = fsx.DirIter.init(a, fd) catch oom();
    defer it.deinit(a);
    while (it.next() catch return &.{}) |e| {
        const digits = for (e.name) |ch| {
            if (!std.ascii.isDigit(ch)) break false;
        } else e.name.len > 0;
        if (!digits) continue;
        found.append(a, std.fmt.parseInt(u64, e.name, 10) catch continue) catch oom();
    }
    std.mem.sort(u64, found.items, {}, std.sort.asc(u64));
    return found.items;
}

/// The pathname of a /proc/PID/maps line: what follows its first five
/// fields, or null when nothing does.
fn mapped(line: []const u8) ?[]const u8 {
    var i: usize = 0;
    for (0..5) |_| {
        while (i < line.len and std.ascii.isWhitespace(line[i])) i += 1;
        if (i == line.len) return null;
        while (i < line.len and !std.ascii.isWhitespace(line[i])) i += 1;
    }
    while (i < line.len and std.ascii.isWhitespace(line[i])) i += 1;
    return if (i == line.len) null else line[i..];
}

const Scan = struct {
    a: Allocator,
    found: []?[]const u8,
    real: []const []const u8,
    held: []const ?Source,

    fn note(s: *Scan, i: usize, comptime f: []const u8, args: anytype) void {
        if (s.found[i] == null) s.found[i] = s.a.print(f, args) catch oom();
    }

    fn bound(s: *const Scan, i: usize, m: Mount) bool {
        const h = s.held[i] orelse return false;
        return std.mem.eql(u8, m.device, h.device) and within(m.root, h.path);
    }

    fn done(s: *const Scan) bool {
        for (s.found) |f| if (f == null) return false;
        return true;
    }

    fn uses(s: *Scan, pid: u64, path: ?[]const u8, what: []const u8) void {
        const p = path orelse return;
        for (s.real, 0..) |r, i| if (within(p, r)) s.note(i, "pid {d} {s}", .{ pid, what });
    }
};

/// For each tree, the first use of it this node shows, or null. `a` holds
/// the answers; each process is read into an arena on `scratch`, reset
/// before the next.
pub fn users(a: Allocator, scratch: Allocator, trees: []const []const u8) []?[]const u8 {
    const found = a.alloc(?[]const u8, trees.len) catch oom();
    @memset(found, null);
    if (trees.len == 0) return found;
    const real = a.alloc([]const u8, trees.len) catch oom();
    for (trees, real) |t, *r| {
        var rb: [sys.path_max]u8 = undefined;
        r.* = if (fsx.realpath(fsx.cwd, t, &rb)) |p| a.dupe(u8, p) catch oom() else |_| t;
    }
    const own = readFile(a, "/proc/self/mountinfo") orelse "";
    const table = mounts(a, own);
    const held = a.alloc(?Source, trees.len) catch oom();
    for (real, held) |r, *h| h.* = source(a, r, table);
    var scan: Scan = .{ .a = a, .found = found, .real = real, .held = held };
    for (table) |m| for (real, 0..) |r, i| {
        if (within(m.point, r)) {
            scan.note(i, "{s} is a mount point", .{m.point});
        } else if (scan.bound(i, m)) {
            scan.note(i, "mounted at {s}", .{m.point});
        }
    };
    var seen: std.StringHashMapUnmanaged(void) = .empty;
    seen.put(a, own, {}) catch oom();
    var arena = std.heap.ArenaAllocator.init(scratch);
    defer arena.deinit();
    for (numbers(a, "/proc")) |pid| {
        if (scan.done()) break;
        _ = arena.reset(.retain_capacity);
        const t = arena.allocator();
        const base = t.print("/proc/{d}/", .{pid}) catch oom();
        if (readFile(t, std.mem.concat(t, u8, &.{ base, "mountinfo" }) catch oom())) |other| {
            if (!seen.contains(other)) {
                seen.put(a, a.dupe(u8, other) catch oom(), {}) catch oom();
                for (mounts(t, other)) |m| for (0..trees.len) |i| {
                    if (scan.bound(i, m)) scan.note(i, "mounted at {s} in the mount namespace of pid {d}", .{ m.point, pid });
                };
            }
        }
        for (links) |l| scan.uses(pid, readlink(t, std.mem.concat(t, u8, &.{ base, l.name }) catch oom()), l.what);
        const fds = std.mem.concat(t, u8, &.{ base, "fd" }) catch oom();
        for (numbers(t, fds)) |fd| {
            scan.uses(pid, readlink(t, t.print("{s}/{d}", .{ fds, fd }) catch oom()), "has a file open in it");
        }
        var lines = std.mem.splitScalar(u8, readFile(t, std.mem.concat(t, u8, &.{ base, "maps" }) catch oom()) orelse "", '\n');
        while (lines.next()) |line| scan.uses(pid, mapped(line), "maps a file in it");
    }
    return found;
}

test "mountinfo fields are unescaped and a tree's path on its device follows the mount that holds it" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const table = mounts(a,
        \\22 1 8:1 / / rw - ext4 /dev/sda1 rw
        \\40 22 0:52 /data /apps rw - nfs4 server:/export rw
        \\41 22 0:52 /data/software/my\040tree /mnt/a\134b rw - nfs4 server:/export rw
        \\short line
        \\
    );
    try std.testing.expectEqual(@as(usize, 3), table.len);
    try std.testing.expectEqualStrings("/data/software/my tree", table[2].root);
    try std.testing.expectEqualStrings("/mnt/a\\b", table[2].point);
    const held = source(a, "/apps/software/my tree", table).?;
    try std.testing.expectEqualStrings("0:52", held.device);
    try std.testing.expectEqualStrings("/data/software/my tree", held.path);
    try std.testing.expectEqualStrings("/data", source(a, "/apps", table).?.path);
    const outside = source(a, "/appsx", table).?;
    try std.testing.expectEqualStrings("8:1", outside.device);
    try std.testing.expectEqualStrings("/appsx", outside.path);
    try std.testing.expectEqualStrings("/etc", source(a, "/etc", table).?.path);
    try std.testing.expect(source(a, "/etc", &.{}) == null);
    try std.testing.expect(within("/apps/x", "/apps") and within("/apps", "/apps") and !within("/appsx", "/apps") and within("/a", "/"));
}

test "a maps line names the file after its first five fields" {
    try std.testing.expectEqualStrings("/lib/libz.so.1 (deleted)", mapped("7f00-7f01 r--p 00000000 08:01 1234    /lib/libz.so.1 (deleted)").?);
    try std.testing.expectEqualStrings("[heap]", mapped("5600-5601 rw-p 00000000 00:00 0  [heap]").?);
    try std.testing.expect(mapped("7f00-7f01 r--p 00000000 00:00 0   ") == null);
    try std.testing.expect(mapped("") == null);
}
