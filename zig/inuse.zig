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

fn readlink(a: Allocator, path: []const u8, limit: usize) error{NameTooLong}!?[]const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var storage: [sys.path_max]u8 = undefined;
    const buf = storage[0..@min(limit, storage.len)];
    const z = sys.z(&zb, path) catch return null;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, buf.ptr, buf.len }) catch |err| switch (err) {
        error.NameTooLong => return error.NameTooLong,
        else => return null,
    };
    // A full buffer is a truncated path. The bytes are a prefix of the
    // target, so a tree check can still match them. The kernel returns
    // NameTooLong, and no bytes, when the path or the path plus
    // " (deleted)" does not fit in one page.
    return a.dupe(u8, buf[0..n]) catch oom();
}

const Id = struct { dev: u64, ino: u64 };

fn identity(path: []const u8) ?Id {
    const st = fsx.statAt(fsx.cwd, path, true) catch return null;
    return .{ .dev = st.dev, .ino = st.ino };
}

/// True when the open directory `dir` is `id` or a directory inside it.
/// Closes `dir`.
fn insideDir(id: Id, dir_fd: linux.fd_t) bool {
    var dir = dir_fd;
    var steps: usize = 0;
    while (steps < sys.path_max) : (steps += 1) {
        const st = fsx.fstat(dir) catch break;
        if (st.dev == id.dev and st.ino == id.ino) {
            sys.close(dir);
            return true;
        }
        const parent = sys.openat(dir, "..", .{ .PATH = true, .DIRECTORY = true }, 0) catch break;
        const up = fsx.fstat(parent) catch {
            sys.close(parent);
            break;
        };
        if (up.dev == st.dev and up.ino == st.ino) {
            sys.close(parent);
            break;
        }
        sys.close(dir);
        dir = parent;
    }
    sys.close(dir);
    return false;
}

