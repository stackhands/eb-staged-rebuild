//! Find the module files EasyBuild wrote and decide which install directory
//! each names.
const std = @import("std");
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const modulefile = @import("modulefile.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const Allocator = std.mem.Allocator;

/// Refused, with the reason in the caller's `why`, or out of memory.
pub const Error = error{Refused} || Allocator.Error;

/// A symbolic link in the module tree: its path relative to the tree, and
/// its target as readlink gives it.
pub const Link = struct { rel: []const u8, target: []const u8 };

fn lessText(_: void, x: []const u8, y: []const u8) bool {
    return json.pyTextLess(x, y);
}

fn lessLink(_: void, x: Link, y: Link) bool {
    if (!std.mem.eql(u8, x.rel, y.rel)) return json.pyTextLess(x.rel, y.rel);
    return json.pyTextLess(x.target, y.target);
}

fn readlinkAt(a: Allocator, dir: sys.fd_t, name: []const u8) []const u8 {
    var name_buf: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = sys.z(&name_buf, name) catch return "";
    const n = sys.call(std.os.linux.readlinkat, .{ dir, z.ptr, &buf, buf.len }) catch return "";
    return a.dupe(u8, buf[0..n]) catch "";
}

fn unreadableTree(a: Allocator, root: []const u8, why: *[]const u8) Error {
    why.* = a.print("module tree is unreadable: {s}", .{root}) catch "module tree is unreadable";
    return error.Refused;
}

fn refuseNotDir(a: Allocator, path: []const u8, why: *[]const u8) Error {
    why.* = a.print("module tree is not a real directory: {s}", .{path}) catch "module tree is not a real directory";
    return error.Refused;
}

fn refuseUnclassified(a: Allocator, path: []const u8, err: sys.Error, why: *[]const u8) Error {
    why.* = a.print("module tree component cannot be classified: {s}: {s}", .{ path, main_mod.osError(a, err, path) }) catch "module tree component cannot be classified";
    return error.Refused;
}

/// Open `path` from `/`, one component at a time. Components above the last
/// are followed. The last component is opened with `O_NOFOLLOW`. A real
/// directory is returned open, whatever its length. A symlink, a plain file,
/// or a special file is refused. A stat that cannot classify a component is
/// refused. The caller closes the descriptor.
fn openModulesRoot(a: Allocator, path_in: []const u8, why: *[]const u8) Error!sys.fd_t {
    const abs = admin.abspath(a, path_in);
    const trimmed = std.mem.trimEnd(u8, abs, "/");
    const path = if (abs.len != 0 and trimmed.len == 0) "/" else trimmed;
    if (path.len == 0) return refuseNotDir(a, path_in, why);
    var fd = fsx.openDir(fsx.cwd, "/", false) catch |err| return refuseUnclassified(a, "/", err, why);
    if (std.mem.eql(u8, path, "/")) return fd;
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, path[1..], '/');
    while (it.next()) |part| {
        if (part.len == 0 or std.mem.eql(u8, part, ".")) continue;
        if (std.mem.eql(u8, part, "..")) {
            sys.close(fd);
            return refuseNotDir(a, path, why);
        }
        parts.append(a, part) catch {
            sys.close(fd);
            return error.OutOfMemory;
        };
    }
    var acc: []const u8 = "";
    for (parts.items, 0..) |part, i| {
        const last = i + 1 == parts.items.len;
        acc = std.fmt.allocPrint(a, "{s}/{s}", .{ acc, part }) catch {
            sys.close(fd);
            return error.OutOfMemory;
        };
        const next = fsx.openDir(fd, part, !last) catch {
            const st = fsx.statAt(fd, part, false) catch |sterr| {
                sys.close(fd);
                return refuseUnclassified(a, acc, sterr, why);
            };
            sys.close(fd);
            if (st.isLink() or !st.isDir()) return refuseNotDir(a, if (last) path else acc, why);
            return unreadableTree(a, path, why);
        };
        sys.close(fd);
        fd = next;
    }
    return fd;
}

