//! A passthrough mount whose directory flush returns EIO.
//! The binary and the hook are the ones the prefix flush test uses.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");

const sys = common.sys;
const fsx = common.fsx;
const atomic = common.atomic;
const Allocator = std.mem.Allocator;

pub const Mount = struct {
    a: Allocator,
    work: []const u8,
    mnt: []const u8,
    pid: linux.pid_t,

    pub fn stop(self: *Mount) void {
        const umount = [_][]const u8{ "/usr/bin/fusermount3", "-u", self.mnt };
        _ = exec(self.a, &umount, baseEnv(self.a) catch return, "/dev/null") catch {};
        var spins: usize = 0;
        while (spins < 2_000_000) : (spins += 1) {
            const gone = pollExit(self.pid) catch break;
            if (gone != null) break;
        } else {
            _ = linux.kill(self.pid, linux.SIG.KILL);
            var status: i32 = 0;
            _ = linux.waitpid(self.pid, &status, 0);
        }
        fsx.deleteTree(self.a, fsx.cwd, self.work) catch {};
    }
};

/// Mount a passthrough of a fresh backing directory. `fuse_dir` is the
/// mount-absolute path whose flush returns EIO, the same value
/// FAULTFS_FSYNC_DIR takes.
pub fn mount(a: Allocator, label: []const u8, fuse_dir: []const u8) !Mount {
    const work = try std.fmt.allocPrint(a, "/tmp/ebsr-fsync-{d}-{s}", .{ linux.getpid(), label });
    const back = try std.fs.path.join(a, &.{ work, "back" });
    const mnt_path = try std.fs.path.join(a, &.{ work, "mnt" });
    try atomic.makedirs(back);
    try atomic.makedirs(mnt_path);
    var rb: [sys.path_max]u8 = undefined;
    const mnt = try a.dupe(u8, try fsx.realpath(fsx.cwd, mnt_path, &rb));
    var bb: [sys.path_max]u8 = undefined;
    const backing = try a.dupe(u8, try fsx.realpath(fsx.cwd, back, &bb));
    const bin = try std.fs.path.join(a, &.{ work, "faultfs" });
    const src = try faultfsSource(a);
    const cc_err = try std.fs.path.join(a, &.{ work, "cc.err" });
    const cc_rc = try exec(a, &.{
        "/usr/bin/cc", "-O2",       "-Wall", "-Wextra",
        "-o",          bin,         src,     "-I/usr/include/fuse3",
        "-lfuse3",     "-lpthread",
    }, try baseEnv(a), cc_err);
    if (cc_rc != 0) {
        const detail = readSmall(a, cc_err);
        std.debug.print("faultfs compile failed:\n{s}\n", .{detail});
        fsx.deleteTree(a, fsx.cwd, work) catch {};
        return error.Compile;
    }
    const fuse_err = try std.fs.path.join(a, &.{ work, "fuse.err" });
    const child = try spawnFuse(a, bin, backing, mnt, fuse_dir, fuse_err);
    const info = try a.alloc(u8, 1 << 20);
    var spins: usize = 0;
    while (spins < 2_000_000) : (spins += 1) {
        if (pollExit(child)) |gone| {
            if (gone != null) {
                std.debug.print("faultfs exited:\n{s}\n", .{readSmall(a, fuse_err)});
                fsx.deleteTree(a, fsx.cwd, work) catch {};
                return error.FuseExited;
            }
        } else |_| {}
        if (mounted(mnt, info)) return .{ .a = a, .work = work, .mnt = mnt, .pid = child };
    }
    _ = linux.kill(child, linux.SIG.KILL);
    var status: i32 = 0;
    _ = linux.waitpid(child, &status, 0);
    std.debug.print("faultfs did not mount:\n{s}\n", .{readSmall(a, fuse_err)});
    fsx.deleteTree(a, fsx.cwd, work) catch {};
    return error.FuseTimeout;
}

fn faultfsSource(a: Allocator) ![]u8 {
    if (sys.openat(fsx.cwd, "tests/faultfs.c", .{ .ACCMODE = .RDONLY }, 0)) |fd| {
        sys.close(fd);
        return a.dupe(u8, "tests/faultfs.c");
    } else |_| {}
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = fsx.realpath(fsx.cwd, ".", &cwd_buf) catch ".";
    std.debug.print("tests/faultfs.c is not in {s}\n", .{here});
    return error.NoFaultfs;
}

fn baseEnv(a: Allocator) ![*:null]const ?[*:0]const u8 {
    const items = [_]?[*:0]const u8{
        try std.fmt.allocPrintSentinel(a, "PATH=/usr/bin:/bin", .{}, 0),
        try std.fmt.allocPrintSentinel(a, "HOME=/tmp", .{}, 0),
        null,
    };
    const copy = try a.dupe(?[*:0]const u8, &items);
    return @ptrCast(copy.ptr);
}

