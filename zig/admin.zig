//! retire, rollback of a retire journal, gc and verify: the commands that
//! change a module tree or a state root without building anything.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");
const generations = @import("generations.zig");
const inuse = @import("inuse.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const state = common.state;
const rootlock = @import("rootlock.zig");
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// The grace period in hours when neither --grace-hours nor
/// EB_STAGED_REBUILD_GRACE_HOURS gives one: the Python tool's
/// DEFAULT_GRACE_HOURS, a job of up to 120 hours and a day more.
pub const default_grace_hours: f64 = 120 + 24;
const seconds_per_hour: f64 = 3600;

fn refuse(comptime f: []const u8, args: anytype) noreturn {
    main_mod.refuse(f, args);
}

/// "eb-staged-rebuild: MESSAGE" on stdout, as the Python tool logs progress.
fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

/// The time in seconds since the epoch, as Python's time.time().
pub fn nowSeconds() f64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.REALTIME, &ts);
    return @as(f64, @floatFromInt(ts.sec)) + @as(f64, @floatFromInt(ts.nsec)) / 1e9;
}

// ---- paths --------------------------------------------------------------

/// os.path.abspath(os.path.normpath(p)).
pub fn abspath(a: Allocator, p: []const u8) []const u8 {
    var full = p;
    if (p.len == 0 or p[0] != '/') {
        var rb: [sys.path_max]u8 = undefined;
        const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch ".";
        full = std.fs.path.join(a, &.{ cwd, p }) catch p;
    }
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, full, '/');
    while (it.next()) |seg| {
        if (seg.len == 0 or std.mem.eql(u8, seg, ".")) continue;
        if (std.mem.eql(u8, seg, "..")) {
            _ = parts.pop();
            continue;
        }
        parts.append(a, seg) catch oom();
    }
    const joined = std.mem.join(a, "/", parts.items) catch return full;
    return a.print("/{s}", .{joined}) catch full;
}

/// Whether `child` is `parent` or a path under it, both made absolute as
/// abspath makes them; symbolic links are not followed.
pub fn isWithin(a: Allocator, child: []const u8, parent: []const u8) bool {
    const c = abspath(a, child);
    const p = abspath(a, parent);
    if (std.mem.eql(u8, c, p)) return true;
    const prefix = a.print("{s}/", .{std.mem.trimEnd(u8, p, "/")}) catch return false;
    return std.mem.startsWith(u8, c, prefix);
}

fn dirname(p: []const u8) []const u8 {
    return std.fs.path.dirname(p) orelse "";
}

// ---- journals -------------------------------------------------------------

fn dictSet(a: Allocator, d: Py, key: []const u8, value: Py) Py {
    var out: std.ArrayList(Py.Entry) = .empty;
    var found = false;
    for (d.dict) |e| {
        if (std.mem.eql(u8, e.key, key)) {
            out.append(a, .{ .key = e.key, .value = value }) catch oom();
            found = true;
        } else out.append(a, e) catch oom();
    }
    if (!found) out.append(a, .{ .key = key, .value = value }) catch oom();
    return .{ .dict = out.items };
}

/// The text of a JSON string, or null for any other value and for none.
pub fn str(v: ?Py) ?[]const u8 {
    const x = v orelse return null;
    return if (x == .str) x.str else null;
}

/// Whether `v` is the JSON string `s`.
pub fn eqlStr(v: ?Py, s: []const u8) bool {
    const x = str(v) orelse return false;
    return std.mem.eql(u8, x, s);
}

/// Pause before a step the tests name in EB_STAGED_REBUILD_PAUSE_AT, as
/// the Python fault injector does.
const PauseCtx = struct { env: probe.Env };

fn reachStep(ctx: ?*anyopaque, step: []const u8) void {
    const c: *const PauseCtx = @ptrCast(@alignCast(ctx.?));
    const token = c.env.get("EB_STAGED_REBUILD_PAUSE_AT") orelse return;
    // A trailing "*" names every step that starts with what precedes it.
    const prefix = token.len > 0 and token[token.len - 1] == '*';
    if (!(std.mem.eql(u8, token, step) or (prefix and std.mem.startsWith(u8, step, token[0 .. token.len - 1])))) return;
    if (c.env.get("EB_STAGED_REBUILD_PAUSE_READY")) |ready| if (ready.len > 0) {
        if (sys.openat(fsx.cwd, ready, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o644)) |fd| sys.close(fd) else |_| {}
    };
    const secs = std.fmt.parseFloat(f64, c.env.get("EB_STAGED_REBUILD_PAUSE_SECONDS") orelse "30") catch 30;
    var ts: linux.timespec = .{ .sec = @intFromFloat(@floor(secs)), .nsec = @intFromFloat((secs - @floor(secs)) * 1e9) };
    _ = linux.nanosleep(&ts, null);
}

/// Fail each step the tests name in EB_STAGED_REBUILD_FAIL_AT (a
/// comma-separated list), as the Python fault injector raises OSError(EIO)
/// there. Only a replaceable build (the -Dtest-ops artifact) asks;
/// Faults.fails never calls it otherwise.
fn failStep(ctx: ?*anyopaque, step: []const u8) bool {
    const c: *const PauseCtx = @ptrCast(@alignCast(ctx.?));
    const token = c.env.get("EB_STAGED_REBUILD_FAIL_AT") orelse return false;
    var it = std.mem.splitScalar(u8, token, ',');
    while (it.next()) |name| if (std.mem.eql(u8, name, step)) return true;
    return false;
}

/// What a command hands down: the allocator, the environment, and the
/// fault hooks the tests reach by name.
pub const Ctx = struct {
    a: Allocator,
    env: probe.Env,
    pause: PauseCtx,

    /// The fault hooks: pause at EB_STAGED_REBUILD_PAUSE_AT and, in the
    /// -Dtest-ops artifact only, fail at EB_STAGED_REBUILD_FAIL_AT.
    pub fn faults(c: *Ctx) atomic.Faults {
        return .{ .ctx = &c.pause, .reach_fn = reachStep, .fail_fn = if (atomic.replaceable) failStep else null };
    }
};

