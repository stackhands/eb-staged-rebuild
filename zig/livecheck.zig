//! A before/after record of a live install directory: every entry's
//! relative path, kind, inode, size, and a symlink's target or a file's
//! sha256. The record is written to the journal in the Python tool's shape,
//! so a run either implementation started can be checked by the other.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const pyfmt = @import("pyfmt.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// st_dev as glibc's makedev() encodes it, which is what Python reports.
fn makedev(major: u64, minor: u64) u64 {
    return ((major & 0xfffff000) << 32) | ((major & 0xfff) << 8) | ((minor & 0xffffff00) << 12) | (minor & 0xff);
}

fn devOf(path: []const u8) !u64 {
    var zb: [sys.path_max:0]u8 = undefined;
    var x: linux.Statx = undefined;
    _ = try sys.call(linux.statx, .{ linux.AT.FDCWD, (try sys.z(&zb, path)).ptr, linux.AT.SYMLINK_NOFOLLOW, linux.STATX{ .INO = true }, &x });
    return makedev(x.dev_major, x.dev_minor);
}

fn readlink(a: Allocator, path: []const u8) ![]const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const n = try sys.call(linux.readlinkat, .{ linux.AT.FDCWD, (try sys.z(&zb, path)).ptr, &buf, buf.len });
    return a.dupe(u8, buf[0..n]);
}

fn sha256File(a: Allocator, path: []const u8) ![]const u8 {
    const fd = try sys.openat(fsx.cwd, path, .{ .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    var h = std.crypto.hash.sha2.Sha256.init(.{});
    var buf: [1024 * 1024]u8 = undefined;
    while (true) {
        const n = try sys.read(fd, &buf);
        if (n == 0) break;
        h.update(buf[0..n]);
    }
    var out: [32]u8 = undefined;
    h.final(&out);
    const hex = std.fmt.bytesToHex(out, .lower);
    return a.dupe(u8, &hex);
}

/// A journal integer is an i64. An inode that does not fit is an error.
fn journalInt(value: u64) error{Overflow}!i64 {
    return std.math.cast(i64, value) orelse error.Overflow;
}

fn row(a: Allocator, rel: []const u8, kind: []const u8, st: fsx.Stat, detail: []const u8) !Py {
    const ino = try journalInt(st.ino);
    return .{ .list = try a.dupe(Py, &.{ Py.s(rel), Py.s(kind), .{ .int = ino }, .{ .int = @intCast(st.size) }, Py.s(detail) }) };
}

/// os.walk without following links: symlinked directories are rows of their
/// own and are not entered.
fn walk(a: Allocator, root: []const u8, top: []const u8, rows: *std.ArrayList(Py)) !void {
    // the worklist holds paths, one directory open at a time whatever the
    // depth or the width of the tree
    var stack: std.ArrayList([]const u8) = .empty;
    defer stack.deinit(a);
    try stack.append(a, top);
    while (stack.pop()) |dir_path| {
        const fd = try fsx.openDir(fsx.cwd, dir_path, false);
        defer sys.close(fd);
        var it = try fsx.DirIter.init(a, fd);
        defer it.deinit(a);
        var subdirs: std.ArrayList([]const u8) = .empty;
        while (try it.next()) |e| {
            const child = try std.fs.path.join(a, &.{ dir_path, e.name });
            const st = try fsx.statAt(fsx.cwd, child, false);
            const rel = @import("modulefile.zig").relpath(a, child, root);
            if (st.isLink()) {
                try rows.append(a, try row(a, rel, "symlink", st, try readlink(a, child)));
            } else if (st.isDir()) {
                try subdirs.append(a, child);
            } else {
                try rows.append(a, try row(a, rel, "file", st, try sha256File(a, child)));
            }
        }
        for (subdirs.items) |d| try stack.append(a, d);
    }
}

fn lessRow(_: void, x: Py, y: Py) bool {
    return json.pyTextLess(x.list[0].str, y.list[0].str);
}

/// snapshot(path) as the Python tool records it.
pub fn snapshot(a: Allocator, path: []const u8) !Py {
    const kind = main_mod.lstatKind(path);
    if (std.mem.eql(u8, kind, "missing")) return .{ .dict = try a.dupe(Py.Entry, &.{.{ .key = "exists", .value = .{ .bool = false } }}) };
    if (std.mem.eql(u8, kind, "symlink")) return .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "exists", .value = .{ .bool = true } },
        .{ .key = "symlink", .value = .{ .bool = true } },
        .{ .key = "target", .value = Py.s(try readlink(a, path)) },
    }) };
    if (!std.mem.eql(u8, kind, "dir")) return .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "exists", .value = .{ .bool = true } },
        .{ .key = "symlink", .value = .{ .bool = false } },
        .{ .key = "kind", .value = Py.s(kind) },
    }) };
    const st = try fsx.statAt(fsx.cwd, path, false);
    var rows: std.ArrayList(Py) = .empty;
    try walk(a, path, path, &rows);
    std.mem.sort(Py, rows.items, {}, lessRow);
    const ino = try journalInt(st.ino);
    return .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "exists", .value = .{ .bool = true } },
        .{ .key = "symlink", .value = .{ .bool = false } },
        .{ .key = "kind", .value = Py.s("dir") },
        .{ .key = "inode", .value = .{ .int = ino } },
        .{ .key = "device", .value = .{ .int = @intCast(try devOf(path)) } },
        .{ .key = "entries", .value = .{ .list = rows.items } },
    }) };
}

