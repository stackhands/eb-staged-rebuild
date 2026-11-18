//! run: build a complete staged install, then swap it with the live
//! directory. This file checks the roots, picks the backend and prints the
//! dry run; run.zig builds, publishes, resumes and rolls back.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const probe = @import("probe.zig");
const pyfmt = @import("pyfmt.zig");
const run_mod = @import("run.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

fn refuse(comptime f: []const u8, args: anytype) noreturn {
    main_mod.refuse(f, args);
}

fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

/// What a run is asked to do: the options of `run`, as the Python tool's
/// Spec holds them.
pub const Spec = struct {
    software: []const u8,
    modules: []const u8,
    state_root: []const u8,
    eb_args: []const []const u8,
    backend: []const u8 = "auto",
    grace_hours: f64 = admin.default_grace_hours,
    dry_run: bool = false,
    module_rels: []const []const u8 = &.{},
    eb_executable: []const u8 = "eb",
    run_id: ?[]const u8 = null,
    skip_live_check: bool = false,
    lock_timeout: ?f64 = null,
    prepare_executable: ?[]const u8 = null,
    ledger_config: ?[]const u8 = null,
    build_dir: ?[]const u8 = null,
    min_free_bytes: ?u128 = null,
    require_exchange: bool = false,
};

// ---- eb arguments, as builders.py shapes them -----------------------------

const drop_flags = [_][]const u8{ "--bwrap", "--disable-bwrap", "--remove-ghost-install-dirs" };
const drop_with_value = [_][]const u8{ "--installpath", "--installpath-software", "--installpath-modules", "--prefix", "--bwrap-installpath", "--bwrap-options", "--subdir-software", "--subdir-modules" };

fn inList(list: []const []const u8, s: []const u8) bool {
    for (list) |x| if (std.mem.eql(u8, x, s)) return true;
    return false;
}

/// The arguments filter_eb_args keeps, one at a time, without allocating.
const Kept = struct {
    args: []const []const u8,
    i: usize = 0,
    drop_buildpath: bool = false,

    fn next(k: *Kept) ?[]const u8 {
        while (k.i < k.args.len) {
            const arg = k.args[k.i];
            const eq = std.mem.findScalar(u8, arg, '=');
            const name = arg[0 .. eq orelse arg.len];
            if (std.mem.eql(u8, arg, "--") or inList(&drop_flags, name)) {
                k.i += 1;
                continue;
            }
            if (inList(&drop_with_value, name) or (k.drop_buildpath and std.mem.eql(u8, name, "--buildpath"))) {
                k.i += if (eq == null) 2 else 1;
                continue;
            }
            k.i += 1;
            return arg;
        }
        return null;
    }
};

/// filter_eb_args: drop install-path and bubblewrap options.
pub fn filterEbArgs(a: Allocator, args: []const []const u8) []const []const u8 {
    return filterEbArgsOpts(a, args, false);
}

/// filterEbArgsOpts: filter_eb_args, optionally dropping --buildpath too.
pub fn filterEbArgsOpts(a: Allocator, args: []const []const u8, drop_buildpath: bool) []const []const u8 {
    var kept: std.ArrayList([]const u8) = .empty;
    var it: Kept = .{ .args = args, .drop_buildpath = drop_buildpath };
    while (it.next()) |arg| kept.append(a, arg) catch oom();
    return kept.items;
}

/// modules_suffix: the suffix EasyBuild adds to its module install path.
/// The last --suffix-modules-path that filter_eb_args keeps, else
/// EASYBUILD_SUFFIX_MODULES_PATH (env_value), else "all", as EasyBuild
/// reads them. One set in a configuration file is not seen.
pub fn modulesSuffix(args: []const []const u8, env_value: ?[]const u8) []const u8 {
    const option = "--suffix-modules-path";
    var suffix = env_value orelse "all";
    var it: Kept = .{ .args = args };
    while (it.next()) |arg| {
        if (std.mem.startsWith(u8, arg, option ++ "=")) {
            suffix = arg[option.len + 1 ..];
        } else if (std.mem.eql(u8, arg, option)) {
            if (it.next()) |value| suffix = value;
        }
    }
    return suffix;
}

/// module_path: the directory of module files that EasyBuild puts on
/// MODULEPATH for the module install path modules.
pub fn modulePath(a: Allocator, modules: []const u8, suffix: []const u8) Allocator.Error![]const u8 {
    if (std.mem.startsWith(u8, suffix, "/")) return suffix;
    if (suffix.len == 0) return modules;
    return std.fs.path.join(a, &.{ modules, suffix });
}

/// ensure_rebuild: add --rebuild unless --rebuild, --force or -f is there.
pub fn ensureRebuild(a: Allocator, args: []const []const u8) []const []const u8 {
    for (args) |arg| {
        const name = arg[0 .. std.mem.findScalar(u8, arg, '=') orelse arg.len];
        if (std.mem.eql(u8, name, "--rebuild") or std.mem.eql(u8, name, "--force") or std.mem.eql(u8, name, "-f")) return args;
    }
    var out = a.dupe([]const u8, args) catch return args;
    out = a.realloc(out, out.len + 1) catch return args;
    out[out.len - 1] = "--rebuild";
    return out;
}

fn join(a: Allocator, parts: []const []const u8) []const u8 {
    return std.mem.join(a, " ", parts) catch "";
}

fn which(a: Allocator, env: probe.Env, name: []const u8) ?[]const u8 {
    return common.proc.which(a, name, env.get("PATH") orelse "/bin:/usr/bin") catch null;
}

const selfExe = probe.selfExe;

/// The build command each backend runs, as the dry run prints it.
fn buildCommand(a: Allocator, env: probe.Env, spec: Spec, probes: Py, generation_root: []const u8) []const u8 {
    const filtered = ensureRebuild(a, filterEbArgsOpts(a, spec.eb_args, spec.build_dir != null));
    var cmd: std.ArrayList([]const u8) = .empty;
    if (std.mem.eql(u8, spec.backend, "bwrap")) {
        cmd.appendSlice(a, &.{ spec.eb_executable, "--experimental", "--bwrap", a.print("--bwrap-installpath={s}", .{generation_root}) catch "", "--bwrap-options=--die-with-parent" }) catch oom();
        if (spec.build_dir) |bd| cmd.append(a, a.print("--buildpath={s}", .{bd}) catch "") catch oom();
        cmd.appendSlice(a, filtered) catch oom();
        return join(a, cmd.items);
    }
    const software = std.fs.path.join(a, &.{ generation_root, "software" }) catch "";
    const modules = std.fs.path.join(a, &.{ generation_root, "modules" }) catch "";
    var inner: std.ArrayList([]const u8) = .empty;
    inner.appendSlice(a, &.{ spec.eb_executable, a.print("--installpath-software={s}", .{software}) catch "", a.print("--installpath-modules={s}", .{modules}) catch "" }) catch oom();
    if (spec.build_dir) |bd| inner.append(a, a.print("--buildpath={s}", .{bd}) catch "") catch oom();
    inner.appendSlice(a, filtered) catch oom();
    if (!std.mem.eql(u8, spec.backend, "userns")) return join(a, inner.items);
    const report = pyfmt.get(probes, "userns_sandbox");
    const unshare = blk: {
        if (report) |r| if (pyfmt.get(r, "path")) |p| if (p == .str and p.str.len > 0) break :blk p.str;
        break :blk which(a, env, "unshare") orelse "unshare";
    };
    cmd.appendSlice(a, &.{ unshare, "--user", "--map-root-user", "--mount", "--fork", "--kill-child", selfExe(a), probe.sandbox_command, "--read-only", spec.software, "--read-only", spec.modules, "--" }) catch oom();
    cmd.appendSlice(a, inner.items) catch oom();
    return join(a, cmd.items);
}

// ---- checks -------------------------------------------------------------

/// assert_real_components: the first existing component that is a symlink.
fn symlinkComponent(a: Allocator, path: []const u8) ?[]const u8 {
    var current = admin.abspath(a, path);
    while (true) {
        if (std.mem.eql(u8, main_mod.lstatKind(current), "symlink")) return current;
        const parent = std.fs.path.dirname(current) orelse return null;
        if (std.mem.eql(u8, parent, current)) return null;
        current = parent;
    }
}

fn realpathDepth(a: Allocator, path: []const u8, depth: usize) Allocator.Error![]const u8 {
    var rb: [sys.path_max]u8 = undefined;
    if (fsx.realpath(fsx.cwd, path, &rb)) |r| return a.dupe(u8, r) else |_| {}
    if (depth < 40 and std.mem.eql(u8, main_mod.lstatKind(path), "symlink")) {
        var zb: [sys.path_max:0]u8 = undefined;
        var lb: [sys.path_max]u8 = undefined;
        if (sys.z(&zb, path)) |z| {
            if (sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &lb, lb.len })) |n| {
                const target = lb[0..n];
                const joined = if (target.len > 0 and target[0] == '/')
                    target
                else
                    try std.fs.path.join(a, &.{ std.fs.path.dirname(path) orelse "/", target });
                return realpathDepth(a, admin.abspath(a, joined), depth + 1);
            } else |_| {}
        } else |_| {}
    }
    const parent = std.fs.path.dirname(path) orelse return path;
    if (std.mem.eql(u8, parent, path)) return path;
    return std.fs.path.join(a, &.{ try realpathDepth(a, parent, depth), std.fs.path.basename(path) });
}