/// save_journal: the schema and publication fields, the directory-swap
/// check, then json.dumps(indent=2, sort_keys=True) written atomically.
fn saveJournal(c: *Ctx, path: []const u8, data_in: Py, step: []const u8) Py {
    var data = data_in;
    if (eqlStr(pyfmt.get(data, "kind"), "retire")) {
        data = dictSet(c.a, data, "schema", .{ .int = 2 });
    } else if (pyfmt.truthy(pyfmt.get(data, "ledger_policy"))) {
        data = dictSet(c.a, data, "schema", .{ .int = 4 });
        data = dictSet(c.a, data, "publication", Py.s("directory-swap"));
    } else {
        data = dictSet(c.a, data, "schema", .{ .int = 2 });
        const p = pyfmt.get(data, "publication");
        if (p == null or p.? == .null) data = dictSet(c.a, data, "publication", Py.s("directory-swap"));
    }
    main_mod.requireDirectorySwap(c.a, data);
    const body = json.dumps(c.a, data, 2, true) catch refuse("out of memory", .{});
    const raw = c.a.print("{s}\n", .{body}) catch refuse("out of memory", .{});
    var why: atomic.Why = .{};
    atomic.atomicWrite(c.a, path, raw, .{ .faults = c.faults(), .step = step }, &why) catch |err|
        if (err == error.Refused) refuse("{s}", .{why.msg}) else refuse("{s}", .{main_mod.osError(c.a, err, path)});
    return data;
}

fn journalPath(a: Allocator, state_root: []const u8, run_id: []const u8) []const u8 {
    return std.fs.path.join(a, &.{ state_root, "runs", run_id, "journal.json" }) catch state_root;
}

fn lock(c: *Ctx, state_root: []const u8, timeout_s: ?f64) state.RootLock {
    const ms: ?u32 = if (timeout_s) |t| @intFromFloat(@min(@max(t, 0) * 1000, 4.0e9)) else null;
    var detail: []const u8 = state_root;
    return rootlock.acquire(c.a, state_root, ms, &detail) catch |err| switch (err) {
        error.Busy => refuse("another eb-staged-rebuild holds the lock at {s}/.lock", .{state_root}),
        error.NotPrivate => refuse("record lock requires a private regular file: {s}/.lock.posix", .{state_root}),
        error.Gate => refuse("lock file must be a private regular file: {s}/.lock", .{state_root}),
        error.Unsafe => refuse("state root is unsafe: {s} is a symlink", .{detail}),
        else => refuse("{s}", .{main_mod.osError(c.a, err, state_root)}),
    };
}

fn sha256Hex(a: Allocator, data: []const u8) []const u8 {
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(data, &out, .{});
    const hex = std.fmt.bytesToHex(out, .lower);
    return a.dupe(u8, &hex) catch "";
}

fn readFile(c: *Ctx, path: []const u8) []const u8 {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    defer sys.close(f.fd);
    return fsx.readAll(c.a, f.fd, 64 << 20) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
}

// ---- retire -------------------------------------------------------------

/// The Unix time `t` in UTC, as 2026-11-17T08:53:00Z with `with_dashes` and
/// as 20261117T085300Z without, to the second.
pub fn gmtimeStamp(a: Allocator, t: f64, comptime with_dashes: bool) []const u8 {
    const secs: u64 = @intFromFloat(@max(@floor(t), 0));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    if (with_dashes) return a.print("{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
    return a.print("{d:0>4}{d:0>2}{d:0>2}T{d:0>2}{d:0>2}{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

fn lexists(p: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, p, false) catch return false;
    return true;
}

const Plan = struct { module: []const u8, data: []const u8, software_dir: []const u8 };

fn plan(c: *Ctx, state_root: []const u8, module_in: []const u8) Plan {
    const module = abspath(c.a, module_in);
    if (isWithin(c.a, module, state_root)) refuse("{s} is inside the state root", .{module});
    for (main_mod.listJournals(c.a, state_root)) |j| {
        if (!eqlStr(pyfmt.get(j, "kind"), "retire")) continue;
        const st = pyfmt.get(j, "state");
        if (eqlStr(pyfmt.get(j, "module_path"), module) and (eqlStr(st, "retiring") or eqlStr(st, "retired"))) {
            var rid: std.ArrayList(u8) = .empty;
            pyfmt.str(&rid, c.a, pyfmt.get(j, "run_id")) catch oom();
            refuse("{s} is already retired ({s})", .{ module, rid.items });
        }
    }
    const kind = main_mod.lstatKind(module);
    if (!std.mem.eql(u8, kind, "file")) refuse("{s} is not a regular file ({s})", .{ module, kind });
    const data = readFile(c, module);
    if (!std.unicode.utf8ValidateSlice(data)) refuse("{s} is not UTF-8 text", .{module});
    const root = modulefile.parseRoot(data) orelse refuse("{s} has no root assignment, so its software directory is unknown", .{module});
    if (root.path.len == 0) refuse("{s} has no root assignment, so its software directory is unknown", .{module});
    return .{ .module = module, .data = data, .software_dir = abspath(c.a, root.path) };
}

fn isRealDir(p: []const u8) bool {
    return std.mem.eql(u8, main_mod.lstatKind(p), "dir");
}

/// The names in directory `dir`, or none when it cannot be listed.
fn entryNames(a: Allocator, dir: []const u8) []const []const u8 {
    const fd = fsx.openDir(fsx.cwd, dir, true) catch return &.{};
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch oom();
    defer it.deinit(a);
    var names: std.ArrayList([]const u8) = .empty;
    while (it.next() catch null) |e| names.append(a, a.dupe(u8, e.name) catch oom()) catch oom();
    return names.items;
}

fn readLink(c: *Ctx, path: []const u8) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    return c.a.dupe(u8, buf[0..n]) catch oom();
}

/// Symbolic links that resolve to `module`, sorted: in the module file's
/// directory and, for each `all` directory above it, in the same place under
/// each sibling of that directory. EasyBuild puts a module class link at
/// `<modules>/<class>/<name>/...` beside `<modules>/all/<name>/...`.
fn linksTo(a: Allocator, module: []const u8) []const []const u8 {
    var rb: [sys.path_max]u8 = undefined;
    const real = a.dupe(u8, fsx.realpath(fsx.cwd, module, &rb) catch return &.{}) catch oom();
    const dir = dirname(module);
    var directories: std.ArrayList([]const u8) = .empty;
    directories.append(a, dir) catch oom();
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, dir, '/');
    while (it.next()) |seg| parts.append(a, seg) catch oom();
    for (parts.items, 0..) |part, index| {
        if (!std.mem.eql(u8, part, "all")) continue;
        const joined = std.mem.join(a, "/", parts.items[0..index]) catch oom();
        const top = if (joined.len > 0) joined else "/";
        for (entryNames(a, top)) |name| {
            if (std.mem.eql(u8, name, "all")) continue;
            const sibling = std.fs.path.join(a, &.{ top, name }) catch oom();
            var pieces: std.ArrayList([]const u8) = .empty;
            pieces.append(a, sibling) catch oom();
            pieces.appendSlice(a, parts.items[index + 1 ..]) catch oom();
            const candidate = std.fs.path.join(a, pieces.items) catch oom();
            if (isRealDir(sibling) and isRealDir(candidate)) directories.append(a, candidate) catch oom();
        }
    }
    var links: std.ArrayList([]const u8) = .empty;
    for (directories.items) |d| {
        for (entryNames(a, d)) |name| {
            const path = std.fs.path.join(a, &.{ d, name }) catch oom();
            if (!std.mem.eql(u8, main_mod.lstatKind(path), "symlink")) continue;
            var lb: [sys.path_max]u8 = undefined;
            const target = fsx.realpath(fsx.cwd, path, &lb) catch continue;
            if (!std.mem.eql(u8, target, real)) continue;
            for (links.items) |seen| {
                if (std.mem.eql(u8, seen, path)) break;
            } else links.append(a, path) catch oom();
        }
    }
    std.mem.sort([]const u8, links.items, {}, struct {
        fn lt(_: void, x: []const u8, y: []const u8) bool {
            return std.mem.order(u8, x, y) == .lt;
        }
    }.lt);
    return links.items;
}