/// A directory under `root` that lists inode `ino` on device `dev`. The
/// caller closes it. `openat` of ".." fails with ENOTDIR when the
/// descriptor is a file, so the directory that holds the file is found by
/// listing `root`. The listing is a readable descriptor: getdents rejects
/// O_PATH. Symbolic links are not followed, and a directory on another
/// device is not entered.
fn holding(root: []const u8, dev: u64, ino: u64) ?linux.fd_t {
    const start = sys.openat(fsx.cwd, root, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch return null;
    return holdingAt(start, dev, ino, 0);
}

fn holdingAt(dir: linux.fd_t, dev: u64, ino: u64, depth: usize) ?linux.fd_t {
    if (depth >= sys.path_max) {
        sys.close(dir);
        return null;
    }
    const buf = std.heap.page_allocator.alloc(u8, 8192) catch {
        sys.close(dir);
        oom();
    };
    defer std.heap.page_allocator.free(buf);
    while (true) {
        const n = sys.call(linux.getdents64, .{ dir, buf.ptr, @as(c_uint, @intCast(buf.len)) }) catch {
            sys.close(dir);
            return null;
        };
        if (n == 0) break;
        var off: usize = 0;
        while (off < n) {
            const ent: *align(1) const linux.dirent64 = @ptrCast(buf[off..].ptr);
            const reclen: usize = ent.reclen;
            if (reclen == 0 or off + reclen > n) break;
            const name_ptr: [*:0]const u8 = @ptrCast(buf[off + @offsetOf(linux.dirent64, "name") ..].ptr);
            const name = std.mem.span(name_ptr);
            off += reclen;
            if (name.len == 0 or std.mem.eql(u8, name, ".") or std.mem.eql(u8, name, "..")) continue;
            if (ent.ino == ino) {
                if (fsx.statAt(dir, name, false)) |st| {
                    if (st.dev == dev and st.ino == ino and !st.isDir()) return dir;
                } else |_| {}
            }
            const kind = ent.type;
            if (kind != linux.DT.DIR and kind != linux.DT.UNKNOWN) continue;
            const st = fsx.statAt(dir, name, false) catch continue;
            if (!st.isDir() or st.isLink() or st.dev != dev) continue;
            const child = sys.openat(dir, name, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0) catch continue;
            if (holdingAt(child, dev, ino, depth + 1)) |found| {
                sys.close(dir);
                return found;
            }
        }
    }
    sys.close(dir);
    return null;
}

/// True when `link` is a directory at `id` or inside it, or a file held in
/// such a directory. Used when readlink(2) will not return the path. `root`
/// is the tree `id` names.
fn inside(id: Id, root: []const u8, link: []const u8) bool {
    const fd = sys.openat(fsx.cwd, link, .{ .PATH = true }, 0) catch return false;
    const st = fsx.fstat(fd) catch {
        sys.close(fd);
        return false;
    };
    if (st.isDir()) return insideDir(id, fd);
    if (sys.openat(fd, "..", .{ .PATH = true, .DIRECTORY = true }, 0)) |parent| {
        sys.close(fd);
        return insideDir(id, parent);
    } else |_| {}
    const dev = st.dev;
    const ino = st.ino;
    sys.close(fd);
    if (dev != id.dev) return false;
    const parent = holding(root, dev, ino) orelse return false;
    return insideDir(id, parent);
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
    ids: []const ?Id,

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

    fn linkUses(s: *Scan, t: Allocator, pid: u64, link: []const u8, what: []const u8, limit: usize) void {
        if (readlink(t, link, limit)) |path| {
            // A filled buffer is a prefix of the path, so it is matched here.
            // The inode walk below runs only for a real ENAMETOOLONG.
            s.uses(pid, path, what);
        } else |err| switch (err) {
            error.NameTooLong => for (s.ids, 0..) |id, i| {
                if (s.found[i] != null) continue;
                if (id) |held| if (inside(held, s.real[i], link)) s.note(i, "pid {d} {s}", .{ pid, what });
            },
        }
    }
};

/// For each tree, the first use of it this node shows, or null. `a` holds
/// the answers; each process is read into an arena on `scratch`, reset
/// before the next.
pub fn users(a: Allocator, scratch: Allocator, trees: []const []const u8) []?[]const u8 {
    return usersLimit(a, scratch, trees, sys.path_max);
}

fn usersLimit(a: Allocator, scratch: Allocator, trees: []const []const u8, limit: usize) []?[]const u8 {
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
    const ids = a.alloc(?Id, trees.len) catch oom();
    for (real, ids) |r, *id| id.* = identity(r);
    var scan: Scan = .{ .a = a, .found = found, .real = real, .held = held, .ids = ids };
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
        for (links) |l| scan.linkUses(t, pid, std.mem.concat(t, u8, &.{ base, l.name }) catch oom(), l.what, limit);
        const fds = std.mem.concat(t, u8, &.{ base, "fd" }) catch oom();
        for (numbers(t, fds)) |fd| {
            scan.linkUses(t, pid, t.print("{s}/{d}", .{ fds, fd }) catch oom(), "has a file open in it", limit);
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

/// Python, whose working directory is inside the tree and too long for
/// readlink(2). A shell's `cd` refuses that path. It writes "ready" and waits.
const long_cwd_script =
    \\import errno, os, sys, time
    \\os.chdir(sys.argv[1])
    \\name = "d" * 180
    \\for _ in range(25):
    \\    os.mkdir(name)
    \\    os.chdir(name)
    \\try:
    \\    os.readlink("/proc/self/cwd")
    \\except OSError as err:
    \\    if err.errno != errno.ENAMETOOLONG:
    \\        raise
    \\else:
    \\    sys.exit(2)
    \\sys.stdout.write("ready\n")
    \\sys.stdout.flush()
    \\time.sleep(60)
;

fn holdLongCwd(tree: [*:0]const u8, wr: linux.fd_t, rd: linux.fd_t) noreturn {
    _ = linux.dup2(wr, 1);
    _ = linux.close(wr);
    _ = linux.close(rd);
    const argv = [_:null]?[*:0]const u8{ "python3", "-c", long_cwd_script, tree };
    const envp = [_:null]?[*:0]const u8{"PATH=/usr/bin:/bin"};
    _ = linux.execve("/usr/bin/python3", &argv, &envp);
    linux.exit_group(127);
}

/// Python opens a regular file under the first tree and execs a binary under
/// the second, both longer than one page, then leaves the working directory
/// at `/`. `sys.argv[3]` is the execveat syscall number. It writes "ready"
/// before the exec.
const long_file_script =
    \\import ctypes, errno, os, shutil, sys
    \\nr = int(sys.argv[3])
    \\name = "d" * 180
    \\def deep(root):
    \\    os.chdir(root)
    \\    for _ in range(25):
    \\        os.mkdir(name)
    \\        os.chdir(name)
    \\    try:
    \\        os.readlink("/proc/self/cwd")
    \\    except OSError as err:
    \\        if err.errno != errno.ENAMETOOLONG:
    \\            raise
    \\    else:
    \\        sys.exit(2)
    \\deep(sys.argv[1])
    \\fd = os.open("held", os.O_CREAT | os.O_RDWR, 0o644)
    \\os.set_inheritable(fd, True)
    \\os.write(fd, b"x")
    \\deep(sys.argv[2])
    \\shutil.copy("/bin/sleep", "heldbin")
    \\os.chmod("heldbin", 0o755)
    \\binfd = os.open("heldbin", os.O_PATH | os.O_CLOEXEC)
    \\os.chdir("/")
    \\try:
    \\    os.readlink("/proc/self/fd/%d" % fd)
    \\except OSError as err:
    \\    if err.errno != errno.ENAMETOOLONG:
    \\        raise
    \\else:
    \\    sys.exit(3)
    \\if os.readlink("/proc/self/cwd") != "/":
    \\    sys.exit(4)
    \\sys.stdout.write("ready\n")
    \\sys.stdout.flush()
    \\libc = ctypes.CDLL(None, use_errno=True)
    \\libc.syscall.restype = ctypes.c_long
    \\argv = (ctypes.c_char_p * 3)(b"heldbin", b"60", None)
    \\envp = (ctypes.c_char_p * 2)(b"PATH=/usr/bin:/bin", None)
    \\libc.syscall(nr, binfd, b"", argv, envp, 0x1000)
    \\sys.exit(127)
;

fn holdLongFile(file_tree: [*:0]const u8, exe_tree: [*:0]const u8, nr: [*:0]const u8, wr: linux.fd_t, rd: linux.fd_t) noreturn {
    _ = linux.dup2(wr, 1);
    _ = linux.close(wr);
    _ = linux.close(rd);
    const argv = [_:null]?[*:0]const u8{ "python3", "-c", long_file_script, file_tree, exe_tree, nr };
    const envp = [_:null]?[*:0]const u8{"PATH=/usr/bin:/bin"};
    _ = linux.execve("/usr/bin/python3", &argv, &envp);
    linux.exit_group(127);
}

fn procLinkTooLong(pid: linux.pid_t, what: []const u8) bool {
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrint(&path_buf, "/proc/{d}/{s}", .{ pid, what }) catch return false;
    var zb: [sys.path_max:0]u8 = undefined;
    var storage: [8]u8 = undefined;
    const z = sys.z(&zb, path) catch return false;
    _ = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &storage, storage.len }) catch |err| switch (err) {
        error.NameTooLong => return true,
        else => return false,
    };
    return false;
}

test "a working directory longer than one memory page still marks its tree in use" {
    const gpa = std.testing.allocator;
    const tree = try fsx.mkdtemp(gpa, "/tmp", "inuse-long-");
    const other = try fsx.mkdtemp(gpa, "/tmp", "inuse-free-");
    var child: linux.pid_t = 0;
    defer {
        if (child > 0) {
            _ = linux.kill(child, .KILL);
            var st: i32 = 0;
            _ = sys.call(linux.waitpid, .{ child, &st, @as(u32, 0) }) catch {};
        }
        fsx.deleteTree(gpa, fsx.cwd, tree) catch {};
        fsx.deleteTree(gpa, fsx.cwd, other) catch {};
        gpa.free(tree);
        gpa.free(other);
    }
    var ends: [2]linux.fd_t = undefined;
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.pipe2(&ends, .{ .CLOEXEC = true })));
    defer sys.close(ends[0]);
    defer sys.close(ends[1]);
    var tree_z: [sys.path_max:0]u8 = undefined;
    const raw = linux.fork();
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(raw));
    if (raw == 0) holdLongCwd((try sys.z(&tree_z, tree)).ptr, ends[1], ends[0]);
    child = @intCast(raw);
    sys.close(ends[1]);
    ends[1] = -1;
    var got: [6]u8 = undefined;
    var n: usize = 0;
    while (n < got.len) {
        const k = try sys.read(ends[0], got[n..]);
        if (k == 0) return error.TestUnexpectedResult;
        n += k;
    }
    try std.testing.expectEqualStrings("ready\n", &got);
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const found = users(arena.allocator(), arena.allocator(), &.{ tree, other });
    const why = found[0] orelse return error.TestExpectedEqual;
    try std.testing.expect(std.mem.indexOf(u8, why, "working directory") != null);
    try std.testing.expect(found[1] == null);
}

