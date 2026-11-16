//! Rewrite a generation install prefix to the stable prefix inside a staged
//! tree. installpath and userns record the generation directory; publication
//! renames that directory, so the bytes have to name the stable path first.
//! Text grows or shrinks. A binary's C string is shortened in place, which
//! keeps an ELF string table's following entries where they are. The
//! completion stamp is left alone.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");

const sys = common.sys;
const fsx = common.fsx;
const oom = @import("oom.zig").oom;
const Allocator = std.mem.Allocator;

pub const Error = error{ Refused, OutOfMemory };

fn pathChar(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '+' or c == '-';
}

const skip_name = ".eb-staged-rebuild-complete";

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

fn readFile(a: Allocator, path: []const u8) ![]u8 {
    const fd = try sys.openat(fsx.cwd, path, .{ .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    return fsx.readAll(a, fd, 1 << 30);
}

fn chmod(path: []const u8, mode: u32) void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return;
    _ = linux.fchmodat(linux.AT.FDCWD, z.ptr, @intCast(mode));
}

fn store(path: []const u8, data: []const u8) !void {
    const st = try fsx.statAt(fsx.cwd, path, false);
    const mode = st.perm();
    const writable = mode & 0o200 != 0;
    if (!writable) chmod(path, mode | 0o200);
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .NOFOLLOW = true, .TRUNC = true }, 0) catch |err| {
        if (!writable) chmod(path, mode);
        return err;
    };
    defer sys.close(fd);
    sys.writeAll(fd, data) catch |err| {
        if (!writable) chmod(path, mode);
        return err;
    };
    if (!writable) chmod(path, mode);
}

fn readlink(a: Allocator, path: []const u8) ![]u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const n = try sys.call(linux.readlinkat, .{ linux.AT.FDCWD, (try sys.z(&zb, path)).ptr, &buf, buf.len });
    return a.dupe(u8, buf[0..n]);
}

fn relink(a: Allocator, path: []const u8, target: []const u8) !void {
    sys.unlinkat(fsx.cwd, path, 0) catch |err| return err;
    var zt: [sys.path_max:0]u8 = undefined;
    var zp: [sys.path_max:0]u8 = undefined;
    const zt_ = try sys.z(&zt, target);
    const zp_ = try sys.z(&zp, path);
    if (linux.errno(linux.symlinkat(zt_.ptr, linux.AT.FDCWD, zp_.ptr)) != .SUCCESS) return error.Unexpected;
    _ = a;
}

fn rewriteOne(a: Allocator, path: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    if (std.mem.eql(u8, std.fs.path.basename(path), skip_name)) return;
    const kind = main_mod.lstatKind(path);
    if (std.mem.eql(u8, kind, "symlink")) {
        const target = readlink(a, path) catch |err| {
            why.* = main_mod.osError(a, err, path);
            return error.Refused;
        };
        const updated = try replacePrefix(a, target, old, new, why);
        if (updated.count == 0) return;
        relink(a, path, updated.bytes) catch |err| {
            why.* = main_mod.osError(a, err, path);
            return error.Refused;
        };
        return;
    }
    if (!std.mem.eql(u8, kind, "file")) return;
    const data = readFile(a, path) catch |err| {
        why.* = main_mod.osError(a, err, path);
        return error.Refused;
    };
    if (std.mem.indexOf(u8, data, old) == null) return;
    const updated = if (std.mem.indexOfScalar(u8, data, 0) != null)
        try rewriteBinary(a, data, old, new, path, why)
    else
        (try replacePrefix(a, data, old, new, why)).bytes;
    if (std.mem.eql(u8, updated, data)) return;
    store(path, updated) catch |err| {
        why.* = main_mod.osError(a, err, path);
        return error.Refused;
    };
}

fn rewriteTree(a: Allocator, root: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    if (std.mem.eql(u8, old, new)) return;
    const fd = fsx.openDir(fsx.cwd, root, false) catch |err| {
        why.* = main_mod.osError(a, err, root);
        return error.Refused;
    };
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch |err| {
        why.* = main_mod.osError(a, err, root);
        return error.Refused;
    };
    var subdirs: std.ArrayList([]const u8) = .empty;
    while (true) {
        const entry = it.next() catch |err| {
            why.* = main_mod.osError(a, err, root);
            return error.Refused;
        } orelse break;
        const child = try std.fs.path.join(a, &.{ root, entry.name });
        const kind = main_mod.lstatKind(child);
        if (std.mem.eql(u8, kind, "dir")) {
            try subdirs.append(a, child);
        } else {
            try rewriteOne(a, child, old, new, why);
        }
    }
    for (subdirs.items) |dir| try rewriteTree(a, dir, old, new, why);
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