fn discardEmpty(a: Allocator, top: []const u8) void {
    // os.walk(top, topdown=False) then rmdir each: children first.
    const fd = fsx.openDir(fsx.cwd, top, false) catch {
        rmdir(top);
        return;
    };
    var names: std.ArrayList([]const u8) = .empty;
    {
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch return;
        while (it.next() catch null) |e| if ((fsx.entryType(fd, e) catch 0) == fsx.DT.DIR) names.append(a, a.dupe(u8, e.name) catch continue) catch oom();
    }
    for (names.items) |n| discardEmpty(a, std.fs.path.join(a, &.{ top, n }) catch continue);
    rmdir(top);
}

fn rmdir(p: []const u8) void {
    sys.unlinkat(fsx.cwd, p, linux.AT.REMOVEDIR) catch {};
}

fn statDev(c: *Ctx, p: []const u8) u64 {
    const st = fsx.statAt(fsx.cwd, p, true) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, p)});
    return st.dev;
}

/// Rename `source` onto `destination` once the file's bytes are durable.
fn move(c: *Ctx, source: []const u8, destination: []const u8, step: []const u8) void {
    const f = c.faults();
    const fsync_step = c.a.print("{s}.fsync", .{step}) catch step;
    if (f.fails(fsync_step)) refuse("[Errno 5] injected failure at {s}", .{fsync_step});
    {
        const fd = sys.openat(fsx.cwd, source, .{}, 0) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, source)});
        defer sys.close(fd);
        sys.fsync(fd) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, source)});
    }
    rename(c, source, destination, c.a.print("{s}.rename", .{step}) catch step);
}

fn rename(c: *Ctx, source: []const u8, destination: []const u8, step: []const u8) void {
    if (c.faults().fails(step)) refuse("[Errno 5] injected failure at {s}", .{step});
    sys.renameat(fsx.cwd, source, fsx.cwd, destination) catch |err| switch (err) {
        error.CrossDevice => refuse("{s} and {s} are on different filesystems", .{ source, destination }),
        else => refuse("{s}", .{main_mod.osError(c.a, err, source)}),
    };
    atomic.fsyncDir(dirname(destination)) catch {};
    atomic.fsyncDir(dirname(source)) catch {};
}

/// retire: move a module file aside under a retire journal, and leave its
/// software directory in place; with `dry_run`, only say what would be
/// retired. Returns the exit status.
pub fn retire(c: *Ctx, state_root_in: []const u8, module_file: []const u8, reason: ?[]const u8, dry_run: bool, lock_timeout: ?f64) u8 {
    const a = c.a;
    const state_root = abspath(a, state_root_in);
    if (dry_run) {
        const p = plan(c, state_root, module_file);
        for (linksTo(a, p.module)) |link| log("would retire the link {s}", .{link});
        log("would retire {s} (software directory {s} is left in place)", .{ p.module, p.software_dir });
        return 0;
    }
    const l = lock(c, state_root, lock_timeout);
    defer l.release();
    const p = plan(c, state_root, module_file);
    const when = nowSeconds();
    const stamp = gmtimeStamp(a, when, false);
    var name = stamp;
    var count: usize = 1;
    const retired_root = std.fs.path.join(a, &.{ state_root, "retired" }) catch state_root;
    while (lexists(std.fs.path.join(a, &.{ retired_root, name }) catch "") or
        lexists(std.fs.path.join(a, &.{ state_root, "runs", a.print("retire-{s}", .{name}) catch "" }) catch ""))
    {
        count += 1;
        name = a.print("{s}-{d}", .{ stamp, count }) catch name;
    }
    const run_id = a.print("retire-{s}", .{name}) catch name;
    const top = std.fs.path.join(a, &.{ retired_root, name }) catch oom();
    const destination = std.fs.path.join(a, &.{ top, std.mem.trimStart(u8, p.module, "/") }) catch "";
    const live_links = linksTo(a, p.module);
    const links = a.alloc(Py, live_links.len) catch oom();
    for (live_links, links) |live, *link| {
        var entry: Py = .{ .dict = &.{} };
        entry = dictSet(a, entry, "live_path", Py.s(live));
        entry = dictSet(a, entry, "target", Py.s(readLink(c, live)));
        entry = dictSet(a, entry, "retired_path", Py.s(std.fs.path.join(a, &.{ top, std.mem.trimStart(u8, live, "/") }) catch oom()));
        link.* = dictSet(a, entry, "status", Py.s("pending"));
    }
    atomic.makedirs(dirname(destination)) catch |err| refuse("{s}", .{main_mod.osError(a, err, dirname(destination))});
    if (statDev(c, dirname(p.module)) != statDev(c, dirname(destination))) {
        discardEmpty(a, top);
        refuse("{s} and the state root are on different filesystems; retire needs one atomic rename, so put --state-root on the same filesystem as the module tree", .{p.module});
    }
    for (links) |link| {
        const live = str(pyfmt.get(link, "live_path")).?;
        const kept = dirname(str(pyfmt.get(link, "retired_path")).?);
        atomic.makedirs(kept) catch |err| refuse("{s}", .{main_mod.osError(a, err, kept)});
        if (statDev(c, dirname(live)) != statDev(c, kept)) {
            discardEmpty(a, top);
            refuse("{s} and the state root are on different filesystems; retire needs one atomic rename, so put --state-root on the same filesystem as the module tree", .{live});
        }
    }
    const path = journalPath(a, state_root, run_id);
    var journal: Py = .{ .dict = &.{} };
    const fields = [_]Py.Entry{
        .{ .key = "schema", .value = .{ .int = 1 } },
        .{ .key = "kind", .value = Py.s("retire") },
        .{ .key = "run_id", .value = Py.s(run_id) },
        .{ .key = "state", .value = Py.s("retiring") },
        .{ .key = "state_root", .value = Py.s(state_root) },
        .{ .key = "module_path", .value = Py.s(p.module) },
        .{ .key = "module_hash", .value = Py.s(sha256Hex(a, p.data)) },
        .{ .key = "software_dir", .value = Py.s(p.software_dir) },
        .{ .key = "retired_path", .value = Py.s(destination) },
        .{ .key = "reason", .value = Py.s(reason orelse "") },
        .{ .key = "created_at", .value = .{ .float = when } },
        .{ .key = "updated_at", .value = .{ .float = when } },
        .{ .key = "retired_at", .value = .null },
        .{ .key = "rolled_back_at", .value = .null },
        .{ .key = "modules_plan", .value = .{ .list = &.{} } },
        .{ .key = "links_plan", .value = .{ .list = links } },
        .{ .key = "message", .value = Py.s("") },
    };
    for (fields) |f| journal = dictSet(a, journal, f.key, f.value);
    journal = saveJournal(c, path, journal, "journal.retiring");
    for (links, 1..) |*link, number| {
        const live = str(pyfmt.get(link.*, "live_path")).?;
        const kept = str(pyfmt.get(link.*, "retired_path")).?;
        rename(c, live, kept, a.print("retire.link:{d}", .{number}) catch oom());
        link.* = dictSet(a, link.*, "status", Py.s("retired"));
        log("retired the link {s} -> {s}", .{ live, kept });
    }
    journal = dictSet(a, journal, "links_plan", .{ .list = links });
    move(c, p.module, destination, "retire");
    const t = nowSeconds();
    journal = dictSet(a, journal, "state", Py.s("retired"));
    journal = dictSet(a, journal, "retired_at", .{ .float = t });
    journal = dictSet(a, journal, "updated_at", .{ .float = t });
    journal = dictSet(a, journal, "message", Py.s("module file retired"));
    _ = saveJournal(c, path, journal, "journal.retired");
    log("retired {s} -> {s}; software directory {s} left in place", .{ p.module, destination, p.software_dir });
    return 0;
}

