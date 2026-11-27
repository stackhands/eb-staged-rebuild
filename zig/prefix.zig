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
const max_bytes: usize = 1 << 30;

const Window = struct {
    fd: fsx.fd_t,
    mode: u32,
    sealed: bool,

    fn close(self: Window) void {
        if (self.sealed) _ = atomic.ops.fchmod(self.fd, self.mode);
        sys.close(self.fd);
    }
};

fn openWritable(a: Allocator, anchor: Anchor, directory: []const u8, why: *[]const u8) Error!Window {
    const fd = openAnchored(anchor, directory, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, directory);
    const info = fsx.fstat(fd) catch |err| {
        sys.close(fd);
        return fail(a, why, err, directory);
    };
    const mode = info.perm();
    const sealed = mode & 0o200 == 0;
    if (sealed) {
        const rc = atomic.ops.fchmod(fd, mode | 0o300);
        const failed = linux.errno(rc);
        if (failed != .SUCCESS) {
            sys.close(fd);
            why.* = main_mod.errnoPath(a, failed, directory);
            return error.Refused;
        }
    }
    return .{ .fd = fd, .mode = mode, .sealed = sealed };
}

fn fail(a: Allocator, why: *[]const u8, err: anyerror, path: []const u8) Error {
    why.* = main_mod.osError(a, err, path);
    return error.Refused;
}

fn readlinkExact(a: Allocator, path: []const u8) ![]u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const n = try sys.call(linux.readlinkat, .{ linux.AT.FDCWD, (try sys.z(&zb, path)).ptr, &buf, buf.len });
    if (n == buf.len) return error.NameTooLong;
    return a.dupe(u8, buf[0..n]);
}

