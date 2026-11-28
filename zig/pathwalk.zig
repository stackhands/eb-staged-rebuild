//! Open and classify a path from a directory descriptor, one component at a
//! time. A path longer than `PATH_MAX` still names a real directory, file,
//! or symlink. A component that cannot be classified is an error. It is
//! never reported as absent.
const std = @import("std");
const common = @import("zig_common");

const sys = common.sys;
const fsx = common.fsx;

pub const OpenStop = union(enum) {
    fd: sys.fd_t,
    /// Opening the start directory failed.
    root: sys.Error,
    outside,
    /// `parent` is still open. `comp` and `here` point into the caller's path.
    blocked: struct {
        parent: sys.fd_t,
        comp: []const u8,
        here: []const u8,
        err: sys.Error,
    },
};

pub const Kind = enum { missing, symlink, dir, file, other };

pub const Classified = union(enum) {
    kind: Kind,
    /// `here` points into the path that was classified.
    failed: struct { here: []const u8, err: sys.Error },
};

const Walk = struct {
    /// Follow every component, including the last.
    follow_all: bool = false,
    /// Follow a component only while its parent is strictly above this
    /// prefix. `..` is `outside`. Null follows intermediates, or every
    /// component when `follow_all` is set, and opens `..`.
    protected: ?[]const u8 = null,
};

/// Follow the next component when `parent` is still strictly above `protected`.
fn followComponent(parent: []const u8, protected: []const u8) bool {
    const prot = std.mem.trimEnd(u8, protected, "/");
    if (prot.len <= 1) return false;
    const above = if (parent.len == 0) "/" else parent;
    if (!(prot.len > above.len and std.mem.startsWith(u8, prot, above) and (above.len == 1 or prot[above.len] == '/')))
        return false;
    const rest = if (above.len == 1 and above[0] == '/') prot[1..] else prot[above.len + 1 ..];
    return std.mem.indexOfScalar(u8, rest, '/') != null;
}

fn startDir(absolute: bool) sys.Error!sys.fd_t {
    return fsx.openDir(fsx.cwd, if (absolute) "/" else ".", false);
}

fn walk(path: []const u8, mode: Walk) OpenStop {
    if (path.len == 0) return .{ .root = error.NotFound };
    const absolute = path[0] == '/';
    var fd = startDir(absolute) catch |err| return .{ .root = err };
    var i: usize = if (absolute) 1 else 0;
    while (i < path.len) {
        while (i < path.len and path[i] == '/') i += 1;
        if (i >= path.len) break;
        const start = i;
        while (i < path.len and path[i] != '/') i += 1;
        const comp = path[start..i];
        if (comp.len == 0 or std.mem.eql(u8, comp, ".")) continue;
        var j = i;
        while (j < path.len and path[j] == '/') j += 1;
        const last = j >= path.len;
        const parent = if (start <= 1) (if (absolute) "/" else "") else path[0 .. start - 1];
        const here = path[0..i];
        if (std.mem.eql(u8, comp, "..") and mode.protected != null) {
            sys.close(fd);
            return .outside;
        }
        const follow = if (mode.protected) |prot|
            followComponent(if (parent.len == 0) "/" else parent, prot)
        else
            mode.follow_all or !last;
        const next = fsx.openDir(fd, comp, follow) catch |err| {
            return .{ .blocked = .{ .parent = fd, .comp = comp, .here = here, .err = err } };
        };
        sys.close(fd);
        fd = next;
    }
    return .{ .fd = fd };
}

/// Open `path` from `/`, one component at a time. A component is followed
/// while its parent is strictly above `protected`. The next directory is
/// opened before the parent is closed, so two descriptors are open across
/// that call. `..` is `outside`.
pub fn openProtected(path: []const u8, protected: []const u8) OpenStop {
    if (path.len == 0 or std.mem.eql(u8, path, "/")) {
        return .{ .fd = startDir(true) catch |err| return .{ .root = err } };
    }
    return walk(path, .{ .protected = protected });
}

/// Open `path`, following every component, including a final symlink.
pub fn openFollow(path: []const u8) OpenStop {
    if (path.len == 0) return .{ .root = error.NotFound };
    if (std.mem.eql(u8, path, "/")) {
        return .{ .fd = startDir(true) catch |err| return .{ .root = err } };
    }
    return walk(path, .{ .follow_all = true });
}

fn unwrap(opened: OpenStop) sys.Error!sys.fd_t {
    switch (opened) {
        .fd => |fd| return fd,
        .root => |err| return err,
        .outside => return error.NotDir,
        .blocked => |b| {
            sys.close(b.parent);
            return b.err;
        },
    }
}

/// Open the directory `path`. `follow_last` follows a final symlink.
/// Intermediate components are followed. The descriptor is new.
pub fn openDir(path: []const u8, follow_last: bool) sys.Error!sys.fd_t {
    if (follow_last) return unwrap(openFollow(path));
    if (path.len == 0 or std.mem.eql(u8, path, "/")) return startDir(path.len != 0 and path[0] == '/');
    return unwrap(walk(path, .{}));
}