fn realpathLoose(a: Allocator, path: []const u8) Allocator.Error![]const u8 {
    return realpathDepth(a, path, 0);
}

/// _check_roots: refuse a software or modules path that is not a real
/// directory, a state root with a symbolic link in its path, an unknown
/// backend, a preparation program that is not an absolute path, and a
/// build directory inside the generation or the live prefix.
pub fn checkRoots(a: Allocator, spec: Spec) void {
    if (spec.prepare_executable) |p| {
        if (p.len == 0) refuse("publication preparation executable must not be empty", .{});
        if (p[0] != '/') refuse("publication preparation executable must be an absolute path", .{});
    }
    const sk = main_mod.lstatKind(spec.software);
    if (!std.mem.eql(u8, sk, "dir")) {
        if (std.mem.eql(u8, sk, "symlink")) refuse("software path is a symlink ({s}); Name/version must stay a real directory", .{spec.software});
        refuse("software path is not a real directory: {s} ({s})", .{ spec.software, sk });
    }
    const mk = main_mod.lstatKind(spec.modules);
    if (!std.mem.eql(u8, mk, "dir")) {
        if (std.mem.eql(u8, mk, "symlink")) refuse("modules path is a symlink: {s}", .{spec.modules});
        refuse("modules path is not a real directory: {s} ({s})", .{ spec.modules, mk });
    }
    if (symlinkComponent(a, spec.state_root)) |s| refuse("state root is unsafe: {s} is a symlink", .{s});
    const known = [_][]const u8{ "auto", "installpath", "bwrap", "userns" };
    if (!inList(&known, spec.backend)) refuse("unknown backend {s}", .{spec.backend});
    if (spec.build_dir) |bd| {
        if (bd.len == 0) refuse("--build-dir must name a directory; it is empty", .{});
        const build_abs = admin.abspath(a, bd);
        const build_real = realpathLoose(a, build_abs) catch oom();
        const gen_abs = std.fs.path.join(a, &.{ admin.abspath(a, spec.state_root), "generations" }) catch oom();
        const gen_real = realpathLoose(a, gen_abs) catch oom();
        if (admin.isWithin(a, build_abs, gen_abs) or admin.isWithin(a, build_real, gen_real))
            refuse("build directory {s} is inside the generation root {s}", .{ build_abs, gen_abs });
        const sw_abs = admin.abspath(a, spec.software);
        const sw_real = realpathLoose(a, sw_abs) catch oom();
        if (admin.isWithin(a, build_abs, sw_abs) or admin.isWithin(a, build_real, sw_real))
            refuse("build directory {s} is inside the live software prefix {s}", .{ build_abs, sw_abs });
        const mod_abs = admin.abspath(a, spec.modules);
        const mod_real = realpathLoose(a, mod_abs) catch oom();
        if (admin.isWithin(a, build_abs, mod_abs) or admin.isWithin(a, build_real, mod_real))
            refuse("build directory {s} is inside the live modules prefix {s}", .{ build_abs, mod_abs });
        if (fsx.statAt(fsx.cwd, build_abs, true)) |st| {
            if (!st.isDir()) refuse("build directory {s} is not a directory", .{build_abs});
        } else |_| {}
    }
}