test "an open file and an executable longer than one memory page still mark their trees in use" {
    const gpa = std.testing.allocator;
    const file_tree = try fsx.mkdtemp(gpa, "/tmp", "inuse-held-");
    const exe_tree = try fsx.mkdtemp(gpa, "/tmp", "inuse-exe-");
    const other = try fsx.mkdtemp(gpa, "/tmp", "inuse-none-");
    var child: linux.pid_t = 0;
    defer {
        if (child > 0) {
            _ = linux.kill(child, .KILL);
            var st: i32 = 0;
            _ = sys.call(linux.waitpid, .{ child, &st, @as(u32, 0) }) catch {};
        }
        fsx.deleteTree(gpa, fsx.cwd, file_tree) catch {};
        fsx.deleteTree(gpa, fsx.cwd, exe_tree) catch {};
        fsx.deleteTree(gpa, fsx.cwd, other) catch {};
        gpa.free(file_tree);
        gpa.free(exe_tree);
        gpa.free(other);
    }
    var ends: [2]linux.fd_t = undefined;
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.pipe2(&ends, .{ .CLOEXEC = true })));
    defer sys.close(ends[0]);
    defer sys.close(ends[1]);
    var nr_buf: [16]u8 = undefined;
    const nr = std.mem.printSentinel(&nr_buf, "{d}", .{@backingInt(linux.SYS.execveat)}, 0) catch unreachable;
    var file_z: [sys.path_max:0]u8 = undefined;
    var exe_z: [sys.path_max:0]u8 = undefined;
    const raw = linux.fork();
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(raw));
    if (raw == 0) holdLongFile((try sys.z(&file_z, file_tree)).ptr, (try sys.z(&exe_z, exe_tree)).ptr, nr.ptr, ends[1], ends[0]);
    child = @intCast(raw);
    sys.close(ends[1]);
    ends[1] = -1;
    var got: [6]u8 = undefined;
    var n: usize = 0;
    while (n < got.len) {
        const k = try sys.read(ends[0], got[n..]);
        if (k == 0) return error.TestUnexpectedResult;
        n += k;
    }
    try std.testing.expectEqualStrings("ready\n", &got);
    // The working directory is already `/`. Wait until the exec'd binary's
    // path is the long one, so the scan is not looking at Python.
    var spins: usize = 0;
    while (!procLinkTooLong(child, "exe")) : (spins += 1) {
        var st: i32 = 0;
        const wr = try sys.call(linux.waitpid, .{ child, &st, linux.W.NOHANG });
        if (wr != 0) return error.TestUnexpectedResult;
        if (spins == 1000000) return error.TestUnexpectedResult;
    }
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const found = users(arena.allocator(), arena.allocator(), &.{ file_tree, exe_tree, other });
    const file_why = found[0] orelse return error.TestExpectedEqual;
    try std.testing.expect(std.mem.indexOf(u8, file_why, "has a file open in it") != null);
    const exe_why = found[1] orelse return error.TestExpectedEqual;
    try std.testing.expect(std.mem.indexOf(u8, exe_why, "runs a program in it") != null);
    try std.testing.expect(found[2] == null);
}