// ---- verify ---------------------------------------------------------------

/// verify: check the stage of a built run under the state root's lock, and
/// write and print the result with run.zig's verifyCommand. Returns the exit
/// status: 0 when the stage passes, 1 when it fails.
pub fn verify(c: *Ctx, state_root: []const u8, run_id: []const u8, lock_timeout: ?f64) u8 {
    const a = c.a;
    const path = journalPath(a, state_root, run_id);
    const l = lock(c, state_root, lock_timeout);
    defer l.release();
    const st = fsx.statAt(fsx.cwd, path, false) catch refuse("no journal at {s}", .{path});
    if (!st.isReg()) refuse("no journal at {s}", .{path});
    const journal = main_mod.loadJournal(a, path);
    if (eqlStr(pyfmt.get(journal, "kind"), "retire")) refuse("run {s} is a retire record; it has no stage to verify", .{run_id});
    const state_name = str(pyfmt.get(journal, "state")) orelse "";
    if (!std.mem.eql(u8, state_name, "built")) refuse("run {s} is {s}; only a built run has a stage to verify", .{ run_id, state_name });
    const run_mod = @import("run.zig");
    var r: run_mod.R = .{ .a = a, .env = c.env, .ctx = c, .spec = .{ .software = "", .modules = "", .state_root = state_root, .eb_args = &.{} }, .journal = journal, .path = path };
    return run_mod.verifyCommand(&r) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{r.why}),
    };
}

// ---- rollback -------------------------------------------------------------

/// rollback: one run, the newest complete run with `latest`, or every run
/// published over `to`, under one hold of the state root's lock. A run
/// journal goes to run.zig's rollbackRun and `to` to its rollbackTo; a
/// retire record's module file is moved back. Returns the exit status.
pub fn rollback(c: *Ctx, state_root: []const u8, run_id: ?[]const u8, to: ?[]const u8, latest: bool, lock_timeout: ?f64) u8 {
    if (@as(u8, @intFromBool(run_id != null)) + @intFromBool(to != null) + @intFromBool(latest) != 1)
        refuse("give one of a run id, --latest and --to GEN", .{});
    const l = lock(c, state_root, lock_timeout);
    defer l.release();
    const run_mod = @import("run.zig");
    var r: run_mod.R = .{ .a = c.a, .env = c.env, .ctx = c, .spec = .{ .software = "", .modules = "", .state_root = state_root, .eb_args = &.{} } };
    if (to) |gen| return outcome(&r, run_mod.rollbackTo(&r, gen));
    const id = run_id orelse run_mod.newestComplete(&r) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{r.why}),
    };
    return rollbackOne(c, &r, state_root, id);
}

fn outcome(r: anytype, result: anytype) u8 {
    const res = result catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{r.why}),
    };
    return res.code;
}

