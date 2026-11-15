//! Find the generation directory path in a staged tree.
//!
//! A build that records the generation directory leaves paths that name
//! nothing once publication renames the tree. Every regular file of a staged
//! installation is checked for four kinds of record: `rpath` (an ELF
//! DT_RPATH entry), `runpath` (DT_RUNPATH), `interp` (PT_INTERP) and `text`
//! (the path as a byte string in any other file). An ELF file is read through
//! its program headers only. A match must end at the end of the data or at a
//! byte that cannot continue a path component, so `generations/r1` is not
//! found in `generations/r10`. This is eb_staged_rebuild/genscan.py.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const admin = @import("admin.zig");
const modulefile = @import("modulefile.zig");
const main_mod = @import("main.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const Allocator = std.mem.Allocator;

pub const Error = error{Refused} || Allocator.Error;

/// The order hits sort in after the path.
pub const Kind = enum(u8) { rpath, runpath, interp, text };
pub const Hit = struct { rel: []const u8, kind: Kind };

pub const stamp = ".eb-staged-rebuild-complete";
/// The stamp's temporary file, left by an interrupted write of it.
const stamp_temp = "." ++ stamp ++ ".new";
const chunk = 1 << 20;
const max_string = 1 << 16;
const max_dynamic = 1 << 20;
const max_program_headers = 1024;
const max_program_table = 1 << 16;
const max_interp = 4096;
const max_offset: u128 = 1 << 63;

const pt_load = 1;
const pt_dynamic = 2;
const pt_interp = 3;
const dt_null = 0;
const dt_strtab = 5;
const dt_rpath = 15;
const dt_runpath = 29;

/// The bit of a kind in a set of kinds.
fn bit(k: Kind) u8 {
    return @as(u8, 1) << @as(u3, @intCast(@backingInt(k)));
}

fn nameByte(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '+' or c == '-';
}

/// `needle` in `data`, followed by a non-name byte or the end of data.
fn hasPath(data: []const u8, needle: []const u8) bool {
    var start: usize = 0;
    while (std.mem.indexOfPos(u8, data, start, needle)) |i| {
        const end = i + needle.len;
        if (end >= data.len or !nameByte(data[end])) return true;
        start = i + 1;
    }
    return false;
}

fn pread(fd: linux.fd_t, buf: []u8, offset: u128) ?usize {
    if (offset >= max_offset) return null;
    return sys.call(linux.pread, .{ fd, buf.ptr, buf.len, @as(i64, @intCast(offset)) }) catch null;
}

/// Exactly `size` bytes at `offset`.
fn readAt(fd: linux.fd_t, buf: []u8, size: usize, offset: u128) ?[]const u8 {
    if (size > buf.len) return null;
    const n = pread(fd, buf[0..size], offset) orelse return null;
    return if (n == size) buf[0..size] else null;
}

fn cstringAt(fd: linux.fd_t, buf: []u8, limit: usize, offset: u128) ?[]const u8 {
    const n = pread(fd, buf[0..@min(limit, buf.len)], offset) orelse return null;
    if (n == 0) return null;
    return buf[0 .. std.mem.indexOfScalar(u8, buf[0..n], 0) orelse n];
}

fn rd(comptime T: type, b: []const u8, off: usize, big: bool) T {
    return std.mem.readInt(T, b[off..][0..@sizeOf(T)], if (big) .big else .little);
}

const Scanner = struct {
    needle: []const u8,
    text_buf: []u8,
    dynamic_buf: []u8,
    string_buf: []u8,

    fn textHas(sc: *Scanner, fd: linux.fd_t) !bool {
        const needle = sc.needle;
        var carry: usize = 0;
        while (true) {
            const n = try sys.read(fd, sc.text_buf[carry .. carry + chunk]);
            const final = n == 0;
            const len = carry + n;
            const data = sc.text_buf[0..len];
            var start: usize = 0;
            while (std.mem.indexOfPos(u8, data, start, needle)) |i| {
                const end = i + needle.len;
                if (end < len) {
                    if (!nameByte(data[end])) return true;
                } else if (final) return true;
                start = i + 1;
            }
            if (final) return false;
            const keep = @min(needle.len, len);
            std.mem.copyForwards(u8, sc.text_buf[0..keep], data[len - keep ..]);
            carry = keep;
        }
    }

    /// The kinds of record in an ELF file, as a bit set indexed by Kind.
    fn elfKinds(sc: *Scanner, fd: linux.fd_t) u8 {
        var head: [64]u8 = undefined;
        const hn = pread(fd, &head, 0) orelse return 0;
        if (hn < 52) return 0;
        const cls = head[4];
        const big = switch (head[5]) {
            1 => false,
            2 => true,
            else => return 0,
        };
        var phoff: u128 = undefined;
        var phentsize: usize = undefined;
        var phnum: usize = undefined;
        var minimum: usize = undefined;
        if (cls == 2 and hn == 64) {
            phoff = rd(u64, &head, 32, big);
            phentsize = rd(u16, &head, 54, big);
            phnum = rd(u16, &head, 56, big);
            minimum = 56;
        } else if (cls == 1) {
            phoff = rd(u32, &head, 28, big);
            phentsize = rd(u16, &head, 42, big);
            phnum = rd(u16, &head, 44, big);
            minimum = 32;
        } else return 0;
        if (phentsize < minimum or phnum > max_program_headers or phentsize * phnum > max_program_table) return 0;
        var table_buf: [max_program_table]u8 = undefined;
        const table = readAt(fd, &table_buf, phentsize * phnum, phoff) orelse return 0;
        const Load = struct { vaddr: u128, offset: u128, filesz: u128 };
        var loads: [max_program_headers]Load = undefined;
        var nloads: usize = 0;
        var interp: ?[2]u128 = null;
        var dynamic: ?[2]u128 = null;
        var i: usize = 0;
        while (i < phnum) : (i += 1) {
            const base = i * phentsize;
            var kind: u32 = undefined;
            var offset: u128 = undefined;
            var vaddr: u128 = undefined;
            var filesz: u128 = undefined;
            if (cls == 2) {
                kind = rd(u32, table, base, big);
                offset = rd(u64, table, base + 8, big);
                vaddr = rd(u64, table, base + 16, big);
                filesz = rd(u64, table, base + 32, big);
            } else {
                kind = rd(u32, table, base, big);
                offset = rd(u32, table, base + 4, big);
                vaddr = rd(u32, table, base + 8, big);
                filesz = rd(u32, table, base + 16, big);
            }
            if (kind == pt_load) {
                loads[nloads] = .{ .vaddr = vaddr, .offset = offset, .filesz = filesz };
                nloads += 1;
            } else if (kind == pt_interp and interp == null) {
                interp = .{ offset, filesz };
            } else if (kind == pt_dynamic and dynamic == null) {
                dynamic = .{ offset, filesz };
            }
        }
        var kinds: u8 = 0;
        if (dynamic) |d| {
            const entries = readAt(fd, sc.dynamic_buf, @intCast(@min(d[1], max_dynamic)), d[0]) orelse &[_]u8{};
            const size: usize = if (cls == 2) 16 else 8;
            var strtab: ?u128 = null;
            var rpath: ?u128 = null;
            var runpath: ?u128 = null;
            var base: usize = 0;
            while (base + size <= entries.len) : (base += size) {
                const tag: u64 = if (cls == 2) rd(u64, entries, base, big) else rd(u32, entries, base, big);
                const value: u128 = if (cls == 2) rd(u64, entries, base + 8, big) else rd(u32, entries, base + 4, big);
                if (tag == dt_null) break;
                if (tag == dt_strtab and strtab == null) {
                    strtab = value;
                } else if (tag == dt_rpath and rpath == null) {
                    rpath = value;
                } else if (tag == dt_runpath and runpath == null) {
                    runpath = value;
                }
            }
            var table_offset: ?u128 = null;
            if (strtab) |st| for (loads[0..nloads]) |l| {
                if (l.vaddr <= st and st < l.vaddr + l.filesz) {
                    table_offset = l.offset + (st - l.vaddr);
                    break;
                }
            };
            if (table_offset) |to| {
                const pairs = [_]struct { v: ?u128, k: Kind }{ .{ .v = rpath, .k = .rpath }, .{ .v = runpath, .k = .runpath } };
                for (pairs) |pr| {
                    const v = pr.v orelse continue;
                    const text = cstringAt(fd, sc.string_buf, max_string, to + v) orelse continue;
                    if (hasPath(text, sc.needle)) kinds |= bit(pr.k);
                }
            }
        }
        if (interp) |ip| {
            const text = cstringAt(fd, sc.string_buf, @intCast(@min(ip[1], max_interp)), ip[0]);
            if (text) |t| if (hasPath(t, sc.needle)) {
                kinds |= bit(.interp);
            };
        }
        return kinds;
    }

    fn fileKinds(sc: *Scanner, path: []const u8) !u8 {
        const f = try fsx.openRegular(fsx.cwd, path, false);
        defer sys.close(f.fd);
        var magic: [4]u8 = undefined;
        const n = pread(f.fd, &magic, 0) orelse 0;
        if (n == 4 and std.mem.eql(u8, &magic, "\x7fELF")) return sc.elfKinds(f.fd);
        _ = linux.lseek(f.fd, 0, linux.SEEK.SET);
        return if (try sc.textHas(f.fd)) bit(.text) else 0;
    }
};

fn hitLess(_: void, x: Hit, y: Hit) bool {
    if (!std.mem.eql(u8, x.rel, y.rel)) return std.mem.lessThan(u8, x.rel, y.rel);
    return @backingInt(x.kind) < @backingInt(y.kind);
}

/// scan_tree for every tree in `roots`, then sort_hits. Paths are relative
/// to `base`.
pub fn scan(a: Allocator, roots: []const []const u8, base: []const u8, generation_root: []const u8, why: *[]const u8) Error![]const Hit {
    var sc: Scanner = .{
        .needle = generation_root,
        .text_buf = try a.alloc(u8, chunk + generation_root.len),
        .dynamic_buf = try a.alloc(u8, max_dynamic),
        .string_buf = try a.alloc(u8, max_string),
    };
    var hits: std.ArrayList(Hit) = .empty;
    for (roots) |root| {
        var stack: std.ArrayList(struct { path: []const u8, top: bool }) = .empty;
        try stack.append(a, .{ .path = root, .top = true });
        while (stack.pop()) |item| {
            const fd = fsx.openDir(fsx.cwd, item.path, false) catch {
                why.* = try a.print("cannot scan {s}: not readable", .{modulefile.relpath(a, item.path, base)});
                return error.Refused;
            };
            defer sys.close(fd);
            var it = fsx.DirIter.init(a, fd) catch |err| switch (err) {
                error.OutOfMemory => return error.OutOfMemory,
            };
            while (true) {
                const e = it.next() catch {
                    why.* = try a.print("cannot scan {s}: not readable", .{modulefile.relpath(a, item.path, base)});
                    return error.Refused;
                } orelse break;
                const child = try std.fs.path.join(a, &.{ item.path, e.name });
                const st = fsx.statAt(fsx.cwd, child, false) catch continue;
                if (st.isLink()) continue;
                if (st.isDir()) {
                    try stack.append(a, .{ .path = child, .top = false });
                } else if (st.isReg()) {
                    if (item.top and (std.mem.eql(u8, e.name, stamp) or std.mem.eql(u8, e.name, stamp_temp))) continue;
                    const kinds = sc.fileKinds(child) catch {
                        why.* = try a.print("cannot scan {s}: not readable", .{modulefile.relpath(a, child, base)});
                        return error.Refused;
                    };
                    if (kinds == 0) continue;
                    const rel = modulefile.relpath(a, child, base);
                    for ([_]Kind{ .rpath, .runpath, .interp, .text }) |k| {
                        if (kinds & bit(k) != 0) try hits.append(a, .{ .rel = rel, .kind = k });
                    }
                }
            }
        }
    }
    std.mem.sort(Hit, hits.items, {}, hitLess);
    return hits.items;
}

test "a match must end at the end of the data or at a non-name byte" {
    try std.testing.expect(hasPath("/s/generations/r1", "/s/generations/r1"));
    try std.testing.expect(hasPath("a:/s/generations/r1/lib:b", "/s/generations/r1"));
    try std.testing.expect(!hasPath("/s/generations/r10/lib", "/s/generations/r1"));
    try std.testing.expect(hasPath("/s/generations/r10 /s/generations/r1/x", "/s/generations/r1"));
    try std.testing.expect(!hasPath("", "/s/generations/r1"));
}