test "a truncated path to an open file marks that tree and leaves another unused" {
    const gpa = std.testing.allocator;
    const tree = try fsx.mkdtemp(gpa, "/tmp", "inuse-file-");
    const other = try fsx.mkdtemp(gpa, "/tmp", "inuse-idle-");
    var leaf: [180]u8 = undefined;
    @memset(&leaf, 'f');
    const file = std.fmt.allocPrint(gpa, "{s}/{s}", .{ tree, &leaf }) catch oom();
    var opened: ?linux.fd_t = null;
    defer {
        if (opened) |fd| sys.close(fd);
        fsx.deleteTree(gpa, fsx.cwd, tree) catch {};
        fsx.deleteTree(gpa, fsx.cwd, other) catch {};
        gpa.free(file);
        gpa.free(tree);
        gpa.free(other);
    }
    opened = try sys.openat(fsx.cwd, file, .{ .ACCMODE = .RDONLY, .CREAT = true, .EXCL = true }, 0o644);
    // Shorter than the file, longer than the tree: readlink fills this
    // buffer and the bytes still begin with the tree.
    const limit: usize = 64;
    try std.testing.expect(tree.len < limit and file.len > limit);
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const found = usersLimit(arena.allocator(), arena.allocator(), &.{ tree, other }, limit);
    const why = found[0] orelse return error.TestExpectedEqual;
    try std.testing.expect(std.mem.indexOf(u8, why, "has a file open in it") != null);
    try std.testing.expect(found[1] == null);
}