fn fuseEnv(a: Allocator, fuse_dir: []const u8) ![*:null]const ?[*:0]const u8 {
    const items = [_]?[*:0]const u8{
        try std.fmt.allocPrintSentinel(a, "PATH=/usr/bin:/bin", .{}, 0),
        try std.fmt.allocPrintSentinel(a, "HOME=/tmp", .{}, 0),
        try std.fmt.allocPrintSentinel(a, "FAULTFS_FSYNC_DIR={s}", .{fuse_dir}, 0),
        null,
    };
    const copy = try a.dupe(?[*:0]const u8, &items);
    return @ptrCast(copy.ptr);
}

fn redirectStdio(err_path: [*:0]const u8) void {
    const null_fd = linux.open("/dev/null", .{ .ACCMODE = .RDWR }, 0);
    if (linux.errno(null_fd) == .SUCCESS) {
        const fd: linux.fd_t = @intCast(null_fd);
        _ = linux.dup2(fd, 0);
        _ = linux.dup2(fd, 1);
        if (fd > 1) _ = linux.close(fd);
    }
    const err_fd = linux.open(err_path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o644);
    if (linux.errno(err_fd) == .SUCCESS) {
        const fd: linux.fd_t = @intCast(err_fd);
        _ = linux.dup2(fd, 2);
        if (fd != 2) _ = linux.close(fd);
    }
}

fn exec(a: Allocator, argv_in: []const []const u8, envp: [*:null]const ?[*:0]const u8, err_path: []const u8) !u8 {
    var argv_buf: [16]?[*:0]const u8 = undefined;
    if (argv_in.len + 1 > argv_buf.len) return error.TooManyArgs;
    for (argv_in, 0..) |arg, i| argv_buf[i] = try std.fmt.allocPrintSentinel(a, "{s}", .{arg}, 0);
    argv_buf[argv_in.len] = null;
    const err_z = try std.fmt.allocPrintSentinel(a, "{s}", .{err_path}, 0);
    const pid_rc = linux.fork();
    if (linux.errno(pid_rc) != .SUCCESS) return error.Fork;
    if (pid_rc == 0) {
        redirectStdio(err_z);
        _ = linux.execve(argv_buf[0].?, @ptrCast(&argv_buf), envp);
        linux.exit_group(127);
    }
    var status: i32 = 0;
    const wr = linux.waitpid(@intCast(pid_rc), &status, 0);
    if (linux.errno(wr) != .SUCCESS) return error.Wait;
    return exitStatus(status);
}

fn spawnFuse(a: Allocator, bin: []const u8, backing: []const u8, mnt: []const u8, fuse_dir: []const u8, err_path: []const u8) !linux.pid_t {
    const argv_buf = [_:null]?[*:0]const u8{
        try std.fmt.allocPrintSentinel(a, "{s}", .{bin}, 0),
        try std.fmt.allocPrintSentinel(a, "{s}", .{backing}, 0),
        try std.fmt.allocPrintSentinel(a, "{s}", .{mnt}, 0),
    };
    const err_z = try std.fmt.allocPrintSentinel(a, "{s}", .{err_path}, 0);
    const envp = try fuseEnv(a, fuse_dir);
    const pid_rc = linux.fork();
    if (linux.errno(pid_rc) != .SUCCESS) return error.Fork;
    if (pid_rc == 0) {
        redirectStdio(err_z);
        _ = linux.execve(argv_buf[0].?, &argv_buf, envp);
        linux.exit_group(127);
    }
    return @intCast(pid_rc);
}

fn pollExit(pid: linux.pid_t) !?u8 {
    var status: i32 = 0;
    const wr = linux.waitpid(pid, &status, linux.W.NOHANG);
    if (wr == 0) return null;
    if (linux.errno(wr) != .SUCCESS) return error.Wait;
    return try exitStatus(status);
}

fn exitStatus(status: i32) !u8 {
    const bits: u32 = @bitCast(status);
    if (bits & 0x7f != 0) return error.Signaled;
    return @intCast((bits >> 8) & 0xff);
}

fn mounted(path: []const u8, buf: []u8) bool {
    const fd = sys.openat(fsx.cwd, "/proc/self/mountinfo", .{ .ACCMODE = .RDONLY }, 0) catch return false;
    defer sys.close(fd);
    // procfs hands out mountinfo about one page per read, so read to EOF.
    var n: usize = 0;
    while (n < buf.len) {
        const got = linux.read(fd, buf.ptr + n, buf.len - n);
        if (linux.errno(got) == .INTR) continue;
        if (linux.errno(got) != .SUCCESS) return false;
        if (got == 0) break;
        n += got;
    }
    if (n == 0) return false;
    var needle: [4096]u8 = undefined;
    const text = std.fmt.bufPrint(&needle, " {s} ", .{path}) catch return false;
    return std.mem.indexOf(u8, buf[0..n], text) != null;
}

fn readSmall(a: Allocator, path: []const u8) []u8 {
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .RDONLY }, 0) catch return "";
    defer sys.close(fd);
    return fsx.readAll(a, fd, 1 << 16) catch "";
}