fn rollbackOne(c: *Ctx, r: anytype, state_root: []const u8, run_id: []const u8) u8 {
    const a = c.a;
    const path = journalPath(a, state_root, run_id);
    const st = fsx.statAt(fsx.cwd, path, false) catch refuse("no journal at {s}", .{path});
    if (!st.isReg()) refuse("no journal at {s}", .{path});
    var journal = main_mod.loadJournal(a, path);
    if (!eqlStr(pyfmt.get(journal, "kind"), "retire")) {
        r.journal = journal;
        r.path = path;
        return outcome(r, @import("run.zig").rollbackRun(r));
    }
    if (eqlStr(pyfmt.get(journal, "state"), "rolled_back")) {
        log("run {s} is already rolled back", .{run_id});
        return 0;
    }
    const module = str(pyfmt.get(journal, "module_path")) orelse refuse("journal has no module_path", .{});
    const retired = str(pyfmt.get(journal, "retired_path")) orelse refuse("journal has no retired_path", .{});
    const want = str(pyfmt.get(journal, "module_hash")) orelse "";
    const live_kind = main_mod.lstatKind(module);
    const retired_kind = main_mod.lstatKind(retired);
    if (std.mem.eql(u8, retired_kind, "file")) {
        if (!std.mem.eql(u8, sha256Hex(a, readFile(c, retired)), want)) refuse("{s} does not match the module file recorded for {s}", .{ retired, run_id });
        if (!std.mem.eql(u8, live_kind, "missing")) refuse("{s} exists again; refusing to overwrite it with the retired copy", .{module});
        move(c, retired, module, "rollback.retire");
        log("restored {s}", .{module});
    } else if (std.mem.eql(u8, live_kind, "file") and std.mem.eql(u8, sha256Hex(a, readFile(c, module)), want)) {
        // Already back in place.
    } else {
        refuse("neither {s} nor {s} holds the retired module file", .{ module, retired });
    }
    const recorded = pyfmt.get(journal, "links_plan");
    const links = a.dupe(Py, if (recorded != null and recorded.? == .list) recorded.?.list else &.{}) catch oom();
    var number = links.len;
    while (number > 0) : (number -= 1) {
        const link = &links[number - 1];
        const live = str(pyfmt.get(link.*, "live_path")) orelse refuse("journal link has no live_path", .{});
        const kept = str(pyfmt.get(link.*, "retired_path")) orelse refuse("journal link has no retired_path", .{});
        const target = str(pyfmt.get(link.*, "target")) orelse refuse("journal link has no target", .{});
        if (std.mem.eql(u8, main_mod.lstatKind(kept), "symlink")) {
            if (!std.mem.eql(u8, readLink(c, kept), target)) refuse("{s} does not match the link recorded for {s}", .{ kept, run_id });
            if (!std.mem.eql(u8, main_mod.lstatKind(live), "missing")) refuse("{s} exists again; refusing to overwrite it with the retired link", .{live});
            rename(c, kept, live, a.print("rollback.retire.link:{d}", .{number}) catch oom());
            log("restored the link {s}", .{live});
        } else if (!(std.mem.eql(u8, main_mod.lstatKind(live), "symlink") and std.mem.eql(u8, readLink(c, live), target))) {
            refuse("neither {s} nor {s} holds the link recorded for {s}", .{ live, kept, run_id });
        }
        link.* = dictSet(a, link.*, "status", Py.s("restored"));
    }
    journal = dictSet(a, journal, "links_plan", .{ .list = links });
    const t = nowSeconds();
    journal = dictSet(a, journal, "state", Py.s("rolled_back"));
    journal = dictSet(a, journal, "rolled_back_at", .{ .float = t });
    journal = dictSet(a, journal, "updated_at", .{ .float = t });
    journal = dictSet(a, journal, "message", Py.s("rolled back"));
    _ = saveJournal(c, path, journal, "journal.rolled_back");
    log("run {s} rolled back; {s} restored", .{ run_id, module });
    return 0;
}

// ---- gc -----------------------------------------------------------------

fn moduleRoot(c: *Ctx, path: []const u8) ?[]const u8 {
    const st = fsx.statAt(fsx.cwd, path, false) catch |err| switch (err) {
        error.NotFound => return null,
        else => refuse("cannot inspect live module {s}: {s}", .{ path, main_mod.osError(c.a, err, path) }),
    };
    if (!st.isReg()) refuse("cannot determine live references from a non-regular module: {s}", .{path});
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("cannot read live references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    defer sys.close(f.fd);
    const data = fsx.readAll(c.a, f.fd, 64 << 20) catch |err| refuse("cannot read live references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    if (!std.unicode.utf8ValidateSlice(data)) refuse("cannot read live references from {s}: not UTF-8 text", .{path});
    const root = modulefile.parseRoot(data) orelse refuse("cannot determine the live software root from {s}", .{path});
    if (root.path.len == 0) refuse("cannot determine the live software root from {s}", .{path});
    return abspath(c.a, root.path);
}

fn resolved(c: *Ctx, path: []const u8) []const u8 {
    var rb: [sys.path_max]u8 = undefined;
    const r = fsx.realpath(fsx.cwd, path, &rb) catch |err| refuse("cannot resolve live software references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    return c.a.dupe(u8, r) catch path;
}

fn liveRoots(c: *Ctx, journals: []const Py) []const []const u8 {
    var roots: std.ArrayList([]const u8) = .empty;
    for (journals) |j| {
        if (eqlStr(pyfmt.get(j, "kind"), "retire")) {
            if (!eqlStr(pyfmt.get(j, "state"), "retired")) {
                if (moduleRoot(c, str(pyfmt.get(j, "module_path")) orelse "")) |r| {
                    roots.append(c.a, r) catch oom();
                    roots.append(c.a, resolved(c, r)) catch oom();
                }
            }
            continue;
        }
        const plan_v = pyfmt.get(j, "modules_plan");
        if (!pyfmt.truthy(plan_v)) continue;
        for (plan_v.?.list) |m| {
            const live = str(pyfmt.get(m, "live_module")) orelse continue;
            if (live.len == 0) continue;
            if (moduleRoot(c, live)) |r| {
                roots.append(c.a, r) catch oom();
                roots.append(c.a, resolved(c, r)) catch oom();
            }
        }
    }
    return roots.items;
}

fn referenced(c: *Ctx, directory: []const u8, roots: []const []const u8) bool {
    const spellings = [_][]const u8{ abspath(c.a, directory), resolved(c, directory) };
    for (roots) |r| for (spellings) |s| if (isWithin(c.a, r, s)) return true;
    return false;
}

/// A JSON number as a float, a boolean as 1 or 0, and null for any other
/// value and for none.
pub fn num(v: ?Py) ?f64 {
    const x = v orelse return null;
    return switch (x) {
        .int => |n| @floatFromInt(n),
        .float => |f| f,
        .bool => |b| if (b) 1 else 0,
        else => null,
    };
}

const Action = struct { path: []const u8, action: []const u8, reason: []const u8 };

fn finishedRun(j: Py, include_failed: bool) ?[]const u8 {
    const st = pyfmt.get(j, "state");
    if (eqlStr(st, "complete")) return "complete run";
    if (eqlStr(st, "rolled_back")) return "rolled-back run";
    if (include_failed and eqlStr(st, "failed")) return "failed run";
    return null;
}

/// What to do with the generation of a run whose hidden trees are all
/// settled: remove it, or keep it while a live module resolves inside it;
/// null when the run is not finished or has no generation directory.
fn generationAction(c: *Ctx, state_root: []const u8, j: Py, roots: []const []const u8, include_failed: bool) ?Action {
    const what = finishedRun(j, include_failed) orelse return null;
    const rid = str(pyfmt.get(j, "run_id")) orelse "";
    if (rid.len == 0 or std.mem.eql(u8, rid, ".") or std.mem.eql(u8, rid, "..") or std.mem.findScalar(u8, rid, '/') != null)
        refuse("refusing a journal whose run id is not a plain name", .{});
    const path = std.fs.path.join(c.a, &.{ state_root, "generations", rid }) catch oom();
    if (!std.mem.eql(u8, main_mod.lstatKind(path), "dir")) return null;
    if (referenced(c, path, roots)) return .{ .path = path, .action = "keep", .reason = "a live module resolves inside it" };
    return .{ .path = path, .action = "remove", .reason = c.a.print("generation of a {s}", .{what}) catch oom() };
}

/// `--keep N`: N as given, compared saturated at 2^64 - 1 and printed as
/// Python prints int(N).
pub const Keep = struct { value: u64, text: []const u8 };

/// A generation `list` shows that a rollback can still switch back to.
fn returnable(listed: []const u8) bool {
    for ([_][]const u8{ "replaced", "original", "unknown" }) |s| if (std.mem.eql(u8, listed, s)) return true;
    return false;
}

/// Each hidden tree a rollback can switch back to, by its place among its
/// install path's, newest first.
fn ranks(a: Allocator, journals: []const Py) std.StringHashMapUnmanaged(u64) {
    var found: std.StringHashMapUnmanaged(u64) = .empty;
    for (generations.collect(a, journals)) |g| {
        var rank: u64 = 0;
        for (g.rows) |row| {
            if (!returnable(row.state)) continue;
            found.put(a, row.path, rank) catch oom();
            rank += 1;
        }
    }
    return found;
}

fn safeRetiredPath(c: *Ctx, m: Py) []const u8 {
    const live_raw = str(pyfmt.get(m, "live_software")) orelse "";
    const retired_raw = str(pyfmt.get(m, "retired")) orelse "";
    const live = abspath(c.a, live_raw);
    const retired = abspath(c.a, retired_raw);
    if (live.len == 0 or retired.len == 0) refuse("journal has no live/retired software path", .{});
    if (std.mem.eql(u8, live, retired) or !std.mem.eql(u8, dirname(live), dirname(retired))) refuse("refusing retired path that is not a live sibling: {s}", .{retired});
    const prefix = c.a.print(".{s}.", .{std.fs.path.basename(live)}) catch "";
    if (!std.mem.startsWith(u8, std.fs.path.basename(retired), prefix)) refuse("refusing unexpected retired path name: {s}", .{retired});
    return retired;
}

/// remove_generation_tree: delete an owned tree on one filesystem without
/// following links, opening a directory 0700 when it refuses entry.
pub fn removeTree(c: *Ctx, path_in: []const u8, boundary_in: []const u8) void {
    const path = abspath(c.a, path_in);
    const boundary = abspath(c.a, boundary_in);
    if (std.mem.eql(u8, path, boundary) or !isWithin(c.a, path, boundary)) refuse("cleanup path is outside its generation boundary: {s}", .{path});
    const parent = fsx.openDir(fsx.cwd, dirname(path), false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, dirname(path))});
    defer sys.close(parent);
    const dev = (fsx.fstat(parent) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)})).dev;
    const name = std.fs.path.basename(path);
    const fd = openOwned(c, parent, name, dev);
    emptyDir(c, fd, (fsx.fstat(fd) catch refuse("cleanup directory changed: {s}", .{name})).dev);
    sys.close(fd);
    sys.unlinkat(parent, name, linux.AT.REMOVEDIR) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    sys.fsync(parent) catch {};
}