/// Whether a JSON value has the shape produced by snapshot().
pub fn validSnapshot(value: Py) bool {
    if (value != .dict) return false;
    const exists = pyfmt.get(value, "exists") orelse return false;
    if (exists != .bool) return false;
    if (!exists.bool) return value.dict.len == 1;
    const symlink = pyfmt.get(value, "symlink") orelse return false;
    if (symlink != .bool) return false;
    if (symlink.bool) {
        const target = pyfmt.get(value, "target") orelse return false;
        return value.dict.len == 3 and target == .str;
    }
    const kind = pyfmt.get(value, "kind") orelse return false;
    if (kind != .str) return false;
    if (!std.mem.eql(u8, kind.str, "dir")) return value.dict.len == 3;
    if (value.dict.len != 6) return false;
    const inode = pyfmt.get(value, "inode") orelse return false;
    const device = pyfmt.get(value, "device") orelse return false;
    const entries = pyfmt.get(value, "entries") orelse return false;
    if (inode != .int or device != .int or entries != .list) return false;
    for (entries.list) |entry| {
        if (entry != .list or entry.list.len != 5) return false;
        const fields = entry.list;
        if (fields[0] != .str or fields[1] != .str or fields[2] != .int or fields[3] != .int or fields[4] != .str) return false;
        if (!std.mem.eql(u8, fields[1].str, "file") and !std.mem.eql(u8, fields[1].str, "symlink")) return false;
    }
    return true;
}

/// same_snapshot: equal as JSON values.
pub fn same(a: Allocator, x: Py, y: Py) bool {
    const sx = json.dumps(a, x, 0, true) catch return false;
    const sy = json.dumps(a, y, 0, true) catch return false;
    return std.mem.eql(u8, sx, sy);
}

fn isDirSnap(snap: Py) bool {
    const kind = pyfmt.get(snap, "kind") orelse return false;
    return kind == .str and std.mem.eql(u8, kind.str, "dir");
}

/// Drop device, the directory inode, and each entry's inode. What remains
/// is the entry path, kind, size, and hash or link text.
fn contentView(a: Allocator, snap: Py) !Py {
    if (!isDirSnap(snap) or snap != .dict) return snap;
    var kept: std.ArrayList(Py.Entry) = .empty;
    for (snap.dict) |e| {
        if (std.mem.eql(u8, e.key, "device") or std.mem.eql(u8, e.key, "inode")) continue;
        if (!std.mem.eql(u8, e.key, "entries")) {
            try kept.append(a, e);
            continue;
        }
        if (e.value != .list) return error.Malformed;
        var rows: std.ArrayList(Py) = .empty;
        for (e.value.list) |entry| {
            if (entry != .list or entry.list.len != 5) return error.Malformed;
            const f = entry.list;
            try rows.append(a, .{ .list = try a.dupe(Py, &.{ f[0], f[1], f[3], f[4] }) });
        }
        try kept.append(a, .{ .key = e.key, .value = .{ .list = rows.items } });
    }
    return .{ .dict = kept.items };
}

/// A resume compares entry kind, size, and hash or link text.
/// Device and directory inode are not part of the comparison.
pub fn contentsSame(a: Allocator, x: Py, y: Py) bool {
    const left = contentView(a, x) catch return false;
    const right = contentView(a, y) catch return false;
    return same(a, left, right);
}

/// empty_real_dir: a real directory with no entries.
pub fn emptyRealDir(a: Allocator, path: []const u8) bool {
    if (!std.mem.eql(u8, main_mod.lstatKind(path), "dir")) return false;
    const fd = fsx.openDir(fsx.cwd, path, false) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    return (it.next() catch return false) == null;
}

test "validSnapshot accepts records from snapshot and rejects other JSON shapes" {
    const a = std.testing.allocator;
    const missing = Py{ .dict = try a.dupe(Py.Entry, &.{.{ .key = "exists", .value = .{ .bool = false } }}) };
    defer a.free(missing.dict);
    try std.testing.expect(validSnapshot(missing));
    try std.testing.expect(!validSnapshot(.{ .list = &.{} }));
    const malformed = Py{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "exists", .value = .{ .bool = true } },
        .{ .key = "symlink", .value = .{ .bool = false } },
        .{ .key = "kind", .value = Py.s("dir") },
        .{ .key = "inode", .value = .{ .int = 1 } },
        .{ .key = "device", .value = .{ .int = 1 } },
        .{ .key = "entries", .value = .{ .dict = &.{} } },
    }) };
    defer a.free(malformed.dict);
    try std.testing.expect(!validSnapshot(malformed));
}