/// Open the file `path`. `follow` follows a final symlink. The parent
/// directory is opened by components, then the file name.
pub fn openFile(path: []const u8, follow: bool) sys.Error!struct { fd: sys.fd_t, stat: fsx.Stat } {
    const slash = std.mem.lastIndexOfScalar(u8, path, '/');
    if (slash == null) {
        const opened = try fsx.openRegular(fsx.cwd, path, follow);
        return .{ .fd = opened.fd, .stat = opened.stat };
    }
    const parent = if (slash.? == 0) "/" else path[0..slash.?];
    const name = path[slash.? + 1 ..];
    if (name.len == 0) return error.NotFound;
    const fd = try unwrap(openFollow(parent));
    defer sys.close(fd);
    const opened = try fsx.openRegular(fd, name, follow);
    return .{ .fd = opened.fd, .stat = opened.stat };
}

fn kindOf(st: fsx.Stat) Kind {
    if (st.isLink()) return .symlink;
    if (st.isDir()) return .dir;
    if (st.isReg()) return .file;
    return .other;
}

fn statName(dir: sys.fd_t, name: []const u8, here: []const u8) Classified {
    const st = fsx.statAt(dir, name, false) catch |err| {
        if (err == error.NotFound) return .{ .kind = .missing };
        return .{ .failed = .{ .here = here, .err = err } };
    };
    return .{ .kind = kindOf(st) };
}

fn blockedKind(parent: sys.fd_t, comp: []const u8, here: []const u8, err: sys.Error) Classified {
    if (err == error.NotFound) {
        sys.close(parent);
        return .{ .kind = .missing };
    }
    const st = fsx.statAt(parent, comp, false) catch |sterr| {
        sys.close(parent);
        if (sterr == error.NotFound) return .{ .kind = .missing };
        return .{ .failed = .{ .here = here, .err = sterr } };
    };
    sys.close(parent);
    // A file, fifo, socket, or a symlink to one of those ends the path.
    // The final name is absent. A directory that would not open stays an
    // error, as does a stat that failed above.
    if (err == error.NotDir and !st.isDir()) return .{ .kind = .missing };
    return .{ .failed = .{ .here = here, .err = err } };
}

/// Classify `path` the way `lstat` does. Intermediate components are
/// followed. The final component is not. `NotFound` is `.missing`. Any
/// other failure to stat a component is `.failed`.
pub fn classify(path: []const u8) Classified {
    const trimmed = std.mem.trimEnd(u8, path, "/");
    if (trimmed.len == 0) {
        if (path.len > 0 and path[0] == '/') {
            const fd = startDir(true) catch |err| return .{ .failed = .{ .here = "/", .err = err } };
            defer sys.close(fd);
            const st = fsx.fstat(fd) catch |err| return .{ .failed = .{ .here = "/", .err = err } };
            return .{ .kind = kindOf(st) };
        }
        return .{ .kind = .missing };
    }
    const slash = std.mem.lastIndexOfScalar(u8, trimmed, '/');
    if (slash == null) return statName(fsx.cwd, trimmed, trimmed);
    const parent = if (slash.? == 0) "/" else trimmed[0..slash.?];
    const name = trimmed[slash.? + 1 ..];
    switch (openFollow(parent)) {
        .fd => |fd| {
            defer sys.close(fd);
            return statName(fd, name, trimmed);
        },
        .root => |err| return .{ .failed = .{ .here = parent, .err = err } },
        .outside => return .{ .failed = .{ .here = parent, .err = error.NotDir } },
        .blocked => |b| return blockedKind(b.parent, b.comp, b.here, b.err),
    }
}

/// Classify `rel` under `root`. No component of `rel` is followed. An
/// absent component is `.missing`. A stat that cannot classify a component
/// is `.failed`.
pub fn classifyUnder(root: []const u8, rel: []const u8) Classified {
    const root_fd = switch (openProtected(root, root)) {
        .fd => |fd| fd,
        .root => |err| return .{ .failed = .{ .here = root, .err = err } },
        .outside => return .{ .failed = .{ .here = root, .err = error.NotDir } },
        .blocked => |b| return blockedKind(b.parent, b.comp, b.here, b.err),
    };
    var fd = root_fd;
    defer sys.close(fd);
    if (rel.len == 0) {
        const st = fsx.fstat(fd) catch |err| return .{ .failed = .{ .here = root, .err = err } };
        return .{ .kind = kindOf(st) };
    }
    var i: usize = 0;
    while (i < rel.len) {
        while (i < rel.len and rel[i] == '/') i += 1;
        if (i >= rel.len) break;
        const start = i;
        while (i < rel.len and rel[i] != '/') i += 1;
        const comp = rel[start..i];
        if (comp.len == 0 or std.mem.eql(u8, comp, ".")) continue;
        if (std.mem.eql(u8, comp, "..")) return .{ .failed = .{ .here = rel, .err = error.NotDir } };
        const st = fsx.statAt(fd, comp, false) catch |err| {
            if (err == error.NotFound) return .{ .kind = .missing };
            return .{ .failed = .{ .here = rel, .err = err } };
        };
        const k = kindOf(st);
        if (k != .dir) return .{ .kind = k };
        var j = i;
        while (j < rel.len and rel[j] == '/') j += 1;
        if (j >= rel.len) return .{ .kind = k };
        const next = fsx.openDir(fd, comp, false) catch |err| {
            if (err == error.NotFound) return .{ .kind = .missing };
            return .{ .failed = .{ .here = rel, .err = err } };
        };
        sys.close(fd);
        fd = next;
    }
    const st = fsx.fstat(fd) catch |err| return .{ .failed = .{ .here = root, .err = err } };
    return .{ .kind = kindOf(st) };
}