fn openOwned(c: *Ctx, parent: sys.fd_t, name: []const u8, dev: u64) sys.fd_t {
    const info = fsx.statAt(parent, name, false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, name)});
    if (info.dev != dev) refuse("cleanup refuses a different filesystem: {s}", .{name});
    if (!info.isDir() or info.uid != linux.geteuid()) refuse("cleanup requires an owned real directory: {s}", .{name});
    const fd = fsx.openDir(parent, name, false) catch |err| switch (err) {
        error.AccessDenied => blk: {
            var zb: [sys.path_max:0]u8 = undefined;
            _ = linux.fchmodat(parent, (sys.z(&zb, name) catch refuse("bad name", .{})).ptr, @intCast(info.perm() | 0o700));
            break :blk fsx.openDir(parent, name, false) catch |e2| refuse("{s}", .{main_mod.osError(c.a, e2, name)});
        },
        else => refuse("{s}", .{main_mod.osError(c.a, err, name)}),
    };
    const cur = fsx.fstat(fd) catch refuse("cleanup directory changed: {s}", .{name});
    if (cur.dev != info.dev or cur.ino != info.ino or cur.uid != info.uid) {
        sys.close(fd);
        refuse("cleanup directory changed: {s}", .{name});
    }
    sys.fchmod(fd, @intCast(cur.perm() | 0o700)) catch {};
    return fd;
}