fn walkOpened(
    a: Allocator,
    modules_root: []const u8,
    dir_path: []const u8,
    fd: sys.fd_t,
    files: *std.ArrayList([]const u8),
    links: *std.ArrayList(Link),
    why: *[]const u8,
) Error!void {
    var subdirs: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(a, fd) catch {
        sys.close(fd);
        return error.OutOfMemory;
    };
    defer it.deinit(a);
    while (it.next() catch {
        sys.close(fd);
        return unreadableTree(a, modules_root, why);
    }) |e| {
        if (e.name.len == 0) continue;
        const name = a.dupe(u8, e.name) catch {
            sys.close(fd);
            return error.OutOfMemory;
        };
        const child = std.fs.path.join(a, &.{ dir_path, name }) catch {
            sys.close(fd);
            return error.OutOfMemory;
        };
        const st = fsx.statAt(fd, name, false) catch |err| {
            sys.close(fd);
            return refuseUnclassified(a, child, err, why);
        };
        if (st.isLink()) {
            links.append(a, .{ .rel = modulefile.relpath(a, child, modules_root), .target = readlinkAt(a, fd, name) }) catch {
                sys.close(fd);
                return error.OutOfMemory;
            };
        } else if (st.isDir()) {
            subdirs.append(a, name) catch {
                sys.close(fd);
                return error.OutOfMemory;
            };
        } else if (st.isReg() and (std.mem.endsWith(u8, name, ".lua") or std.mem.endsWith(u8, name, ".tcl"))) {
            files.append(a, child) catch {
                sys.close(fd);
                return error.OutOfMemory;
            };
        }
    }
    sys.close(fd);
    for (subdirs.items) |name| {
        const child = try std.fs.path.join(a, &.{ dir_path, name });
        const child_fd = try openModulesRoot(a, child, why);
        try walkOpened(a, modules_root, child, child_fd, files, links, why);
    }
}

/// walk_module_tree: the regular .lua and .tcl files (sorted), and every
/// symbolic link as (relative path, link text); linked directories are not
/// entered. The root is opened one component at a time. Each child is
/// classified with `statAt` on the open parent descriptor. A stat that
/// cannot classify the child is refused.
pub fn walkModuleTree(a: Allocator, modules_root: []const u8, why: *[]const u8) Error!struct { files: []const []const u8, links: []const Link } {
    const root = admin.abspath(a, modules_root);
    const fd = try openModulesRoot(a, root, why);
    var files: std.ArrayList([]const u8) = .empty;
    var links: std.ArrayList(Link) = .empty;
    try walkOpened(a, root, root, fd, &files, &links, why);
    std.mem.sort([]const u8, files.items, {}, lessText);
    std.mem.sort(Link, links.items, {}, lessLink);
    return .{ .files = files.items, .links = links.items };
}

/// One installation a module file names: the staged tree, the stable live
/// tree, and the module text with its root set to the live tree.
pub const Install = struct { staged: []const u8, live: []const u8, text: []const u8 };

/// The staged path `swapInstallFor` would use, and the relative path under
/// the stage software tree. The directory does not have to exist.
pub const StagedPath = struct { staged: []const u8, relative: []const u8 };

/// The staged path named by `text`. A root under the stage software tree is
/// that path. A root under the live software tree is the same relative path
/// under the stage software tree.
pub fn stagedInstallPath(a: Allocator, text: []const u8, stage_software: []const u8, live_software: []const u8, why: *[]const u8) Error!StagedPath {
    const r = modulefile.parseRoot(text) orelse {
        why.* = "module file has no root assignment";
        return error.Refused;
    };
    if (r.path.len == 0) {
        why.* = "module file has no root assignment";
        return error.Refused;
    }
    const root_abs = admin.abspath(a, r.path);
    const stage_abs = admin.abspath(a, stage_software);
    const live_abs = admin.abspath(a, live_software);
    if (admin.isWithin(a, root_abs, stage_abs)) {
        return .{ .staged = root_abs, .relative = modulefile.relpath(a, root_abs, stage_abs) };
    }
    if (admin.isWithin(a, root_abs, live_abs)) {
        const relative = modulefile.relpath(a, root_abs, live_abs);
        const staged = if (std.mem.eql(u8, relative, ".")) stage_abs else try std.fs.path.join(a, &.{ stage_abs, relative });
        return .{ .staged = staged, .relative = relative };
    }
    why.* = try a.print("module root {s} is outside both the stage software tree {s} and the live software tree {s}", .{ root_abs, stage_abs, live_abs });
    return error.Refused;
}