fn metadataFromFd(a: Allocator, fd: sys.fd_t, path: []const u8, why: *atomic.Why) atomic.Error!atomic.Metadata {
    const info = try fsx.fstat(fd);
    if (!info.isReg()) {
        why.msg = a.print("metadata source is not a regular file: {s}", .{path}) catch return error.OutOfMemory;
        return error.Refused;
    }
    const acls = try atomic.readAcls(a, fd, why);
    errdefer acls.deinit(a);
    const again = try fsx.fstat(fd);
    if (again.dev != info.dev or again.ino != info.ino or again.uid != info.uid or again.gid != info.gid or again.mode != info.mode or again.ctime.sec != info.ctime.sec or again.ctime.nsec != info.ctime.nsec) {
        why.msg = a.dupe(u8, "access metadata changed during inspection") catch return error.OutOfMemory;
        return error.Refused;
    }
    return .{ .info = info, .acls = acls };
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
    var inner: atomic.Why = .{};
    const now = atomic.readAcls(a, fd, &inner) catch {
        why.* = if (inner.msg.len > 0) inner.msg else "cannot read access metadata";
        return error.Refused;
    };
    if (!now.eql(meta.acls)) {
        for (meta.acls.present, 0..) |present, i| if (present) |value| {
            if (linux.errno(atomic.ops.fsetxattr(fd, atomic.acl_attributes[i], value)) != .SUCCESS) {
                why.* = "cannot preserve access control of a rewritten file";
                return error.Refused;
            }
            break;
        };
    }
    for (0..atomic.acl_attributes.len) |_| {
        const present = atomic.readAcls(a, fd, &inner) catch {
            why.* = if (inner.msg.len > 0) inner.msg else "cannot read access metadata";
            return error.Refused;
        };
        var extra: ?usize = null;
        for (present.present, meta.acls.present, 0..) |have, want, i| if (have != null and want == null) {
            extra = i;
            break;
        };
        const i = extra orelse break;
        if (linux.errno(atomic.ops.fremovexattr(fd, atomic.acl_attributes[i])) != .SUCCESS) {
            why.* = "cannot preserve access control of a rewritten file";
            return error.Refused;
        }
    }
    const after = atomic.readAcls(a, fd, &inner) catch {
        why.* = if (inner.msg.len > 0) inner.msg else "cannot read access metadata";
        return error.Refused;
    };
    const restored = fsx.fstat(fd) catch |err| return fail(a, why, err, "rewritten file");
    if (restored.uid != meta.info.uid or restored.gid != meta.info.gid or restored.perm() != mode or !after.eql(meta.acls)) {
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

fn commitNames(a: Allocator, anchor: Anchor, names: []const []const u8, ready: []const u8, why: *[]const u8) Error!void {
    if (names.len == 1) {
        const parent = std.fs.path.dirname(names[0]) orelse "/";
        renameAnchored(anchor, ready, names[0]) catch |err| return fail(a, why, err, names[0]);
        fsyncAnchored(anchor, parent) catch |err| return fail(a, why, err, parent);
        return;
    }
    for (names) |name| {
        const parent = std.fs.path.dirname(name) orelse ".";
        const temporary = try std.fmt.allocPrint(a, "{s}/.{s}.{d}.ebsr-link", .{ parent, std.fs.path.basename(name), linux.getpid() });
        linkAnchored(anchor, ready, temporary) catch |err| return fail(a, why, err, name);
        renameAnchored(anchor, temporary, name) catch |err| {
            unlinkAnchored(anchor, temporary, 0) catch {};
            return fail(a, why, err, name);
        };
        fsyncAnchored(anchor, parent) catch |err| return fail(a, why, err, parent);
    }
    const ready_parent = std.fs.path.dirname(ready) orelse "/";
    if (injectedReadyUnlink()) return fail(a, why, error.AccessDenied, ready);
    unlinkAnchored(anchor, ready, 0) catch |err| return fail(a, why, err, ready);
    fsyncAnchored(anchor, ready_parent) catch |err| return fail(a, why, err, ready_parent);
}

fn publish(a: Allocator, anchor: Anchor, names: []const []const u8, image: []const u8, why: *[]const u8) Error!void {
    const source = names[0];
    const info = statAnchored(anchor, source, false) catch |err| return fail(a, why, err, source);
    const parent = std.fs.path.dirname(source) orelse ".";
    const partial = try std.fmt.allocPrint(a, "{s}/.{d}.ebsr-partial", .{ parent, info.ino });
    const ready = try std.fmt.allocPrint(a, "{s}/.{d}.ebsr-ready", .{ parent, info.ino });
    const src = openAnchored(anchor, source, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true, .NONBLOCK = true }, 0) catch |err| return fail(a, why, err, source);
    defer sys.close(src);
    var inner: atomic.Why = .{};
    const meta = metadataFromFd(a, src, source, &inner) catch {
        why.* = if (inner.msg.len > 0) inner.msg else "cannot read access metadata";
        return error.Refused;
    };
    const window = try openWritable(a, anchor, parent, why);
    defer window.close();
    const fd = openAnchored(anchor, partial, .{ .ACCMODE = .RDWR, .CREAT = true, .EXCL = true, .NOFOLLOW = true }, 0o600) catch |err| return fail(a, why, err, partial);
    var failed = true;
    defer if (failed) {
        sys.close(fd);
        unlinkAnchored(anchor, partial, 0) catch {};
    };
    sys.writeAll(fd, image) catch |err| return fail(a, why, err, partial);
    try punchZeros(a, fd, image, partial, why);
    try copyUserXattrs(a, src, fd, source, partial, why);
    try restoreAccess(a, fd, meta, why);
    sys.fsync(fd) catch |err| return fail(a, why, err, partial);
    sys.close(fd);
    failed = false;
    renameAnchored(anchor, partial, ready) catch |err| return fail(a, why, err, ready);
    // The directory entry for the finished file has to be durable before
    // the published name starts pointing at it. Python flushes here.
    fsyncAnchored(anchor, parent) catch |err| {
        unlinkAnchored(anchor, ready, 0) catch {};
        return fail(a, why, err, parent);
    };
    try commitNames(a, anchor, names, ready, why);
}

fn rewriteSymlink(a: Allocator, anchor: Anchor, path: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    const target = readlinkAnchored(a, anchor, path) catch |err| return fail(a, why, err, path);
    const updated = try replacePrefix(a, target, old, new, why);
    if (updated.count == 0) return;
    const parent = std.fs.path.dirname(path) orelse ".";
    const temporary = try std.fmt.allocPrint(a, "{s}/.{s}.{d}.ebsr-symlink", .{ parent, std.fs.path.basename(path), linux.getpid() });
    const window = try openWritable(a, anchor, parent, why);
    defer window.close();
    symlinkAnchored(anchor, updated.bytes, temporary) catch {
        why.* = try a.print("cannot replace symbolic link {s}", .{path});
        return error.Refused;
    };
    renameAnchored(anchor, temporary, path) catch |err| {
        unlinkAnchored(anchor, temporary, 0) catch {};
        return fail(a, why, err, path);
    };
    fsyncAnchored(anchor, parent) catch |err| return fail(a, why, err, parent);
}

const private_suffixes = [_][]const u8{
    ".ebsr-partial",
    ".ebsr-ready",
    ".ebsr-symlink",
    ".ebsr-link",
};

fn ours(name: []const u8) bool {
    if (std.mem.eql(u8, name, skip_name)) return true;
    for (private_suffixes) |suffix| {
        if (std.mem.endsWith(u8, name, suffix)) return true;
    }
    return false;
}

const Anchor = struct {
    fd: sys.fd_t,
    path: []const u8,
};

const Parent = struct {
    fd: sys.fd_t,
    name: []const u8,
    owned: bool,

    fn close(self: Parent) void {
        if (self.owned) sys.close(self.fd);
    }
};

fn relOf(anchor: Anchor, path: []const u8) error{Outside}![]const u8 {
    if (std.mem.eql(u8, path, anchor.path)) return "";
    if (anchor.path.len > 0 and path.len > anchor.path.len and std.mem.startsWith(u8, path, anchor.path) and path[anchor.path.len] == '/')
        return path[anchor.path.len + 1 ..];
    return error.Outside;
}

/// Open `rel` under `dir`, one component at a time, with `O_DIRECTORY` and
/// `O_NOFOLLOW`. An empty component or `.` is skipped. `..` is
/// `error.Outside`. The returned descriptor is new; `dir` stays open.
pub fn openDirComponents(dir: sys.fd_t, rel: []const u8) (sys.Error || error{Outside})!sys.fd_t {
    var fd = dir;
    var owned = false;
    errdefer if (owned) sys.close(fd);
    var it = std.mem.splitScalar(u8, rel, '/');
    while (it.next()) |comp| {
        if (comp.len == 0 or std.mem.eql(u8, comp, ".")) continue;
        if (std.mem.eql(u8, comp, "..")) return error.Outside;
        const next = try fsx.openDir(fd, comp, false);
        if (owned) sys.close(fd);
        fd = next;
        owned = true;
    }
    if (!owned) return fsx.openDir(dir, ".", false);
    return fd;
}

/// close(2) once. Linux releases the descriptor even when close fails, so
/// this does not retry. EINTR is that release, not a failure to report.
pub fn closeChecked(fd: sys.fd_t) sys.Error!void {
    if (try sys.result(linux.close(fd))) |_| return;
}

/// fsync `fd`, then close it. A failed close after a successful fsync is
/// an error. The descriptor is released on every path.
pub fn fsyncCloseFd(fd: sys.fd_t) sys.Error!void {
    sys.fsync(fd) catch |err| {
        sys.close(fd);
        return err;
    };
    return closeChecked(fd);
}

/// Open `rel` under `anchor`, one component at a time, without following a link.
fn openDirRel(anchor: Anchor, rel: []const u8) !sys.fd_t {
    return openDirComponents(anchor.fd, rel) catch |err| switch (err) {
        error.Outside => error.InvalidArgument,
        else => |e| e,
    };
}

fn parentFd(anchor: Anchor, path: []const u8) !Parent {
    const rel = try relOf(anchor, path);
    if (rel.len == 0) return error.InvalidArgument;
    const cut = std.mem.lastIndexOfScalar(u8, rel, '/');
    const parent_rel = if (cut) |i| rel[0..i] else "";
    const name = if (cut) |i| rel[i + 1 ..] else rel;
    if (name.len == 0 or std.mem.eql(u8, name, ".") or std.mem.eql(u8, name, "..")) return error.InvalidArgument;
    if (parent_rel.len == 0) return .{ .fd = anchor.fd, .name = name, .owned = false };
    return .{ .fd = try openDirRel(anchor, parent_rel), .name = name, .owned = true };
}

fn openAnchored(anchor: Anchor, path: []const u8, flags: sys.O, mode: linux.mode_t) !sys.fd_t {
    const rel = try relOf(anchor, path);
    if (rel.len == 0) return sys.openat(anchor.fd, ".", flags, mode);
    const parent = try parentFd(anchor, path);
    defer parent.close();
    return sys.openat(parent.fd, parent.name, flags, mode);
}

fn statAnchored(anchor: Anchor, path: []const u8, follow: bool) !fsx.Stat {
    const rel = try relOf(anchor, path);
    if (rel.len == 0) return fsx.fstat(anchor.fd);
    const parent = try parentFd(anchor, path);
    defer parent.close();
    return fsx.statAt(parent.fd, parent.name, follow);
}

fn kindAnchored(anchor: Anchor, path: []const u8) []const u8 {
    const st = statAnchored(anchor, path, false) catch return "missing";
    if (st.isLink()) return "symlink";
    if (st.isDir()) return "dir";
    if (st.isReg()) return "file";
    return "other";
}

fn unlinkAnchored(anchor: Anchor, path: []const u8, flags: u32) !void {
    const parent = try parentFd(anchor, path);
    defer parent.close();
    try sys.unlinkat(parent.fd, parent.name, flags);
}

fn renameAnchored(anchor: Anchor, old_path: []const u8, new_path: []const u8) !void {
    const old_p = try parentFd(anchor, old_path);
    defer old_p.close();
    const new_p = try parentFd(anchor, new_path);
    defer new_p.close();
    try sys.renameat(old_p.fd, old_p.name, new_p.fd, new_p.name);
}

fn linkAnchored(anchor: Anchor, old_path: []const u8, new_path: []const u8) !void {
    const old_p = try parentFd(anchor, old_path);
    defer old_p.close();
    const new_p = try parentFd(anchor, new_path);
    defer new_p.close();
    var zo: [sys.path_max:0]u8 = undefined;
    var zn: [sys.path_max:0]u8 = undefined;
    const left = try sys.z(&zo, old_p.name);
    const right = try sys.z(&zn, new_p.name);
    if (linux.errno(linux.linkat(old_p.fd, left.ptr, new_p.fd, right.ptr, 0)) != .SUCCESS) return error.Unexpected;
}

fn symlinkAnchored(anchor: Anchor, target: []const u8, path: []const u8) !void {
    const parent = try parentFd(anchor, path);
    defer parent.close();
    var zt: [sys.path_max:0]u8 = undefined;
    var zn: [sys.path_max:0]u8 = undefined;
    const text = try sys.z(&zt, target);
    const name = try sys.z(&zn, parent.name);
    if (linux.errno(linux.symlinkat(text.ptr, parent.fd, name.ptr)) != .SUCCESS) return error.Unexpected;
}

fn fsyncAnchored(anchor: Anchor, path: []const u8) !void {
    const fd = try openAnchored(anchor, path, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    try fsyncCloseFd(fd);
}

fn readlinkAnchored(a: Allocator, anchor: Anchor, path: []const u8) ![]u8 {
    const parent = try parentFd(anchor, path);
    defer parent.close();
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const n = try sys.call(linux.readlinkat, .{ parent.fd, (try sys.z(&zb, parent.name)).ptr, &buf, buf.len });
    if (n == buf.len) return error.NameTooLong;
    return a.dupe(u8, buf[0..n]);
}

const Group = struct { dev: u64, ino: u64, nlink: u32, names: std.ArrayList([]const u8) };

fn consider(a: Allocator, anchor: Anchor, groups: *std.ArrayList(Group), path: []const u8, why: *[]const u8) Error!void {
    const info = statAnchored(anchor, path, false) catch |err| return fail(a, why, err, path);
    if (!info.isReg()) return;
    for (groups.items) |*group| if (group.dev == info.dev and group.ino == info.ino) {
        try group.names.append(a, path);
        return;
    };
    var names: std.ArrayList([]const u8) = .empty;
    try names.append(a, path);
    try groups.append(a, .{ .dev = info.dev, .ino = info.ino, .nlink = info.nlink, .names = names });
}

fn collect(a: Allocator, anchor: Anchor, root: []const u8, groups: *std.ArrayList(Group), links: *std.ArrayList([]const u8), why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    {
        // the descriptor and the iterator are closed before the walk
        // descends, so one is held at a time whatever the tree's shape
        const fd = openAnchored(anchor, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            if (ours(item.name)) continue;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            const kind = kindAnchored(anchor, child);
            if (std.mem.eql(u8, kind, "symlink")) {
                try links.append(a, child);
            } else if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
            } else if (std.mem.eql(u8, kind, "file")) {
                try consider(a, anchor, groups, child, why);
            }
        }
    }
    for (subdirs.items) |dir| try collect(a, anchor, dir, groups, links, why);
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

fn discardPath(a: Allocator, anchor: Anchor, path: []const u8, why: *[]const u8) Error!void {
    unlinkAnchored(anchor, path, 0) catch |err| switch (err) {
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
fn recoverTree(a: Allocator, anchor: Anchor, why: *[]const u8) Error!void {
    var readies: std.ArrayList([]const u8) = .empty;
    try sweepReady(a, anchor, anchor.path, &readies, true, why);
    for (readies.items) |ready| {
        const inode = readyInode(ready) orelse continue;
        var names: std.ArrayList([]const u8) = .empty;
        try namesOnInode(a, anchor, anchor.path, inode, ready, &names, why);
        if (names.items.len == 0) {
            try discardPath(a, anchor, ready, why);
            continue;
        }
        std.mem.sort([]const u8, names.items, {}, pathLess);
        try commitNames(a, anchor, names.items, ready, why);
    }
    var gone: std.ArrayList([]const u8) = .empty;
    try sweepReady(a, anchor, anchor.path, &gone, false, why);
}

fn readyInode(ready: []const u8) ?u64 {
    const base = std.fs.path.basename(ready);
    if (base.len < 2 or base[0] != '.') return null;
    const rest = base[1..];
    const dot = std.mem.indexOfScalar(u8, rest, '.') orelse return null;
    if (!std.mem.eql(u8, rest[dot + 1 ..], "ebsr-ready")) return null;
    return std.fmt.parseInt(u64, rest[0..dot], 10) catch null;
}

fn sweepReady(a: Allocator, anchor: Anchor, root: []const u8, found: *std.ArrayList([]const u8), keep_ready: bool, why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    {
        // closed before the descent, one descriptor at a time
        const fd = openAnchored(anchor, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            const kind = kindAnchored(anchor, child);
            if (std.mem.eql(u8, kind, "symlink")) continue;
            if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
                continue;
            }
            if (keep_ready and std.mem.startsWith(u8, item.name, ".") and std.mem.endsWith(u8, item.name, ".ebsr-ready")) {
                try found.append(a, child);
            } else if (!std.mem.endsWith(u8, item.name, ".ebsr-ready")) {
                for (private_suffixes) |suffix| {
                    if (std.mem.endsWith(u8, item.name, suffix)) {
                        try discardPath(a, anchor, child, why);
                        break;
                    }
                }
            }
        }
    }
    for (subdirs.items) |dir| try sweepReady(a, anchor, dir, found, keep_ready, why);
}

fn namesOnInode(a: Allocator, anchor: Anchor, root: []const u8, inode: u64, ready: []const u8, names: *std.ArrayList([]const u8), why: *[]const u8) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    {
        // closed before the descent, one descriptor at a time
        const fd = openAnchored(anchor, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch |err| return fail(a, why, err, root);
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch |err| return fail(a, why, err, root);
        defer it.deinit(a);
        while (true) {
            const entry = it.next() catch |err| return fail(a, why, err, root);
            const item = entry orelse break;
            if (ours(item.name)) continue;
            const child = try std.fs.path.join(a, &.{ root, item.name });
            if (std.mem.eql(u8, child, ready)) continue;
            const kind = kindAnchored(anchor, child);
            if (std.mem.eql(u8, kind, "symlink")) continue;
            if (std.mem.eql(u8, kind, "dir")) {
                try subdirs.append(a, child);
                continue;
            }
            if (!std.mem.eql(u8, kind, "file")) continue;
            const info = statAnchored(anchor, child, false) catch continue;
            if (info.isReg() and info.ino == inode) try names.append(a, child);
        }
    }
    for (subdirs.items) |dir| try namesOnInode(a, anchor, dir, inode, ready, names, why);
}

fn rewriteAnchored(a: Allocator, anchor: Anchor, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    if (std.mem.eql(u8, old, new)) return;
    try recoverTree(a, anchor, why);
    var groups: std.ArrayList(Group) = .empty;
    var links: std.ArrayList([]const u8) = .empty;
    try collect(a, anchor, anchor.path, &groups, &links, why);
    for (groups.items) |group| {
        if (group.nlink != group.names.items.len) {
            why.* = try a.print("{s} has {d} hard links and this tree holds {d}; refusing to split them", .{ group.names.items[0], group.nlink, group.names.items.len });
            return error.Refused;
        }
        const fd = openAnchored(anchor, group.names.items[0], .{ .ACCMODE = .RDONLY, .NOFOLLOW = true, .NONBLOCK = true }, 0) catch |err| return fail(a, why, err, group.names.items[0]);
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
        try publish(a, anchor, group.names.items, updated, why);
    }
    for (links.items) |link| if (std.mem.eql(u8, kindAnchored(anchor, link), "symlink")) try rewriteSymlink(a, anchor, link, old, new, why);
}

fn rewriteTree(a: Allocator, root: []const u8, old: []const u8, new: []const u8, why: *[]const u8) Error!void {
    if (std.mem.eql(u8, old, new)) return;
    const fd = fsx.openDir(fsx.cwd, root, false) catch |err| return fail(a, why, err, root);
    defer sys.close(fd);
    try rewriteAnchored(a, .{ .fd = fd, .path = root }, old, new, why);
}

/// rewrite_install_prefix: files under `staged` name `live`'s prefix.
pub fn rewriteInstall(a: Allocator, staged: []const u8, live: []const u8, why: *[]const u8) Error!void {
    const pair = try prefixPair(a, staged, live, why);
    try rewriteTree(a, staged, pair.old, pair.new, why);
}

/// Same rewrite, starting from `dir`, which the caller opened with
/// `O_DIRECTORY` and `O_NOFOLLOW` on the final component of `staged`.
pub fn rewriteInstallPinned(a: Allocator, dir: sys.fd_t, staged: []const u8, live: []const u8, why: *[]const u8) Error!void {
    const pair = try prefixPair(a, staged, live, why);
    try rewriteAnchored(a, .{ .fd = dir, .path = staged }, pair.old, pair.new, why);
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

fn readOnlyChmod(_: sys.fd_t, _: u32) usize {
    return @as(usize, @bitCast(@as(isize, -@as(isize, @backingInt(linux.E.ROFS)))));
}

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
    atomic.ops.fchmod = readOnlyChmod;
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
    const root_fd = try fsx.openDir(fsx.cwd, base, false);
    defer sys.close(root_fd);
    collect(a, .{ .fd = root_fd, .path = base }, base, &groups, &links, &why) catch |err| {
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