fn emptyDir(c: *Ctx, fd: sys.fd_t, dev: u64) void {
    var names: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(c.a, fd) catch return;
    while (it.next() catch |err| refuse("{t}", .{err})) |e| names.append(c.a, c.a.dupe(u8, e.name) catch continue) catch oom();
    for (names.items) |n| {
        const info = fsx.statAt(fd, n, false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
        if (info.isDir()) {
            const child = openOwned(c, fd, n, dev);
            emptyDir(c, child, dev);
            sys.close(child);
            sys.unlinkat(fd, n, linux.AT.REMOVEDIR) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
        } else sys.unlinkat(fd, n, 0) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
    }
}

/// _run_grace: the grace period in hours that the run recorded, or
/// `default`. A recorded value is used only when it is a number of hours,
/// at least 0.
fn runGrace(j: Py, default: f64) f64 {
    const hours: f64 = switch (pyfmt.get(j, "grace_hours") orelse return default) {
        .int => |n| @floatFromInt(n),
        .float => |f| f,
        else => return default,
    };
    return if (hours >= 0) hours else default;
}

/// gc: remove each hidden tree whose grace period is over and, with
/// `include_failed`, the staged tree of a failed run that published
/// nothing; with `dry_run`, only list them. `grace_hours` applies to every
/// tree, or with `run_grace` only to the trees whose run recorded no grace
/// period and to retired module files. With `keep`, the newest generations
/// of each install path that a rollback can return to are kept, and with
/// `check_in_use`, a tree a process or mount on this node uses. A run's
/// generation directory goes once every hidden tree of the run is settled,
/// unless `check_in_use` finds this node using it. Returns the exit status.
pub fn gc(c: *Ctx, state_root_in: []const u8, grace_hours: f64, dry_run: bool, include_failed: bool, keep: ?Keep, check_in_use: bool, lock_timeout: ?f64, run_grace: bool) u8 {
    const a = c.a;
    const now = nowSeconds();
    const l = lock(c, state_root_in, lock_timeout);
    defer l.release();
    const state_root = abspath(a, state_root_in);
    var journals = main_mod.listJournals(a, state_root);
    const roots = liveRoots(c, journals);
    const ranked: std.StringHashMapUnmanaged(u64) = if (keep != null) ranks(a, journals) else .empty;
    // Each run's lines, the trees it would remove and its generation, decided
    // before one scan of /proc for the ones that are still in use.
    const Removal = struct { mod: usize, action: usize };
    const Planned = struct { journal: usize, mods: []Py, actions: std.ArrayList(Action), removals: std.ArrayList(Removal), generation: ?Action };
    var planned: std.ArrayList(Planned) = .empty;
    const grace = grace_hours * seconds_per_hour;
    for (journals, 0..) |j, ji| {
        if (eqlStr(pyfmt.get(j, "kind"), "retire")) continue;
        const tree_grace = (if (run_grace) runGrace(j, grace_hours) else grace_hours) * seconds_per_hour;
        const plan_v = pyfmt.get(j, "modules_plan");
        var p: Planned = .{ .journal = ji, .mods = a.dupe(Py, if (pyfmt.truthy(plan_v)) plan_v.?.list else &.{}) catch oom(), .actions = .empty, .removals = .empty, .generation = null };
        // A failed build records no plan; the run's own clock decides.
        var settled = p.mods.len > 0 or blk: {
            if (!include_failed or !eqlStr(pyfmt.get(j, "state"), "failed")) break :blk false;
            const w = num(if (pyfmt.truthy(pyfmt.get(j, "updated_at"))) pyfmt.get(j, "updated_at") else pyfmt.get(j, "created_at")) orelse break :blk false;
            break :blk now - w >= tree_grace;
        };
        for (p.mods, 0..) |m, mi| {
            if (pyfmt.truthy(pyfmt.get(m, "retired_removed_at"))) continue;
            const retired = safeRetiredPath(c, m);
            const st = str(pyfmt.get(j, "state"));
            var when: ?f64 = null;
            var why: []const u8 = "";
            if (st != null and std.mem.eql(u8, st.?, "complete")) {
                when = num(pyfmt.get(m, "published_at"));
                why = "previous live tree";
            } else if (st != null and std.mem.eql(u8, st.?, "rolled_back")) {
                when = num(pyfmt.get(j, "rolled_back_at"));
                why = "rolled-back candidate";
            } else if (include_failed and st != null and std.mem.eql(u8, st.?, "failed") and eqlStr(pyfmt.get(m, "status"), "pending")) {
                when = num(if (pyfmt.truthy(pyfmt.get(j, "updated_at"))) pyfmt.get(j, "updated_at") else pyfmt.get(j, "created_at"));
                why = "failed candidate";
            } else {
                var b: std.ArrayList(u8) = .empty;
                pyfmt.str(&b, a, pyfmt.get(j, "state")) catch oom();
                why = a.print("state {s} is kept", .{b.items}) catch "";
            }
            if (!std.mem.eql(u8, main_mod.lstatKind(retired), "dir")) {
                if (when == null or now - when.? < tree_grace) settled = false;
                continue;
            }
            const w = when orelse {
                p.actions.append(a, .{ .path = retired, .action = "keep", .reason = why }) catch oom();
                settled = false;
                continue;
            };
            if (referenced(c, retired, roots)) {
                p.actions.append(a, .{ .path = retired, .action = "keep", .reason = "a live module resolves inside it" }) catch oom();
                settled = false;
                continue;
            }
            const rank = ranked.get(retired);
            if (rank != null and rank.? < keep.?.value) {
                p.actions.append(a, .{ .path = retired, .action = "keep", .reason = a.print("kept by --keep {s}", .{keep.?.text}) catch oom() }) catch oom();
                settled = false;
                continue;
            }
            if (now - w < tree_grace) {
                p.actions.append(a, .{ .path = retired, .action = "keep", .reason = a.print("within grace ({s})", .{why}) catch why }) catch oom();
                settled = false;
                continue;
            }
            const reason = if (rank == null) why else a.print("{s}, beyond --keep {s}", .{ why, keep.?.text }) catch oom();
            p.actions.append(a, .{ .path = retired, .action = "remove", .reason = reason }) catch oom();
            p.removals.append(a, .{ .mod = mi, .action = p.actions.items.len - 1 }) catch oom();
        }
        if (settled) p.generation = generationAction(c, state_root, j, roots, include_failed);
        planned.append(a, p) catch oom();
    }
    var paths: std.ArrayList([]const u8) = .empty;
    for (planned.items) |p| {
        for (p.removals.items) |r| paths.append(a, p.actions.items[r.action].path) catch oom();
        if (p.generation) |g| if (std.mem.eql(u8, g.action, "remove")) paths.append(a, g.path) catch oom();
    }
    const used = if (check_in_use) inuse.users(a, std.heap.page_allocator, paths.items) else none: {
        const none = a.alloc(?[]const u8, paths.items.len) catch oom();
        @memset(none, null);
        break :none none;
    };
    var next: usize = 0;
    var actions: std.ArrayList(Action) = .empty;
    for (planned.items) |*p| {
        var j = journals[p.journal];
        var changed = false;
        var settled = true;
        for (p.removals.items) |r| {
            const act = &p.actions.items[r.action];
            const user = used[next];
            next += 1;
            if (user) |why| {
                act.action = "keep";
                act.reason = a.print("in use on this node: {s}", .{why}) catch oom();
                settled = false;
                continue;
            }
            if (dry_run) continue;
            removeTree(c, act.path, dirname(act.path));
            p.mods[r.mod] = dictSet(a, p.mods[r.mod], "retired_removed_at", .{ .float = now });
            changed = true;
        }
        var generation_removed = false;
        if (p.generation) |planned_generation| {
            var g = planned_generation;
            const scanned = std.mem.eql(u8, g.action, "remove");
            const user = if (scanned) used[next] else null;
            if (scanned) next += 1;
            if (settled) {
                if (user) |why| {
                    g.action = "keep";
                    g.reason = a.print("in use on this node: {s}", .{why}) catch oom();
                } else if (scanned and !dry_run) {
                    removeTree(c, g.path, dirname(g.path));
                    generation_removed = true;
                }
                p.actions.append(a, g) catch oom();
            }
        }
        actions.appendSlice(a, p.actions.items) catch oom();
        if (generation_removed) j = dictSet(a, j, "generation_removed_at", .{ .float = now });
        if (changed or generation_removed) {
            if (changed) j = dictSet(a, j, "modules_plan", .{ .list = p.mods });
            const message = if (changed and generation_removed) "retired tree and generation removed" else if (changed) "retired tree removed" else "generation removed";
            j = dictSet(a, j, "message", Py.s(message));
            const rid = str(pyfmt.get(j, "run_id")) orelse "";
            j = saveJournal(c, journalPath(a, state_root, rid), j, "journal.gc");
            journals = blk: {
                const copy = a.dupe(Py, journals) catch break :blk journals;
                copy[p.journal] = j;
                break :blk copy;
            };
        }
    }
    // Stable software directories named by retired module files: reported,
    // never deleted.
    var latest: std.StringArrayHashMapUnmanaged(f64) = .empty;
    for (journals) |j| {
        if (!eqlStr(pyfmt.get(j, "kind"), "retire") or !eqlStr(pyfmt.get(j, "state"), "retired")) continue;
        const dir = abspath(a, str(pyfmt.get(j, "software_dir")) orelse continue);
        const w = num(pyfmt.get(j, "retired_at")) orelse continue;
        const r = latest.getOrPut(a, dir) catch continue;
        r.value_ptr.* = if (r.found_existing) @max(r.value_ptr.*, w) else w;
    }
    const dirs = a.dupe([]const u8, latest.keys()) catch &.{};
    std.mem.sort([]const u8, @constCast(dirs), {}, struct {
        fn lt(_: void, x: []const u8, y: []const u8) bool {
            return json.pyTextLess(x, y);
        }
    }.lt);
    for (dirs) |dir| {
        const st = fsx.statAt(fsx.cwd, dir, true) catch continue;
        if (!st.isDir()) continue;
        const ready = latest.get(dir).? + grace;
        if (referenced(c, dir, roots)) {
            actions.append(a, .{ .path = dir, .action = "keep", .reason = "a live module resolves inside it" }) catch oom();
        } else if (now < ready) {
            actions.append(a, .{ .path = dir, .action = "keep", .reason = a.print("within grace (retired module); ready to remove by hand after {s}", .{gmtimeStamp(a, ready, true)}) catch "" }) catch oom();
        } else {
            actions.append(a, .{ .path = dir, .action = "manual", .reason = a.print("ready to remove by hand after {s}; gc does not delete the stable software directory", .{gmtimeStamp(a, ready, true)}) catch "" }) catch oom();
        }
    }
    for (actions.items) |act| cli.out("{s}: {s} ({s})\n", .{ act.action, act.path, act.reason });
    return 0;
}

test "runGrace uses the hours a run recorded, when they are a number at least 0" {
    const Case = struct { value: ?Py, want: f64 };
    const cases = [_]Case{
        .{ .value = .{ .float = 0 }, .want = 0 },
        .{ .value = .{ .int = 2 }, .want = 2 },
        .{ .value = .{ .float = 2.5 }, .want = 2.5 },
        .{ .value = .{ .float = -5 }, .want = 144 },
        .{ .value = .{ .int = -1 }, .want = 144 },
        .{ .value = .{ .float = std.math.nan(f64) }, .want = 144 },
        .{ .value = .{ .bool = true }, .want = 144 },
        .{ .value = Py.s("0"), .want = 144 },
        .{ .value = .null, .want = 144 },
        .{ .value = null, .want = 144 },
    };
    for (cases) |case| {
        const j: Py = if (case.value) |v| .{ .dict = &.{.{ .key = "grace_hours", .value = v }} } else .{ .dict = &.{} };
        try std.testing.expectEqual(case.want, runGrace(j, 144));
    }
}

test "finishedRun: a complete or rolled-back run, and a failed one only with include_failed" {
    const complete = [_]Py.Entry{.{ .key = "state", .value = Py.s("complete") }};
    const rolled_back = [_]Py.Entry{.{ .key = "state", .value = Py.s("rolled_back") }};
    const failed = [_]Py.Entry{.{ .key = "state", .value = Py.s("failed") }};
    const building = [_]Py.Entry{.{ .key = "state", .value = Py.s("building") }};
    try std.testing.expectEqualStrings("complete run", finishedRun(.{ .dict = &complete }, false).?);
    try std.testing.expectEqualStrings("rolled-back run", finishedRun(.{ .dict = &rolled_back }, false).?);
    try std.testing.expectEqual(@as(?[]const u8, null), finishedRun(.{ .dict = &failed }, false));
    try std.testing.expectEqualStrings("failed run", finishedRun(.{ .dict = &failed }, true).?);
    try std.testing.expectEqual(@as(?[]const u8, null), finishedRun(.{ .dict = &building }, true));
    try std.testing.expectEqual(@as(?[]const u8, null), finishedRun(.{ .dict = &.{} }, true));
}

test "linksTo finds the links that resolve to a module file beside it and in each class directory" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const root = try a.print("/tmp/esr-links-{d}", .{linux.getpid()});
    defer fsx.deleteTree(a, fsx.cwd, root) catch {};
    const module = try a.print("{s}/modules/all/demo/1.lua", .{root});
    for ([_][]const u8{ "all/demo", "tools/demo", "lib/demo", "other" }) |d| try atomic.makedirs(try a.print("{s}/modules/{s}", .{ root, d }));
    sys.close(try sys.openat(fsx.cwd, module, .{ .ACCMODE = .WRONLY, .CREAT = true }, 0o644));
    const links = [_][2][]const u8{
        .{ module, "modules/tools/demo/1.lua" },
        .{ "../../all/demo/1.lua", "modules/lib/demo/1.lua" },
        .{ "1.lua", "modules/all/demo/default" },
        .{ "../all/demo/2.lua", "modules/tools/demo/2.lua" },
        .{ module, "modules/other/demo-1.lua" },
    };
    for (links) |l| {
        var zt: [sys.path_max:0]u8 = undefined;
        var zl: [sys.path_max:0]u8 = undefined;
        _ = try sys.call(linux.symlinkat, .{ (try sys.z(&zt, l[0])).ptr, linux.AT.FDCWD, (try sys.z(&zl, try a.print("{s}/{s}", .{ root, l[1] }))).ptr });
    }
    const found = linksTo(a, module);
    try std.testing.expectEqual(@as(usize, 3), found.len);
    for (found, [_][]const u8{ "modules/all/demo/default", "modules/lib/demo/1.lua", "modules/tools/demo/1.lua" }) |got, want| {
        try std.testing.expectEqualStrings(try a.print("{s}/{s}", .{ root, want }), got);
    }
}