/// swap_install_for: the staged tree, the stable live tree, and the module
/// text pointing at the live tree.
pub fn swapInstallFor(a: Allocator, text: []const u8, stage_software: []const u8, live_software: []const u8, why: *[]const u8) Error!Install {
    const found = try stagedInstallPath(a, text, stage_software, live_software, why);
    const staged = found.staged;
    const relative = found.relative;
    const live_abs = admin.abspath(a, live_software);
    const live = if (std.mem.eql(u8, relative, ".")) live_abs else try std.fs.path.join(a, &.{ live_abs, relative });
    if (!std.mem.eql(u8, main_mod.lstatKind(staged), "dir")) {
        why.* = try a.print("staged installation is not a real directory: {s}", .{staged});
        return error.Refused;
    }
    const new_text = modulefile.rewriteRoot(a, text, live, why) catch |err| return switch (err) {
        error.Refused => error.Refused,
        error.OutOfMemory => error.OutOfMemory,
    };
    return .{ .staged = staged, .live = live, .text = new_text };
}

/// Texts of the module files that could be read, plus the first read or
/// UTF-8 failure. A bad file does not drop the files already read.
pub const ModuleTexts = struct {
    files: []const []const u8,
    texts: std.StringArrayHashMapUnmanaged([]const u8),
    why: []const u8 = "",
};

pub fn readModuleTexts(a: Allocator, files: []const []const u8) Allocator.Error!ModuleTexts {
    var texts: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    var good: std.ArrayList([]const u8) = .empty;
    var why: []const u8 = "";
    for (files) |p| {
        const opened = fsx.openRegular(fsx.cwd, p, true) catch |err| {
            if (why.len == 0) why = try a.print("cannot read staged module file {s}: {s}", .{ p, main_mod.osError(a, err, p) });
            continue;
        };
        defer sys.close(opened.fd);
        const t = fsx.readAll(a, opened.fd, 64 << 20) catch |err| {
            if (why.len == 0) why = try a.print("cannot read staged module file {s}: {s}", .{ p, main_mod.osError(a, err, p) });
            continue;
        };
        if (!std.unicode.utf8ValidateSlice(t)) {
            if (why.len == 0) why = try a.print("{s} is not UTF-8 text", .{p});
            continue;
        }
        try texts.put(a, p, t);
        try good.append(a, p);
    }
    return .{ .files = good.items, .texts = texts, .why = why };
}