fn usable(probes: Py, key: []const u8) bool {
    const r = pyfmt.get(probes, key) orelse return false;
    return pyfmt.truthy(pyfmt.get(r, "usable"));
}

fn strOr(v: ?Py, fallback: []const u8) []const u8 {
    if (pyfmt.truthy(v) and v.? == .str) return v.?.str;
    return fallback;
}

fn exchangeOk(probes: Py) bool {
    const exchange = pyfmt.get(probes, "rename_exchange") orelse return false;
    return pyfmt.truthy(pyfmt.get(exchange, "ok"));
}

fn requiredExchangeProblem(a: Allocator, probes: Py) []const u8 {
    const exchange = pyfmt.get(probes, "rename_exchange") orelse Py{ .dict = &.{} };
    return a.print(
        "RENAME_EXCHANGE is required on {s}, but the probe failed: {s}",
        .{
            strOr(pyfmt.get(exchange, "fstype"), "this filesystem"),
            strOr(pyfmt.get(exchange, "error"), "the operation is not supported"),
        },
    ) catch "RENAME_EXCHANGE is required, but the probe failed";
}

test "required exchange reports the failed filesystem probe" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const exchange: Py = .{ .dict = &.{
        .{ .key = "ok", .value = .{ .bool = false } },
        .{ .key = "fstype", .value = Py.s("testfs") },
        .{ .key = "error", .value = Py.s("not supported here") },
    } };
    const probes: Py = .{ .dict = &.{.{ .key = "rename_exchange", .value = exchange }} };
    try std.testing.expect(!exchangeOk(probes));
    try std.testing.expectEqualStrings(
        "RENAME_EXCHANGE is required on testfs, but the probe failed: not supported here",
        requiredExchangeProblem(a, probes),
    );
}