fn dirSnap(a: Allocator, inode: i64, device: i64, entries: []const Py) !Py {
    return .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "exists", .value = .{ .bool = true } },
        .{ .key = "symlink", .value = .{ .bool = false } },
        .{ .key = "kind", .value = Py.s("dir") },
        .{ .key = "inode", .value = .{ .int = inode } },
        .{ .key = "device", .value = .{ .int = device } },
        .{ .key = "entries", .value = .{ .list = entries } },
    }) };
}

test "an inode that does not fit an i64 is an error" {
    try std.testing.expectError(error.Overflow, journalInt(@as(u64, 1) << 63));
    try std.testing.expectEqual(@as(i64, 1), try journalInt(1));
}

test "contentsSame ignores device and directory inode" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const file = Py{ .list = &.{ Py.s("PAYLOAD"), Py.s("file"), .{ .int = 3 }, .{ .int = 4 }, Py.s("abc") } };
    const moved = Py{ .list = &.{ Py.s("PAYLOAD"), Py.s("file"), .{ .int = 8 }, .{ .int = 4 }, Py.s("abc") } };
    const left = try dirSnap(a, 10, 20, &.{file});
    const right = try dirSnap(a, 11, 21, &.{moved});
    try std.testing.expect(!same(a, left, right));
    try std.testing.expect(contentsSame(a, left, right));
    const changed = Py{ .list = &.{ Py.s("PAYLOAD"), Py.s("file"), .{ .int = 9 }, .{ .int = 4 }, Py.s("abd") } };
    const other = try dirSnap(a, 10, 20, &.{changed});
    try std.testing.expect(!contentsSame(a, left, other));
}

test "walk crosses a deep tree under a tight descriptor limit" {
    const gpa = std.testing.allocator;
    const seed: u64 = 0x5EEDC1;
    var prng = std.Random.DefaultPrng.init(seed);
    const rand = prng.random();
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(gpa, "{s}/.zig-cache/ebsr-livewalk-{d}", .{ here, linux.getpid() });
    defer gpa.free(base);
    try common.atomic.makedirs(base);

    var names: std.ArrayList(u8) = .empty;
    defer names.deinit(gpa);
    var zb: [sys.path_max:0]u8 = undefined;
    var fd = try fsx.openDir(fsx.cwd, base, false);
    defer sys.close(fd);
    var depth: usize = 0;
    while (depth < 1025) : (depth += 1) {
        const c: u8 = 'a' + @as(u8, @intCast(rand.uintLessThan(u8, 26)));
        try names.append(gpa, c);
        const nm = names.items[names.items.len - 1 ..];
        _ = try sys.call(linux.mkdirat, .{ fd, (try sys.z(&zb, nm)).ptr, @as(linux.mode_t, 0o755) });
        const sub = try fsx.openDir(fd, nm, false);
        sys.close(fd);
        fd = sub;
    }
    const leaf = try sys.openat(fd, "leaf.txt", .{ .ACCMODE = .WRONLY, .CREAT = true }, 0o644);
    try sys.writeAll(leaf, "deep\n");
    sys.close(leaf);

    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var rows: std.ArrayList(Py) = .empty;
    walk(a, base, base, &rows) catch |err| {
        std.debug.print("livecheck walk seed: {d}: gave {s} errno E{s}\n", .{ seed, @errorName(err), @tagName(sys.last_errno) });
        return err;
    };
    var found = false;
    for (rows.items) |r| {
        const p = r.list[0].str;
        if (std.mem.endsWith(u8, p, "leaf.txt")) found = true;
    }
    if (!found) return error.TestUnexpectedResult;

    var full: std.ArrayList(u8) = .empty;
    defer full.deinit(gpa);
    try full.appendSlice(gpa, base);
    for (names.items) |ch| {
        try full.append(gpa, '/');
        try full.append(gpa, ch);
    }
    try full.appendSlice(gpa, "/leaf.txt");
    var fz: [sys.path_max:0]u8 = undefined;
    _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, full.items), 0);
    full.shrinkRetainingCapacity(full.items.len - "/leaf.txt".len);
    while (full.items.len > base.len) {
        _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, full.items), linux.AT.REMOVEDIR);
        full.shrinkRetainingCapacity(full.items.len - 2);
    }
    _ = try sys.unlinkat(fsx.cwd, try sys.z(&fz, base), linux.AT.REMOVEDIR);
}