/// order_module_files: dependencies first, ties and the remainder of a
/// cycle in path order. Returns the order and each file's text. Every file
/// is read; the first unreadable or non-UTF-8 file is reported after that.
pub fn orderModuleFiles(a: Allocator, files: []const []const u8, modules_root: []const u8, why: *[]const u8) Error!struct { order: []const []const u8, texts: std.StringArrayHashMapUnmanaged([]const u8) } {
    const read = try readModuleTexts(a, files);
    if (read.why.len != 0) {
        why.* = read.why;
        return error.Refused;
    }
    const texts = read.texts;
    var by_identity: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    for (files) |p| {
        const id = modulefile.moduleIdentity(a, modules_root, p);
        if (!by_identity.contains(id)) try by_identity.put(a, id, p);
    }
    var incoming: std.StringArrayHashMapUnmanaged(usize) = .empty;
    var children: std.StringArrayHashMapUnmanaged(std.ArrayList([]const u8)) = .empty;
    for (files) |p| {
        try incoming.put(a, p, 0);
        try children.put(a, p, .empty);
    }
    for (files) |p| {
        for (try modulefile.loads(a, texts.get(p).?)) |name| {
            const earlier = by_identity.get(name) orelse continue;
            if (std.mem.eql(u8, earlier, p)) continue;
            incoming.getPtr(p).?.* += 1;
            try children.getPtr(earlier).?.append(a, p);
        }
    }
    var ready: std.ArrayList([]const u8) = .empty;
    for (files) |p| if (incoming.get(p).? == 0) try ready.append(a, p);
    std.mem.sort([]const u8, ready.items, {}, lessText);
    var ordered: std.ArrayList([]const u8) = .empty;
    var seen: std.StringArrayHashMapUnmanaged(void) = .empty;
    while (ready.items.len > 0) {
        const p = ready.orderedRemove(0);
        if (seen.contains(p)) continue;
        try seen.put(a, p, {});
        try ordered.append(a, p);
        for (children.get(p).?.items) |c| {
            const n = incoming.getPtr(c).?;
            n.* -= 1;
            if (n.* == 0) try ready.append(a, c);
        }
        std.mem.sort([]const u8, ready.items, {}, lessText);
    }
    for (files) |p| if (!seen.contains(p)) try ordered.append(a, p);
    return .{ .order = ordered.items, .texts = texts };
}

/// map_link_target: an absolute target inside the staged module tree points
/// at the same relative path in the live tree.
pub fn mapLinkTarget(a: Allocator, target: []const u8, stage_modules: []const u8, live_modules: []const u8) []const u8 {
    if (target.len == 0 or target[0] != '/') return target;
    const target_abs = admin.abspath(a, target);
    const stage_abs = admin.abspath(a, stage_modules);
    if (admin.isWithin(a, target_abs, stage_abs)) {
        const rel = modulefile.relpath(a, target_abs, stage_abs);
        if (std.mem.eql(u8, rel, ".")) return admin.abspath(a, live_modules);
        return std.fs.path.join(a, &.{ admin.abspath(a, live_modules), rel }) catch target_abs;
    }
    return target_abs;
}

test "ordering puts dependencies first and maps links" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try a.print("/tmp/esr-discover-{d}", .{std.os.linux.getpid()});
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const m = try a.print("{s}/all", .{base});
    try common.atomic.makedirs(try a.print("{s}/zlib", .{m}));
    try common.atomic.makedirs(try a.print("{s}/app", .{m}));
    var why: common.atomic.Why = .{};
    try common.atomic.atomicWrite(a, try a.print("{s}/app/1.lua", .{m}), "load(\"zlib/2\")\n", .{}, &why);
    try common.atomic.atomicWrite(a, try a.print("{s}/zlib/2.lua", .{m}), "whatis(\"z\")\n", .{}, &why);
    var w: []const u8 = "";
    const tree = try walkModuleTree(a, m, &w);
    try std.testing.expectEqual(@as(usize, 2), tree.files.len);
    const o = try orderModuleFiles(a, tree.files, m, &w);
    try std.testing.expect(std.mem.endsWith(u8, o.order[0], "zlib/2.lua"));
    const bad = try a.print("{s}/app/bad.lua", .{m});
    try common.atomic.atomicWrite(a, bad, "local root = \"/x\"\n\xff\n", .{}, &why);
    try std.testing.expectError(error.Refused, orderModuleFiles(a, &.{bad}, m, &w));
    try std.testing.expect(std.mem.endsWith(u8, w, " is not UTF-8 text"));
    try std.testing.expectEqualStrings("/live/all/x", mapLinkTarget(a, "/stage/all/x", "/stage", "/live"));
    try std.testing.expectEqualStrings("rel/x", mapLinkTarget(a, "rel/x", "/stage", "/live"));
}