// ---- run ----------------------------------------------------------------

/// run: check the roots and make them absolute, probe, pick the backend,
/// then print the dry run or hand the run to run.zig. Returns the exit status.
pub fn run(a: Allocator, env: probe.Env, spec_in: Spec) u8 {
    var spec = spec_in;
    if (spec.run_id) |run_id| if (!admin.plainRunId(run_id))
        refuse("run id is not a plain name: '{s}'", .{run_id});
    checkRoots(a, spec);
    spec.software = admin.abspath(a, spec.software);
    spec.modules = admin.abspath(a, spec.modules);
    spec.state_root = admin.abspath(a, spec.state_root);
    if (spec.build_dir) |bd| spec.build_dir = admin.abspath(a, bd);
    const probes = probe.probeAll(a, env, spec.software, spec.eb_executable) catch |err| refuse("{s}", .{main_mod.osError(a, err, spec.software)});
    if (std.mem.eql(u8, spec.backend, "auto")) {
        spec.backend = if (usable(probes, "bwrap")) "bwrap" else if (usable(probes, "userns_sandbox")) "userns" else "installpath";
        if (std.mem.eql(u8, spec.backend, "installpath"))
            log("neither bubblewrap nor the user-namespace sandbox is usable; using the unsandboxed install-path fallback", .{});
    }
    if (spec.dry_run) return dryRun(a, env, spec, probes);
    if (spec.require_exchange and !exchangeOk(probes))
        refuse("{s}", .{requiredExchangeProblem(a, probes)});
    var ctx: admin.Ctx = .{ .a = a, .env = env, .pause = .{ .env = env } };
    var r: run_mod.R = .{ .a = a, .env = env, .ctx = &ctx, .spec = spec };
    const res = run_mod.run(&r, probes) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{r.why}),
    };
    return res.code;
}

fn dryRun(a: Allocator, env: probe.Env, spec: Spec, probes: Py) u8 {
    const bwrap = pyfmt.get(probes, "bwrap") orelse Py{ .dict = &.{} };
    const easybuild = pyfmt.get(probes, "easybuild") orelse Py{ .dict = &.{} };
    const exchange = pyfmt.get(probes, "rename_exchange") orelse Py{ .dict = &.{} };
    const ns = pyfmt.get(probes, "user_namespaces") orelse Py{ .dict = &.{} };
    var maxns: std.ArrayList(u8) = .empty;
    pyfmt.str(&maxns, a, pyfmt.get(ns, "max_user_namespaces")) catch oom();
    var lines: std.ArrayList([]const u8) = .empty;
    const fmt = struct {
        fn f(al: Allocator, comptime s: []const u8, args: anytype) []const u8 {
            return al.print(s, args) catch "";
        }
    }.f;
    lines.append(a, fmt(a, "backend: {s}", .{spec.backend})) catch oom();
    lines.append(a, fmt(a, "software: {s}", .{admin.abspath(a, spec.software)})) catch oom();
    lines.append(a, fmt(a, "modules: {s}", .{admin.abspath(a, spec.modules)})) catch oom();
    lines.append(a, fmt(a, "state root: {s}", .{admin.abspath(a, spec.state_root)})) catch oom();
    lines.append(a, fmt(a, "bwrap usable: {s} ({s})", .{
        if (usable(probes, "bwrap")) "True" else "False",
        strOr(pyfmt.get(bwrap, "reason"), strOr(pyfmt.get(bwrap, "version"), "present")),
    })) catch oom();
    lines.append(a, fmt(a, "user.max_user_namespaces: {s}", .{maxns.items})) catch oom();
    lines.append(a, fmt(a, "renameat2 RENAME_EXCHANGE on {s}: {s}", .{
        strOr(pyfmt.get(exchange, "fstype"), "this filesystem"),
        if (pyfmt.truthy(pyfmt.get(exchange, "ok"))) "yes" else "no",
    })) catch oom();
    lines.append(a, fmt(a, "easybuild: {s}", .{strOr(pyfmt.get(easybuild, "version_text"), strOr(pyfmt.get(easybuild, "reason"), "not found"))})) catch oom();
    lines.append(a, "lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured") catch oom();
    lines.append(
        a,
        if (spec.require_exchange)
            "publish: renameat2(RENAME_EXCHANGE) required; ordinary-rename fallback disabled"
        else
            "publish: renameat2(RENAME_EXCHANGE), with a restoring ordinary-rename fallback",
    ) catch oom();
    var problems: std.ArrayList([]const u8) = .empty;
    if (std.mem.eql(u8, spec.backend, "bwrap") and !usable(probes, "bwrap"))
        problems.append(a, strOr(pyfmt.get(bwrap, "reason"), "bwrap is not usable")) catch oom();
    if (spec.require_exchange and !exchangeOk(probes))
        problems.append(a, requiredExchangeProblem(a, probes)) catch oom();
    for (spec.module_rels) |rel| {
        const path = std.fs.path.join(a, &.{ spec.software, rel }) catch continue;
        if (std.mem.eql(u8, main_mod.lstatKind(path), "symlink")) problems.append(a, fmt(a, "{s} is a symlink", .{path})) catch oom();
    }
    const gen = std.fs.path.join(a, &.{ spec.state_root, "generations", "<run-id>" }) catch "";
    lines.append(a, fmt(a, "command: {s}", .{buildCommand(a, env, spec, probes, gen)})) catch oom();
    if (problems.items.len > 0) {
        log("dry-run would refuse: {s}", .{std.mem.join(a, "; ", problems.items) catch ""});
        for (lines.items) |l| log("{s}", .{l});
        return 2;
    }
    log("dry-run: no build, no module file written", .{});
    for (lines.items) |l| log("{s}", .{l});
    return 0;
}

test "filterEbArgs and ensureRebuild" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const kept = filterEbArgs(a, &.{ "--", "x.eb", "--prefix", "/p", "--installpath=/i", "--bwrap", "--robot" });
    try std.testing.expectEqual(@as(usize, 2), kept.len);
    try std.testing.expectEqualStrings("--robot", kept[1]);
    const r = ensureRebuild(a, kept);
    try std.testing.expectEqualStrings("--rebuild", r[2]);
    try std.testing.expectEqual(@as(usize, 2), ensureRebuild(a, &.{ "x.eb", "-f" }).len);
}

test "filterEbArgsOpts drops --buildpath only when requested" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const raw = [_][]const u8{ "x.eb", "--buildpath=/old", "--buildpath", "/old2", "--robot" };
    const kept_default = filterEbArgsOpts(a, &raw, false);
    try std.testing.expectEqual(@as(usize, 5), kept_default.len);
    const kept_drop = filterEbArgsOpts(a, &raw, true);
    try std.testing.expectEqual(@as(usize, 2), kept_drop.len);
    try std.testing.expectEqualStrings("--robot", kept_drop[1]);
}

test "buildCommand includes --buildpath only when build_dir is set" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const env: probe.Env = .{ .envp = &[_:null]?[*:0]const u8{"PATH=/bin"} };
    const probes: Py = .{ .dict = &.{} };
    const without = buildCommand(a, env, .{ .software = "/s", .modules = "/m", .state_root = "/st", .eb_args = &.{ "x.eb", "--buildpath=/keep" }, .backend = "installpath" }, probes, "/st/generations/g1");
    try std.testing.expect(std.mem.indexOf(u8, without, "--buildpath=/keep") != null);
    const with_ip = buildCommand(a, env, .{ .software = "/s", .modules = "/m", .state_root = "/st", .eb_args = &.{ "x.eb", "--buildpath=/drop" }, .backend = "installpath", .build_dir = "/tmp/bd" }, probes, "/st/generations/g1");
    try std.testing.expect(std.mem.indexOf(u8, with_ip, "--buildpath=/tmp/bd") != null);
    try std.testing.expect(std.mem.indexOf(u8, with_ip, "/drop") == null);
    const with_bw = buildCommand(a, env, .{ .software = "/s", .modules = "/m", .state_root = "/st", .eb_args = &.{"x.eb"}, .backend = "bwrap", .build_dir = "/tmp/bd" }, probes, "/st/generations/g1");
    try std.testing.expect(std.mem.indexOf(u8, with_bw, "--bwrap") != null);
    try std.testing.expect(std.mem.indexOf(u8, with_bw, "--buildpath=/tmp/bd") != null);
}
