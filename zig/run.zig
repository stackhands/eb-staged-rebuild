//! run, resume and rollback of a run journal: build into a generation
//! under the state root, check the live tree did not change, plan each
//! module, optionally run a publication-preparation program, then exchange
//! each staged tree with its live directory and write the module files;
//! with a ledger configuration, publish.zig records each stage. Every step
//! is journaled before it happens; a failure while publishing puts the old
//! live trees back.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const engine = @import("engine.zig");
const probe = @import("probe.zig");
const pyfmt = @import("pyfmt.zig");
const swap = @import("swap.zig");
const livecheck = @import("livecheck.zig");
const discover = @import("discover.zig");
const modulefile = @import("modulefile.zig");
const builders = @import("builders.zig");
const publish_mod = @import("publish.zig");
const prefix_mod = @import("prefix.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const state = common.state;
const rootlock = @import("rootlock.zig");
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// Refused and NeedsIntervention are the Python tool's own errors; Os is an
/// OSError it does not catch on the way up, which the command line reports
/// the same way (exit 2) but which leaves the journal as it was.
pub const E = error{ Refused, NeedsIntervention, Os, OutOfMemory };

fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

/// A command's outcome: its exit status and, for a failure, its message.
pub const Result = struct { code: u8, message: []const u8 = "" };

/// One run in progress: its journal, kept as the Python tool keeps it.
pub const R = struct {
    a: Allocator,
    env: probe.Env,
    ctx: *admin.Ctx,
    spec: engine.Spec,
    journal: Py = .{ .dict = &.{} },
    path: []const u8 = "",
    why: []const u8 = "",

    fn faults(r: *R) atomic.Faults {
        return r.ctx.faults();
    }

    fn fail(r: *R, comptime which_err: E, comptime f: []const u8, args: anytype) E {
        r.why = r.a.print(f, args) catch "out of memory";
        return which_err;
    }

    fn refuse(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.Refused, f, args);
    }

    fn osFail(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.Os, f, args);
    }

    fn intervene(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.NeedsIntervention, f, args);
    }

    /// The journal's value of `k`, or null.
    pub fn get(r: *R, k: []const u8) ?Py {
        return pyfmt.get(r.journal, k);
    }

    /// Set `k` in the journal held in memory; saving it is a step of its own.
    pub fn set(r: *R, k: []const u8, v: Py) void {
        r.journal = dictSet(r.a, r.journal, k, v);
    }

    fn str(r: *R, k: []const u8) []const u8 {
        const v = r.get(k) orelse return "";
        return if (v == .str) v.str else "";
    }

    fn list(r: *R, k: []const u8) []Py {
        const v = r.get(k) orelse return &.{};
        if (v != .list) return &.{};
        return @constCast(v.list);
    }

    fn setItem(r: *R, k: []const u8, i: usize, field: []const u8, v: Py) void {
        const items = r.a.dupe(Py, r.list(k)) catch return;
        items[i] = dictSet(r.a, items[i], field, v);
        r.set(k, .{ .list = items });
    }

    fn save(r: *R, step: []const u8) E!void {
        try saveJournal(r, r.path, step, r.faults());
    }

    /// The journal save on a restore path. Python passes a fresh
    /// FaultInjector() there, which records no steps but still reads the
    /// environment's pause and fail hooks; so does this.
    fn saveRestore(r: *R, step: []const u8) E!void {
        try saveJournal(r, r.path, step, r.faults());
    }
};

fn checkStageFilesystem(r: *R, rels: []const []const u8) E!void {
    for (rels) |rel| {
        const target = try std.fs.path.join(r.a, &.{ r.spec.software, rel });
        const target_parent = std.fs.path.dirname(target) orelse "/";
        if (!admin.sameFilesystem(r.ctx, r.spec.state_root, target_parent))
            return r.refuse(
                "the state root and live software tree are on different filesystems; place --state-root beside the software tree",
                .{},
            );
    }
}

fn dictSet(a: Allocator, d: Py, key: []const u8, value: Py) Py {
    var out: std.ArrayList(Py.Entry) = .empty;
    var found = false;
    if (d == .dict) for (d.dict) |e| {
        if (std.mem.eql(u8, e.key, key)) {
            out.append(a, .{ .key = e.key, .value = value }) catch oom();
            found = true;
        } else out.append(a, e) catch oom();
    };
    if (!found) out.append(a, .{ .key = key, .value = value }) catch oom();
    return .{ .dict = out.items };
}

fn s(v: ?Py) []const u8 {
    const x = v orelse return "";
    return if (x == .str) x.str else "";
}

fn now() f64 {
    return admin.nowSeconds();
}

fn kind(p: []const u8) []const u8 {
    return main_mod.lstatKind(p);
}

fn isKind(p: []const u8, k: []const u8) bool {
    return std.mem.eql(u8, kind(p), k);
}

fn readBytes(r: *R, p: []const u8) E![]const u8 {
    const f = fsx.openRegular(fsx.cwd, p, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, p)});
    defer sys.close(f.fd);
    return fsx.readAll(r.a, f.fd, 1 << 30) catch |err| r.refuse("{s}", .{main_mod.osError(r.a, err, p)});
}

fn sha(a: Allocator, data: []const u8) []const u8 {
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(data, &out, .{});
    const hex = std.fmt.bytesToHex(out, .lower);
    return a.dupe(u8, &hex) catch "";
}

/// Reach `step`; a step a test made fail is refused with the text of
/// Python's OSError(EIO, "injected failure at STEP").
fn hit(r: *R, faults: atomic.Faults, step: []const u8) E!void {
    if (faults.fails(step)) return r.osFail("[Errno 5] injected failure at {s}", .{step});
}

fn write(r: *R, path: []const u8, data: []const u8, step: []const u8, metadata_from: ?[]const u8, faults: atomic.Faults) E!void {
    var why: atomic.Why = .{};
    // atomic_write raises Refused for what it refuses and OSError for what
    // the system refused; a failure a test injected carries OSError's text.
    atomic.atomicWrite(r.a, path, data, .{ .faults = faults, .step = step, .metadata_from = metadata_from }, &why) catch |err| {
        if (err == error.Refused and !std.mem.startsWith(u8, why.msg, "[Errno ")) return r.refuse("{s}", .{why.msg});
        return r.osFail("{s}", .{if (err == error.Refused) why.msg else main_mod.osError(r.a, err, path)});
    };
}

fn saveJournal(r: *R, path: []const u8, step: []const u8, faults: atomic.Faults) E!void {
    if (r.get("ledger_policy")) |lp| if (pyfmt.truthy(lp)) {
        r.set("schema", .{ .int = 4 });
        r.set("publication", Py.s("directory-swap"));
    } else {
        r.set("schema", .{ .int = 2 });
    } else r.set("schema", .{ .int = 2 });
    const p = r.get("publication");
    if (p == null or p.? == .null) r.set("publication", Py.s("directory-swap"));
    const body = json.dumps(r.a, r.journal, 2, true) catch return error.OutOfMemory;
    const raw = r.a.print("{s}\n", .{body}) catch return error.OutOfMemory;
    try write(r, path, raw, step, null, faults);
}

fn joinCmd(a: Allocator, cmd: []const []const u8) []const u8 {
    return std.mem.join(a, " ", cmd) catch "";
}

fn pyList(a: Allocator, items: []const []const u8) Py {
    const out = a.alloc(Py, items.len) catch return .{ .list = &.{} };
    for (items, 0..) |x, i| out[i] = Py.s(x);
    return .{ .list = out };
}

fn nowStampRunId(a: Allocator) []const u8 {
    const t = now();
    const secs: u64 = @intFromFloat(@floor(t));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    return a.print("{d:0>4}{d:0>2}{d:0>2}-{d:0>2}{d:0>2}{d:0>2}", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

fn runDir(a: Allocator, state_root: []const u8, run_id: []const u8) []const u8 {
    return std.fs.path.join(a, &.{ state_root, "runs", run_id }) catch "";
}

// ---- the environment eb runs with -------------------------------------------

/// child_env: the install-path and ghost-removal variables replaced, and the
/// module files of the live module tree, all/ under it unless the eb
/// arguments or the environment give EasyBuild another suffix, first on
/// MODULEPATH. Running eb without these would let it install into its
/// default path, so an allocation failure is an error, never a fallback to
/// the parent environment.
fn childEnv(r: *R, env: probe.Env, software: []const u8, modules: []const u8, live_modules: []const u8) E!common.proc.Envp {
    var b = try childEnvBuilder(r, env, software, modules, live_modules, r.spec.build_dir != null);
    return envpOf(r, &b);
}

/// The environment of the dry runs: the build's, with
/// EASYBUILD_OUTPUT_STYLE=basic. With rich, EasyBuild wraps its output at
/// the console width and, once FORCE_COLOR is set and not empty, colours it
/// through a pipe too; basic output, which EasyBuild reads since 4.5.0, has
/// neither, and --output-style among the eb arguments still wins.
fn dryRunEnv(r: *R) E!common.proc.Envp {
    var b = try childEnvBuilder(r, r.env, r.spec.software, r.spec.modules, r.spec.modules, false);
    try b.set("EASYBUILD_OUTPUT_STYLE", "basic");
    return envpOf(r, &b);
}

/// The environment childEnv gives, before it becomes an envp.
fn childEnvBuilder(r: *R, env: probe.Env, software: []const u8, modules: []const u8, live_modules: []const u8, drop_buildpath: bool) E!common.env.Builder {
    const a = r.a;
    var b = try common.env.Builder.fromEnv(a, common.env.Env.fromEnvp(env.envp));
    for ([_][]const u8{ "EASYBUILD_INSTALLPATH", "EASYBUILD_PREFIX", "EASYBUILD_REMOVE_GHOST_INSTALL_DIRS", "EASYBUILD_INSTALLPATH_SOFTWARE", "EASYBUILD_INSTALLPATH_MODULES" }) |k| b.unset(k);
    if (drop_buildpath) b.unset("EASYBUILD_BUILDPATH");
    try b.set("EASYBUILD_INSTALLPATH_SOFTWARE", software);
    try b.set("EASYBUILD_INSTALLPATH_MODULES", modules);
    const live = try engine.modulePath(a, live_modules, engine.modulesSuffix(r.spec.eb_args, b.get("EASYBUILD_SUFFIX_MODULES_PATH")));
    const mp = b.get("MODULEPATH") orelse "";
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, mp, ':');
    var present = false;
    while (it.next()) |x| if (x.len > 0) {
        try parts.append(a, x);
        if (std.mem.eql(u8, x, live)) present = true;
    };
    if (!present) {
        const joined = if (parts.items.len > 0) try a.print("{s}:{s}", .{ live, try std.mem.join(a, ":", parts.items) }) else live;
        try b.set("MODULEPATH", joined);
    }
    return b;
}

fn envpOf(r: *R, b: *common.env.Builder) E!common.proc.Envp {
    return b.envp() catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        // Paths come from argv and the environment, which cannot hold NUL.
        error.InvalidArgument => r.refuse("eb's environment would hold a NUL byte", .{}),
    };
}

fn resolveExe(a: Allocator, env: probe.Env, name: []const u8) ?[]const u8 {
    if (std.mem.findScalar(u8, name, '/') != null) return admin.abspath(a, name);
    return common.proc.which(a, name, env.get("PATH") orelse "/bin:/usr/bin") catch null;
}

// ---- learning the module list ----------------------------------------------

/// strip_escapes: text without its ANSI CSI and OSC escape sequences. With
/// rich, EasyBuild colours its output once FORCE_COLOR is set and not empty,
/// through a pipe too, unless its output style is basic; the dry runs ask
/// for basic output, but --output-style among the eb arguments wins. A
/// sequence ends on its line, and one that does not end there is kept.
fn stripEscapes(a: Allocator, text: []const u8) []const u8 {
    const esc = std.ascii.control_code.esc;
    if (std.mem.findScalar(u8, text, esc) == null) return text;
    var out: std.ArrayList(u8) = .empty;
    out.ensureTotalCapacity(a, text.len) catch oom();
    var i: usize = 0;
    while (i < text.len) {
        if (text[i] == esc) if (escapeLen(text[i..])) |n| {
            i += n;
            continue;
        };
        out.appendAssumeCapacity(text[i]);
        i += 1;
    }
    return out.items;
}

/// The length of the CSI or OSC sequence rest starts with, as ECMA-48 has
/// them: ESC [, parameter bytes 0x30-0x3F, intermediate bytes 0x20-0x2F and
/// a final byte 0x40-0x7E; or ESC ], a string without a newline, and BEL or
/// ESC \.
fn escapeLen(rest: []const u8) ?usize {
    const cc = std.ascii.control_code;
    if (rest.len < 2 or rest[0] != cc.esc) return null;
    var i: usize = 2;
    switch (rest[1]) {
        '[' => {
            while (i < rest.len and rest[i] >= 0x30 and rest[i] <= 0x3f) i += 1;
            while (i < rest.len and rest[i] >= 0x20 and rest[i] <= 0x2f) i += 1;
            return if (i < rest.len and rest[i] >= 0x40 and rest[i] <= 0x7e) i + 1 else null;
        },
        ']' => while (i < rest.len) : (i += 1) switch (rest[i]) {
            cc.bel => return i + 1,
            cc.esc => return if (i + 1 < rest.len and rest[i + 1] == '\\') i + 2 else null,
            '\n' => return null,
            else => {},
        } else return null,
        else => return null,
    }
}

/// The status of a line that starts a dry-run entry: ^\s*\*\s+\[([ xFR-])\]
fn entryStatus(line: []const u8) ?u8 {
    var i: usize = 0;
    while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
    if (i >= line.len or line[i] != '*') return null;
    i += 1;
    const ws = i;
    while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
    if (i == ws or i + 3 > line.len or line[i] != '[' or std.mem.findScalar(u8, " xFR-", line[i + 1]) == null or line[i + 2] != ']') return null;
    return line[i + 1];
}

/// The module field of a whole entry:
/// ^\s*\*\s+\[[ xFR-]\]\s+.*\(module:\s*(.+)\)\s*$
fn moduleField(line: []const u8) ?[]const u8 {
    if (entryStatus(line) == null) return null;
    var i = (std.mem.findScalar(u8, line, ']') orelse return null) + 1;
    const ws = i;
    while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
    if (i == ws) return null;
    // .*\(module:\s*(.+)\)\s*$ : the last "(module:" whose tail closes.
    const trimmed = std.mem.trimEnd(u8, line, " \t");
    if (trimmed.len == 0 or trimmed[trimmed.len - 1] != ')') return null;
    const at = std.mem.findLast(u8, trimmed[i..], "(module:") orelse return null;
    const field = std.mem.trim(u8, trimmed[i + at + "(module:".len .. trimmed.len - 1], " \t");
    return if (field.len == 0) null else field;
}

const DryRunModule = struct { status: u8, rel: []const u8 };

/// The status and Name/version of each module line of eb --dry-run. Off a
/// terminal, EasyBuild's rich output wraps at the console width; the lines
/// after an entry are joined until it names its module, or until the next
/// entry, an EasyBuild log line or an empty line.
fn dryRunModules(a: Allocator, text: []const u8) []const DryRunModule {
    var modules: std.ArrayList(DryRunModule) = .empty;
    var entry: std.ArrayList(u8) = .empty;
    var open = false;
    var it = std.mem.splitScalar(u8, stripEscapes(a, text), '\n');
    while (true) {
        const raw = it.next();
        const line = std.mem.trimEnd(u8, raw orelse "", "\r");
        if (raw != null and open and line.len > 0 and !std.mem.startsWith(u8, line, "==") and entryStatus(line) == null and moduleField(entry.items) == null) {
            entry.appendSlice(a, line) catch oom();
            continue;
        }
        if (open) {
            if (moduleField(entry.items)) |field| {
                const status = entryStatus(entry.items).?;
                if (std.mem.find(u8, field, " | ")) |bar| {
                    const sub = std.mem.trim(u8, field[0..bar], " \t");
                    const short = std.mem.trim(u8, field[bar + 3 ..], " \t");
                    if (std.fs.path.join(a, &.{ sub, short })) |rel| modules.append(a, .{ .status = status, .rel = rel }) catch oom() else |_| {}
                } else modules.append(a, .{ .status = status, .rel = a.dupe(u8, field) catch oom() }) catch oom();
            }
        }
        if (raw == null) break;
        open = entryStatus(line) != null;
        entry.clearRetainingCapacity();
        if (open) entry.appendSlice(a, line) catch oom();
    }
    return modules.items;
}

/// software_rels_from_dry_run: Name/version of each module the build
/// replaces or installs. [x] marks an installed module the build leaves
/// alone, such as an EESSI dependency that --robot lists.
fn relsFromDryRun(a: Allocator, text: []const u8) []const []const u8 {
    return relsOf(a, dryRunModules(a, text));
}

fn relsOf(a: Allocator, modules: []const DryRunModule) []const []const u8 {
    var rels: std.ArrayList([]const u8) = .empty;
    for (modules) |m| if (m.status != 'x') rels.append(a, m.rel) catch oom();
    return rels.items;
}

const building = "== building and installing";

/// modules_built_by_extended_dry_run: Name/version of each module that eb
/// --extended-dry-run builds, in order. EasyBuild prints "== building and
/// installing NAME/VERSION..." for each easyconfig it builds. A line wider
/// than the console is wrapped as in the dry run, so the lines after one
/// that starts with "==" are joined while they can still make such a line,
/// and until the "...".
fn builtByExtendedDryRun(a: Allocator, text: []const u8) []const []const u8 {
    var built: std.ArrayList([]const u8) = .empty;
    var entry: std.ArrayList(u8) = .empty;
    var open = false;
    var it = std.mem.splitScalar(u8, stripEscapes(a, text), '\n');
    while (it.next()) |raw| {
        const line = std.mem.trimEnd(u8, raw, "\r");
        if (std.mem.startsWith(u8, line, "==")) {
            entry.clearRetainingCapacity();
            entry.appendSlice(a, line) catch oom();
            open = true;
        } else if (open and std.mem.trim(u8, line, " \t").len > 0) {
            entry.appendSlice(a, line) catch oom();
        } else {
            open = false;
            continue;
        }
        const e = entry.items;
        const tail = std.mem.trimEnd(u8, e, " \t");
        if (std.mem.startsWith(u8, e, building) and std.mem.endsWith(u8, tail, "...")) {
            built.append(a, a.dupe(u8, std.mem.trim(u8, tail[building.len .. tail.len - 3], " \t")) catch oom()) catch oom();
            open = false;
        } else if (!std.mem.startsWith(u8, e, building) and !std.mem.startsWith(u8, building, e)) open = false;
    }
    return built.items;
}

/// The modules the build replaces, and those the dry run lists that it
/// leaves out.
const Learned = struct { rels: []const []const u8, left_out: []const []const u8 = &.{} };

/// The modules of found that the build installs, and the others. With
/// --robot, the dry run marks [ ] the dependencies of installed modules
/// too, such as the build dependencies of an installed toolchain, which the
/// build leaves alone. eb --extended-dry-run resolves the dependencies as
/// the build does and names each module it would build.
fn leaveOutUnbuilt(r: *R, modules: []const DryRunModule, found: []const []const u8) E!Learned {
    const a = r.a;
    var cmd: std.ArrayList([]const u8) = .empty;
    try cmd.appendSlice(a, &.{ r.spec.eb_executable, "--extended-dry-run" });
    try cmd.appendSlice(a, engine.ensureRebuild(a, engine.filterEbArgs(a, r.spec.eb_args)));
    // EasyBuild 5.4.2 creates the install lock of each easyconfig in .locks
    // under the live software tree and, in this mode, does not remove it.
    try cmd.appendSlice(a, &.{ "--stop=fetch", "--ignore-locks" });
    log("module preflight: {s}", .{joinCmd(a, cmd.items)});
    const envp = try dryRunEnv(r);
    const exe = resolveExe(a, r.env, r.spec.eb_executable) orelse return r.refuse("[Errno 2] No such file or directory: '{s}'", .{r.spec.eb_executable});
    const res = common.proc.run(a, exe, cmd.items, envp, .{ .stdout_limit = 256 << 20, .stderr_limit = 64 << 20, .timeout_ms = null, .new_session = false }) catch |err|
        return r.refuse("{s}", .{main_mod.osError(a, err, r.spec.eb_executable)});
    const code = res.term.code();
    if (code != 0) {
        const tail = std.mem.trim(u8, if (res.stderr.len > 0) res.stderr else res.stdout, " \t\n\r");
        return r.refuse("eb --extended-dry-run exited {d}, so it is not known which modules marked [ ] the build installs. {s}", .{ code, tail });
    }
    const built = builtByExtendedDryRun(a, try a.print("{s}\n{s}", .{ res.stdout, res.stderr }));
    var unnamed: std.ArrayList([]const u8) = .empty;
    for (modules) |m| if ((m.status == 'R' or m.status == 'F') and !contains(built, m.rel)) try unnamed.append(a, m.rel);
    if (built.len == 0 or unnamed.items.len > 0)
        return r.refuse("eb --extended-dry-run did not name {s} among the modules it builds; refusing to build for an unknown live path", .{if (unnamed.items.len > 0) try std.mem.join(a, ", ", unnamed.items) else "any module"});
    var kept: std.ArrayList([]const u8) = .empty;
    var left_out: std.ArrayList([]const u8) = .empty;
    for (found) |rel| try (if (unbuilt(modules, built, rel)) &left_out else &kept).append(a, rel);
    if (left_out.items.len > 0)
        log("module preflight: eb would not build {d} module(s) that the dry run marks [ ]: {s}", .{ left_out.items.len, try std.mem.join(a, ", ", left_out.items) });
    return .{ .rels = kept.items, .left_out = left_out.items };
}

fn unbuilt(modules: []const DryRunModule, built: []const []const u8, rel: []const u8) bool {
    if (contains(built, rel)) return false;
    for (modules) |m| if (m.status == ' ' and std.mem.eql(u8, m.rel, rel)) return true;
    return false;
}

fn learnRels(r: *R) E!Learned {
    const a = r.a;
    var rels: std.ArrayList([]const u8) = .empty;
    var left_out: []const []const u8 = &.{};
    for (r.spec.module_rels) |rel| if (!contains(rels.items, rel)) try rels.append(a, rel);
    var cmd: std.ArrayList([]const u8) = .empty;
    try cmd.appendSlice(a, &.{ r.spec.eb_executable, "--dry-run" });
    try cmd.appendSlice(a, engine.ensureRebuild(a, engine.filterEbArgs(a, r.spec.eb_args)));
    log("module preflight: {s}", .{joinCmd(a, cmd.items)});
    const envp = try dryRunEnv(r);
    var modules: []const DryRunModule = &.{};
    var code: u32 = 0;
    var tail: []const u8 = "";
    if (resolveExe(a, r.env, r.spec.eb_executable)) |exe| {
        if (common.proc.run(a, exe, cmd.items, envp, .{ .stdout_limit = 256 << 20, .stderr_limit = 64 << 20, .timeout_ms = null, .new_session = false })) |res| {
            code = res.term.code();
            const both = try a.print("{s}\n{s}", .{ res.stdout, res.stderr });
            modules = dryRunModules(a, both);
            tail = std.mem.trim(u8, if (res.stderr.len > 0) res.stderr else res.stdout, " \t\n\r");
        } else |err| return r.refuse("{s}", .{main_mod.osError(a, err, r.spec.eb_executable)});
    } else return r.refuse("[Errno 2] No such file or directory: '{s}'", .{r.spec.eb_executable});
    var found = relsOf(a, modules);
    if (code != 0 and std.mem.eql(u8, r.spec.backend, "bwrap") and found.len == 0 and rels.items.len == 0)
        return r.refuse("bwrap backend did not get a module list from eb --dry-run, so it will not mount on an unknown path (eb exited {d}). {s}", .{ code, tail });
    for (modules) |m| {
        if (m.status == ' ' and !contains(rels.items, m.rel)) {
            const learned = try leaveOutUnbuilt(r, modules, found);
            found = learned.rels;
            left_out = learned.left_out;
            break;
        }
    }
    for (found) |rel| if (!contains(rels.items, rel)) try rels.append(a, rel);
    if (rels.items.len == 0) return r.refuse("run needs --module Name/version or an eb --dry-run module line; refusing to build for an unknown live path", .{});
    return .{ .rels = rels.items, .left_out = left_out };
}

/// The live paths EasyBuild's bubblewrap mode would leave behind. It
/// creates the live installation directory of each module its dry run
/// lists, as a mount point for the generation, including the modules the
/// build leaves out. Those paths under --software, and their parents, that
/// do not exist yet, deepest first.
fn mountPoints(r: *R, left_out: []const []const u8) E![]const []const u8 {
    const a = r.a;
    var absent: std.ArrayList([]const u8) = .empty;
    if (!std.mem.eql(u8, r.spec.backend, "bwrap")) return absent.items;
    for (left_out) |rel| {
        var parts = std.mem.splitScalar(u8, rel, '/');
        const sane = while (parts.next()) |part| {
            if (part.len == 0 or std.mem.eql(u8, part, ".") or std.mem.eql(u8, part, "..")) break false;
        } else true;
        if (!sane) continue;
        var end = rel.len;
        while (true) {
            const sub = rel[0..end];
            if (!isKind(try std.fs.path.join(a, &.{ r.spec.software, sub }), "missing")) break;
            if (!contains(absent.items, sub)) try absent.append(a, sub);
            end = std.mem.findScalarLast(u8, sub, '/') orelse break;
        }
    }
    std.mem.sort([]const u8, absent.items, {}, deeperFirst);
    return absent.items;
}

fn deeperFirst(_: void, x: []const u8, y: []const u8) bool {
    return std.mem.countScalar(u8, x, '/') > std.mem.countScalar(u8, y, '/');
}

/// Remove the mount points of mountPoints that are still empty directories.
fn removeMountPoints(r: *R) void {
    const points = r.get("mount_points") orelse return;
    if (points != .list) return;
    for (points.list) |p| if (p == .str) {
        const path = std.fs.path.join(r.a, &.{ r.spec.software, p.str }) catch return;
        if (livecheck.emptyRealDir(r.a, path)) sys.unlinkat(fsx.cwd, path, linux.AT.REMOVEDIR) catch {};
    };
}

fn contains(list: []const []const u8, x: []const u8) bool {
    for (list) |y| if (std.mem.eql(u8, x, y)) return true;
    return false;
}

fn symlinkComponent(a: Allocator, path: []const u8) ?[]const u8 {
    var current = admin.abspath(a, path);
    while (true) {
        if (isKind(current, "symlink")) return current;
        const parent = std.fs.path.dirname(current) orelse return null;
        if (std.mem.eql(u8, parent, current)) return null;
        current = parent;
    }
}

fn checkInstallDir(r: *R, rel: []const u8) E!void {
    const a = r.a;
    const path = admin.abspath(a, try std.fs.path.join(a, &.{ r.spec.software, rel }));
    if (std.mem.eql(u8, path, admin.abspath(a, r.spec.software)) or !admin.isWithin(a, path, r.spec.software))
        return r.refuse("module path escapes the software root: {s}", .{rel});
    if (isKind(path, "symlink"))
        return r.refuse("{s} is a symlink. Name/version must stay a real directory: bubblewrap 0.12 and later refuse to mount on a symlink, and bubblewrap 0.6.3 follows one. This tool depends on neither.", .{path});
    if (symlinkComponent(a, path)) |c| return r.refuse("{s}: {s} is a symlink. Name/version must stay a real directory", .{ rel, c });
    const k = kind(path);
    if (!std.mem.eql(u8, k, "dir")) return r.refuse("{s} is {s}, not an existing rebuild directory", .{ path, notADirectory(k) });
}

/// What a path of the kind `k`, not a directory, is, as a refusal says it.
fn notADirectory(k: []const u8) []const u8 {
    if (std.mem.eql(u8, k, "missing")) return "missing";
    if (std.mem.eql(u8, k, "file")) return "a file";
    if (std.mem.eql(u8, k, "symlink")) return "a symlink";
    return "a special file";
}

// ---- build ------------------------------------------------------------

fn removeGenerationModules(r: *R, generation_root: []const u8) E!void {
    const modules = try std.fs.path.join(r.a, &.{ admin.abspath(r.a, generation_root), "modules" });
    if (isKind(modules, "symlink")) return r.refuse("refusing to clear a symlinked module stage: {s}", .{modules});
    // remove_generation_tree: owned directories on one filesystem, each
    // opened 0700 when a sealed build left it without write or search.
    if (isKind(modules, "dir")) admin.removeTree(r.ctx, modules, admin.abspath(r.a, generation_root));
}

fn build(r: *R) E!u8 {
    const a = r.a;
    r.set("state", Py.s("building"));
    if (r.get("generation_removed_at")) |v| if (v != .null) r.set("generation_removed_at", .null);
    try r.save("journal.building");
    const gen = r.str("generation_root");
    atomic.makedirs(gen) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, gen)});
    try removeGenerationModules(r, gen);
    try hit(r, r.faults(), "build.start");
    const filtered = engine.ensureRebuild(a, engine.filterEbArgsOpts(a, r.spec.eb_args, r.spec.build_dir != null));
    var cmd: std.ArrayList([]const u8) = .empty;
    var envp: common.proc.Envp = undefined;
    const software = try std.fs.path.join(a, &.{ gen, "software" });
    const modules = try std.fs.path.join(a, &.{ gen, "modules" });
    if (std.mem.eql(u8, r.spec.backend, "bwrap")) {
        try cmd.appendSlice(a, &.{ r.spec.eb_executable, "--experimental", "--bwrap", try a.print("--bwrap-installpath={s}", .{gen}), "--bwrap-options=--die-with-parent" });
        if (r.spec.build_dir) |bd| try cmd.append(a, try a.print("--buildpath={s}", .{bd}));
        try cmd.appendSlice(a, filtered);
        envp = try childEnv(r, r.env, r.spec.software, r.spec.modules, r.spec.modules);
    } else {
        atomic.makedirs(software) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, software)});
        atomic.makedirs(modules) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, modules)});
        var inner: std.ArrayList([]const u8) = .empty;
        try inner.appendSlice(a, &.{ r.spec.eb_executable, try a.print("--installpath-software={s}", .{software}), try a.print("--installpath-modules={s}", .{modules}) });
        if (r.spec.build_dir) |bd| try inner.append(a, try a.print("--buildpath={s}", .{bd}));
        try inner.appendSlice(a, filtered);
        envp = try childEnv(r, r.env, software, modules, r.spec.modules);
        if (std.mem.eql(u8, r.spec.backend, "userns")) {
            const probes = r.get("probes") orelse Py{ .dict = &.{} };
            const unshare = blk: {
                if (pyfmt.get(probes, "userns_sandbox")) |u| if (pyfmt.get(u, "path")) |p| if (p == .str and p.str.len > 0) break :blk p.str;
                break :blk resolveExe(a, r.env, "unshare") orelse "unshare";
            };
            try cmd.appendSlice(a, &.{ unshare, "--user", "--map-root-user", "--mount", "--fork", "--kill-child", selfExe(a), probe.sandbox_command, "--read-only", r.spec.software, "--read-only", r.spec.modules, "--" });
            try cmd.appendSlice(a, inner.items);
        } else try cmd.appendSlice(a, inner.items);
    }
    r.set("eb_command", pyList(a, cmd.items));
    log("running {s}", .{joinCmd(a, cmd.items)});
    const exe = resolveExe(a, r.env, cmd.items[0]) orelse return r.refuse("[Errno 2] No such file or directory: '{s}'", .{cmd.items[0]});
    const rc = builders.runCommand(a, exe, cmd.items, envp, r.faults(), "build.invoke") catch |err| switch (err) {
        error.Refused => return r.refuse("external commands require /proc to confirm their process group stopped", .{}),
        error.CannotStart => return r.refuse("could not start {s}", .{cmd.items[0]}),
        error.ExecFailed => if (builders.injected_at) |st|
            return r.refuse("[Errno 5] injected failure at {s}", .{st})
        else
            return r.refuse("[Errno {d}] {s}: '{s}'", .{ @backingInt(builders.exec_errno), builders.strerror(builders.exec_errno), cmd.items[0] }),
    };
    if (std.mem.eql(u8, r.spec.backend, "bwrap")) removeMountPoints(r);
    try hit(r, r.faults(), "build.finish");
    r.set("eb_returncode", .{ .int = rc });
    return rc;
}

const selfExe = probe.selfExe;

fn countKey(entries: []const Py.Entry, key: []const u8) usize {
    var count: usize = 0;
    for (entries) |entry| if (std.mem.eql(u8, entry.key, key)) {
        count += 1;
    };
    return count;
}

fn enforceManifest(r: *R) E!void {
    if (r.spec.skip_live_check) return;
    const a = r.a;
    var changed: std.ArrayList([]const u8) = .empty;
    const rels = r.get("module_rels") orelse
        return r.intervene("journal manifests do not match its module relations", .{});
    const manifests = r.get("manifests") orelse
        return r.intervene("journal manifests do not match its module relations", .{});
    if (rels != .list or manifests != .dict or rels.list.len != manifests.dict.len)
        return r.intervene("journal manifests do not match its module relations", .{});
    for (rels.list) |rel| {
        if (rel != .str or countKey(manifests.dict, rel.str) != 1)
            return r.intervene("journal manifests do not match its module relations", .{});
        const path = try std.fs.path.join(a, &.{ r.spec.software, rel.str });
        if (std.mem.eql(u8, admin.abspath(a, path), admin.abspath(a, r.spec.software)) or
            !admin.isWithin(a, path, r.spec.software))
            return r.intervene("journal module relation escapes the software root", .{});
    }
    for (manifests.dict) |e| {
        var rel_count: usize = 0;
        for (rels.list) |rel| if (rel == .str and std.mem.eql(u8, rel.str, e.key)) {
            rel_count += 1;
        };
        if (rel_count != 1)
            return r.intervene("journal manifests do not match its module relations", .{});
        if (!livecheck.validSnapshot(e.value))
            return r.intervene("journal manifest for {s} is malformed", .{e.key});
        const path = try std.fs.path.join(a, &.{ r.spec.software, e.key });
        if (std.mem.eql(u8, r.spec.backend, "bwrap")) {
            const ex = pyfmt.get(e.value, "exists");
            if (ex != null and ex.? == .bool and !ex.?.bool and livecheck.emptyRealDir(a, path))
                sys.unlinkat(fsx.cwd, path, linux.AT.REMOVEDIR) catch {};
        }
        const after = livecheck.snapshot(a, path) catch return r.refuse("cannot snapshot {s}", .{path});
        if (!livecheck.same(a, e.value, after)) try changed.append(a, e.key);
    }
    if (changed.items.len > 0) return r.refuse("live software directory changed during the build; not published: {s}", .{std.mem.join(a, ", ", changed.items) catch ""});
}

// ---- plan ---------------------------------------------------------------

fn stageRoots(r: *R, gen: []const u8) E!struct { software: []const u8, modules: []const u8, how: []const u8 } {
    const a = r.a;
    if (!std.mem.eql(u8, r.spec.backend, "bwrap"))
        return .{ .software = try std.fs.path.join(a, &.{ gen, "software" }), .modules = try std.fs.path.join(a, &.{ gen, "modules" }), .how = "installpath" };
    const info_path = try std.fs.path.join(a, &.{ gen, "bwrap_info.json" });
    if (!isKind(info_path, "file")) return r.refuse("bwrap backend finished without {s}; nothing was published", .{info_path});
    const raw = try readBytes(r, info_path);
    const parsed = json.parse(a, raw) catch return r.refuse("cannot read {s}: not valid JSON", .{info_path});
    const info = json.toPy(a, parsed) catch return r.refuse("cannot read {s}: not valid JSON", .{info_path});
    if (info != .dict) return r.refuse("bwrap_info.json is not an object", .{});
    const command = pyfmt.get(info, "bwrap_cmd");
    if (command) |c| if (c == .list) for (c.list) |x| if (x == .str and std.mem.eql(u8, x.str, "--overlay"))
        return r.refuse("bwrap used overlayfs for this install; the stage directory is an upper layer, not a complete generation, so it was not published", .{});
    var software = s(pyfmt.get(info, "bwrap_installpath_software"));
    var modules = s(pyfmt.get(info, "bwrap_installpath_modules"));
    var how: []const u8 = "bwrap_info";
    if (software.len == 0 or modules.len == 0) {
        const probes = r.get("probes") orelse Py{ .dict = &.{} };
        const eb = pyfmt.get(probes, "easybuild") orelse Py{ .dict = &.{} };
        const v = pyfmt.get(eb, "framework_version");
        var legacy = false;
        if (v) |vv| if (vv == .list and vv.list.len == 3) {
            const x = vv.list;
            legacy = x[0] == .int and x[1] == .int and x[2] == .int and x[0].int == 5 and x[1].int == 3 and (x[2].int == 0 or x[2].int == 1);
        };
        if (!legacy) {
            var keys: std.ArrayList([]const u8) = .empty;
            for (info.dict) |e| try keys.append(a, e.key);
            std.mem.sort([]const u8, keys.items, {}, struct {
                fn lt(_: void, x: []const u8, y: []const u8) bool {
                    return json.pyTextLess(x, y);
                }
            }.lt);
            const shown = if (keys.items.len > 0) try std.mem.join(a, ", ", keys.items) else "none";
            return r.refuse("bwrap_info.json has no bwrap_installpath_software (keys: {s}). EasyBuild 5.3.0 and 5.3.1 omit it; this version was not identified as either, so the stage was kept and not published", .{shown});
        }
        const root = if (s(pyfmt.get(info, "bwrap_installpath")).len > 0) s(pyfmt.get(info, "bwrap_installpath")) else gen;
        software = try std.fs.path.join(a, &.{ root, "software" });
        modules = try std.fs.path.join(a, &.{ root, "modules" });
        how = "easybuild-5.3-layout";
    }
    for ([_][2][]const u8{ .{ "software", software }, .{ "modules", modules } }) |p| {
        if (!admin.isWithin(a, p[1], gen)) return r.refuse("bwrap stage {s} path escapes this run: {s}", .{ p[0], p[1] });
    }
    return .{ .software = software, .modules = modules, .how = how };
}

fn withWritableDir(r: *R, dir: []const u8) E!?u32 {
    const st = fsx.statAt(fsx.cwd, dir, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, dir)});
    const mode = st.perm();
    if (mode & 0o200 != 0) return null;
    chmod(dir, mode | 0o300) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, dir)});
    return mode;
}

fn chmod(p: []const u8, mode: u32) sys.Error!void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = try sys.z(&zb, p);
    while (true) {
        if (try sys.result(linux.fchmodat(linux.AT.FDCWD, z.ptr, @intCast(mode)))) |_| return;
    }
}

fn plan(r: *R) E!void {
    const a = r.a;
    const gen = r.str("generation_root");
    const roots = try stageRoots(r, gen);
    r.set("stage_software", Py.s(admin.abspath(a, roots.software)));
    r.set("stage_modules", Py.s(admin.abspath(a, roots.modules)));
    r.set("stage_how", Py.s(roots.how));
    if (!isKind(roots.software, "dir")) return r.refuse("stage software tree is not a real directory: {s}", .{roots.software});
    var why: []const u8 = "";
    const tree = discover.walkModuleTree(a, roots.modules, &why) catch |err| return if (err == error.Refused) r.refuse("{s}", .{why}) else error.OutOfMemory;
    if (tree.files.len == 0) return r.refuse("no module files under {s}", .{roots.modules});
    const ordered = discover.orderModuleFiles(a, tree.files, roots.modules, &why) catch |err| return if (err == error.Refused) r.refuse("{s}", .{why}) else error.OutOfMemory;
    const Draft = struct { rel: []const u8, staged: []const u8, live: []const u8, retired: []const u8, text: []const u8, live_module: []const u8, path: []const u8 };
    var drafted: std.ArrayList(Draft) = .empty;
    const run_id = r.str("run_id");
    for (ordered.order) |path| {
        const rel = modulefile.relpath(a, path, roots.modules);
        const inst = discover.swapInstallFor(a, ordered.texts.get(path).?, roots.software, r.spec.software, &why) catch |err| return if (err == error.Refused) r.refuse("{s}", .{why}) else error.OutOfMemory;
        if (!generationComplete(a, inst.staged)) return r.refuse("{s} is not a complete staged installation", .{inst.staged});
        if (isKind(inst.staged, "symlink")) return r.refuse("staged installation {s} is a symlink", .{inst.staged});
        if (isKind(inst.live, "symlink")) return r.refuse("{s} is a symlink. Name/version must stay a real directory", .{inst.live});
        if (!isKind(inst.live, "dir")) return r.refuse("staged rebuild target must already be a real directory: {s}", .{inst.live});
        const retired = try std.fs.path.join(a, &.{ std.fs.path.dirname(inst.live) orelse "/", try a.print(".{s}.{s}", .{ std.fs.path.basename(inst.live), run_id }) });
        if (!isKind(retired, "missing")) return r.refuse("retired path already exists: {s}", .{retired});
        const live_module = try std.fs.path.join(a, &.{ r.spec.modules, rel });
        if (!admin.isWithin(a, live_module, r.spec.modules)) return r.refuse("module path escapes the module root: {s}", .{rel});
        if (isKind(live_module, "symlink")) return r.refuse("refusing to replace module symlink {s}. The module file that is the pointer must be a regular file", .{live_module});
        if (symlinkComponent(a, live_module)) |c| return r.refuse("{s} is a symlink", .{c});
        const lk = kind(live_module);
        if (!std.mem.eql(u8, lk, "missing") and !std.mem.eql(u8, lk, "file")) return r.refuse("{s} is not a regular file", .{live_module});
        try drafted.append(a, .{ .rel = rel, .staged = inst.staged, .live = inst.live, .retired = retired, .text = inst.text, .live_module = live_module, .path = path });
    }
    const rdir = runDir(a, r.spec.state_root, run_id);
    var plan_items: std.ArrayList(Py) = .empty;
    for (drafted.items) |d| {
        const prepared = try std.fs.path.join(a, &.{ rdir, "prepared", d.rel });
        const metadata_source = if (isKind(d.live_module, "file")) d.live_module else d.path;
        try write(r, prepared, d.text, try a.print("prepare:{s}", .{d.rel}), metadata_source, r.faults());
        const stamp = Py{ .dict = &.{
            .{ .key = "run_id", .value = Py.s(run_id) },
            .{ .key = "staged", .value = Py.s(d.staged) },
            .{ .key = "live", .value = Py.s(d.live) },
            .{ .key = "module", .value = Py.s(d.rel) },
            .{ .key = "sha256", .value = Py.s(sha(a, d.text)) },
        } };
        const stamp_text = try a.print("{s}\n", .{try json.dumps(a, stamp, 2, true)});
        const sealed = try withWritableDir(r, d.staged);
        const res = write(r, try std.fs.path.join(a, &.{ d.staged, ".eb-staged-rebuild-complete" }), stamp_text, try a.print("stamp:{s}", .{d.rel}), null, r.faults());
        if (sealed) |m| chmod(d.staged, m) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, d.staged)});
        try res;
        const existed = isKind(d.live_module, "file");
        var old_hash: Py = .null;
        var previous_root: Py = .null;
        if (existed) {
            const old = try readBytes(r, d.live_module);
            old_hash = Py.s(sha(a, old));
            if (!std.unicode.utf8ValidateSlice(old)) return r.refuse("{s} is not UTF-8 text", .{d.live_module});
            if (modulefile.parseRoot(old)) |pr| if (pr.path.len > 0) {
                previous_root = Py.s(pr.path);
                if (!std.mem.eql(u8, admin.abspath(a, pr.path), admin.abspath(a, d.live)))
                    return r.refuse("live module {s} points at {s}, not its stable install path {s}", .{ d.live_module, pr.path, d.live });
            };
        }
        try plan_items.append(a, .{ .dict = try a.dupe(Py.Entry, &.{
            .{ .key = "rel", .value = Py.s(d.rel) },
            .{ .key = "live_module", .value = Py.s(d.live_module) },
            .{ .key = "prepared", .value = Py.s(prepared) },
            .{ .key = "staged", .value = Py.s(d.staged) },
            .{ .key = "live_software", .value = Py.s(d.live) },
            .{ .key = "retired", .value = Py.s(d.retired) },
            .{ .key = "backup", .value = Py.s(try std.fs.path.join(a, &.{ rdir, "backups", d.rel })) },
            .{ .key = "new_hash", .value = Py.s(sha(a, d.text)) },
            .{ .key = "old_hash", .value = old_hash },
            .{ .key = "previous_root", .value = previous_root },
            .{ .key = "existed", .value = .{ .bool = existed } },
            .{ .key = "status", .value = Py.s("pending") },
            .{ .key = "swap_method", .value = .null },
            .{ .key = "published_at", .value = .null },
        }) });
    }
    var link_items: std.ArrayList(Py) = .empty;
    for (tree.links) |l| {
        try link_items.append(a, .{ .dict = try a.dupe(Py.Entry, &.{
            .{ .key = "rel", .value = Py.s(l.rel) },
            .{ .key = "live_path", .value = Py.s(try std.fs.path.join(a, &.{ r.spec.modules, l.rel })) },
            .{ .key = "target", .value = Py.s(discover.mapLinkTarget(a, l.target, roots.modules, r.spec.modules)) },
            .{ .key = "status", .value = Py.s("pending") },
            .{ .key = "created", .value = .{ .bool = false } },
        }) });
    }
    r.set("modules_plan", .{ .list = plan_items.items });
    r.set("links_plan", .{ .list = link_items.items });
}

fn generationComplete(a: Allocator, path: []const u8) bool {
    if (!isKind(path, "dir")) return false;
    for ([_][]const u8{ "PAYLOAD", ".eb-staged-rebuild-complete" }) |n| if (isKind(std.fs.path.join(a, &.{ path, n }) catch return false, "file")) return true;
    return installCompleteMeta(a, path);
}

fn installCompleteMeta(a: Allocator, path: []const u8) bool {
    const meta = std.fs.path.join(a, &.{ path, "easybuild" }) catch return false;
    if (!isKind(meta, "dir")) return false;
    const fd = fsx.openDir(fsx.cwd, meta, false) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    while (it.next() catch null) |e| {
        if (std.mem.endsWith(u8, e.name, ".eb") and isKind(std.fs.path.join(a, &.{ meta, e.name }) catch continue, "file")) return true;
    }
    return false;
}

/// install_complete: PAYLOAD, or a readable easybuild/*.eb; the tool's own
/// stamp is not build evidence.
fn installComplete(a: Allocator, path: []const u8) bool {
    if (!isKind(path, "dir")) return false;
    if (isKind(std.fs.path.join(a, &.{ path, "PAYLOAD" }) catch return false, "file")) return true;
    return installCompleteMeta(a, path);
}

// ---- publish --------------------------------------------------------------

fn treeRunId(a: Allocator, path: []const u8) ?[]const u8 {
    const stamp = std.fs.path.join(a, &.{ path, ".eb-staged-rebuild-complete" }) catch return null;
    if (!isKind(stamp, "file")) return null;
    const f = fsx.openRegular(fsx.cwd, stamp, true) catch return null;
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 1 << 20) catch return null;
    const v = json.toPy(a, json.parse(a, raw) catch return null) catch return null;
    const id = pyfmt.get(v, "run_id") orelse return null;
    return if (id == .str) id.str else null;
}

fn allowExchange(r: *R) bool {
    const probes = r.get("probes") orelse return false;
    const ex = pyfmt.get(probes, "rename_exchange") orelse return false;
    return pyfmt.truthy(pyfmt.get(ex, "ok"));
}

fn requireExchange(r: *R) bool {
    return pyfmt.truthy(r.get("require_exchange"));
}

fn swapPair(r: *R, live: []const u8, retired: []const u8, faults: atomic.Faults, step: []const u8) E![]const u8 {
    var why: swap.Why = .{};
    const m = swap.swapDirectories(r.a, live, retired, allowExchange(r), !requireExchange(r), faults, step, &why) catch |err| return switch (err) {
        error.Refused => r.refuse("{s}", .{why.msg}),
        error.NeedsIntervention => r.intervene("{s}", .{why.msg}),
        error.Failed => r.refuse("{s}", .{why.msg}),
    };
    return @tagName(m);
}

fn recoverPair(r: *R, live: []const u8, retired: []const u8) E!void {
    var why: swap.Why = .{};
    swap.recoverInterrupted(r.a, live, retired, &why) catch |err| return switch (err) {
        error.Refused, error.Failed => r.refuse("{s}", .{why.msg}),
        error.NeedsIntervention => r.intervene("{s}", .{why.msg}),
    };
}

fn sameAccess(a: Allocator, left: []const u8, right: []const u8) bool {
    var why: atomic.Why = .{};
    const x = atomic.readMetadata(a, left, &why) catch return false;
    const y = atomic.readMetadata(a, right, &why) catch return false;
    return x.info.uid == y.info.uid and x.info.gid == y.info.gid and x.info.perm() == y.info.perm() and x.acls.eql(y.acls);
}

fn fileHash(r: *R, p: []const u8) E![]const u8 {
    return sha(r.a, try readBytes(r, p));
}

fn prepareBackup(r: *R, m: Py) E!void {
    const a = r.a;
    const live = s(pyfmt.get(m, "live_module"));
    const rel = s(pyfmt.get(m, "rel"));
    const k = kind(live);
    if (std.mem.eql(u8, k, "symlink")) return r.refuse("refusing to replace module symlink {s}", .{live});
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        if (!std.mem.eql(u8, k, "file")) return r.intervene("{s} disappeared before publish", .{live});
        const current = try readBytes(r, live);
        const current_hash = sha(a, current);
        const backup = s(pyfmt.get(m, "backup"));
        if (std.mem.eql(u8, current_hash, s(pyfmt.get(m, "new_hash"))) and isKind(backup, "file")) {
            if (!std.mem.eql(u8, try fileHash(r, backup), s(pyfmt.get(m, "old_hash")))) return r.intervene("backup for {s} does not match the previous module file", .{rel});
            return;
        }
        if (!std.mem.eql(u8, current_hash, s(pyfmt.get(m, "old_hash")))) return r.intervene("{s} changed after the build", .{live});
        if (isKind(backup, "missing")) {
            try write(r, backup, current, try a.print("publish.backup:{s}", .{rel}), live, r.faults());
        } else if (!isKind(backup, "file") or !std.mem.eql(u8, try fileHash(r, backup), s(pyfmt.get(m, "old_hash")))) {
            return r.intervene("backup for {s} does not match the previous module file", .{rel});
        }
    } else if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        return;
    } else if (!std.mem.eql(u8, k, "missing")) {
        return r.intervene("new module path appeared before publish: {s}", .{live});
    }
}

fn moveSealed(r: *R, source: []const u8, destination: []const u8) E!void {
    const st = fsx.statAt(fsx.cwd, source, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, source)});
    const mode = st.perm();
    if (mode & 0o200 == 0) chmod(source, mode | 0o300) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, source)});
    sys.renameat(fsx.cwd, source, fsx.cwd, destination) catch |err| {
        if (mode & 0o200 == 0) chmod(source, mode) catch |restore| return r.refuse("{s}", .{main_mod.osError(r.a, restore, source)});
        if (err == error.CrossDevice) return r.refuse("the stage and live software tree are on different filesystems; place --state-root beside the software tree", .{});
        return r.refuse("{s}", .{main_mod.osError(r.a, err, source)});
    };
    if (mode & 0o200 == 0) chmod(destination, mode) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, destination)});
}

fn prepareSwapSource(r: *R, m: Py, run_id: []const u8) E!void {
    const staged = s(pyfmt.get(m, "staged"));
    const retired = s(pyfmt.get(m, "retired"));
    const sk = kind(staged);
    const rk = kind(retired);
    // The staged tree still holds the generation path. Rewrite it before the
    // directory moves, so the bytes that land at the stable path name that path.
    if (std.mem.eql(u8, sk, "dir")) {
        var why: []const u8 = "";
        prefix_mod.rewriteInstall(r.a, staged, s(pyfmt.get(m, "live_software")), &why) catch |err| switch (err) {
            error.Refused => return r.refuse("{s}", .{why}),
            else => return err,
        };
    }
    if (std.mem.eql(u8, sk, "dir") and std.mem.eql(u8, rk, "missing")) {
        try hit(r, r.faults(), try r.a.print("stage.move:{s}", .{s(pyfmt.get(m, "rel"))}));
        try moveSealed(r, staged, retired);
        const flushed = std.fs.path.dirname(retired) orelse "/";
        atomic.fsyncDir(flushed) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, flushed)});
    } else if (!(std.mem.eql(u8, sk, "missing") and std.mem.eql(u8, rk, "dir"))) {
        return r.intervene("cannot prepare {s}: staged={s}, retired={s}", .{ s(pyfmt.get(m, "rel")), sk, rk });
    }
    // After an exchange the journal did not record, the run's tree is live
    // and the retired name holds the old tree; the baseline step swaps them
    // back.
    const tid = treeRunId(r.a, retired);
    const lid = treeRunId(r.a, s(pyfmt.get(m, "live_software")));
    const retired_ours = tid != null and std.mem.eql(u8, tid.?, run_id);
    const live_ours = lid != null and std.mem.eql(u8, lid.?, run_id);
    if (!retired_ours and !live_ours) return r.intervene("prepared tree does not belong to run {s}: {s}", .{ run_id, retired });
}

fn writeNewModule(r: *R, m: Py, faults: atomic.Faults) E!void {
    const a = r.a;
    const rel = s(pyfmt.get(m, "rel"));
    const prepared = try readBytes(r, s(pyfmt.get(m, "prepared")));
    if (!std.mem.eql(u8, sha(a, prepared), s(pyfmt.get(m, "new_hash")))) return r.intervene("prepared module bytes for {s} changed", .{rel});
    const live = s(pyfmt.get(m, "live_module"));
    const k = kind(live);
    if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        if (sameAccess(a, live, s(pyfmt.get(m, "prepared")))) return;
    }
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        if (!std.mem.eql(u8, k, "file") or !std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "old_hash"))))
            return r.intervene("{s} is not the previous module file", .{live});
    } else if (!std.mem.eql(u8, k, "missing")) {
        return r.intervene("{s} appeared before it could be published", .{live});
    }
    try write(r, live, prepared, try a.print("publish.file:{s}", .{rel}), s(pyfmt.get(m, "prepared")), faults);
}

fn restoreOldModule(r: *R, m: Py, faults: atomic.Faults) E!void {
    const a = r.a;
    const rel = s(pyfmt.get(m, "rel"));
    const live = s(pyfmt.get(m, "live_module"));
    const k = kind(live);
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        const backup = s(pyfmt.get(m, "backup"));
        if (!isKind(backup, "file")) return r.intervene("no backup to restore for {s}", .{rel});
        const previous = try readBytes(r, backup);
        if (!std.mem.eql(u8, sha(a, previous), s(pyfmt.get(m, "old_hash")))) return r.intervene("backup for {s} changed", .{rel});
        if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "old_hash")))) {
            // The backup carries the previous access policy; the same bytes
            // under a published policy still have to go back.
            if (sameAccess(a, live, backup)) return;
        }
        if (!std.mem.eql(u8, k, "file") or !std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash"))))
            return r.intervene("{s} is neither the old nor new module file", .{live});
        try write(r, live, previous, try a.print("restore.file:{s}", .{rel}), backup, faults);
        return;
    }
    if (std.mem.eql(u8, k, "missing")) return;
    if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        try hit(r, faults, try a.print("restore.remove:{s}", .{rel}));
        sys.unlinkat(fsx.cwd, live, 0) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, live)});
        const flushed = std.fs.path.dirname(live) orelse "/";
        atomic.fsyncDir(flushed) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, flushed)});
        return;
    }
    return r.intervene("{s} is not the module file this run created", .{live});
}

fn readlinkText(a: Allocator, p: []const u8) []const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = sys.z(&zb, p) catch return "";
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch return "";
    return a.dupe(u8, buf[0..n]) catch "";
}

fn createLinks(r: *R, faults: atomic.Faults) E!void {
    const a = r.a;
    for (r.list("links_plan"), 0..) |l, i| {
        const path = s(pyfmt.get(l, "live_path"));
        const target = s(pyfmt.get(l, "target"));
        const k = kind(path);
        if (std.mem.eql(u8, k, "symlink") and std.mem.eql(u8, readlinkText(a, path), target)) {
            r.setItem("links_plan", i, "status", Py.s("present"));
            continue;
        }
        if (!std.mem.eql(u8, k, "missing")) {
            r.setItem("links_plan", i, "status", Py.s("kept"));
            continue;
        }
        const parent = std.fs.path.dirname(path) orelse "/";
        if (symlinkComponent(a, parent)) |c| return r.refuse("{s} is a symlink", .{c});
        atomic.makedirs(parent) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, parent)});
        try hit(r, faults, try a.print("links.create:{s}", .{s(pyfmt.get(l, "rel"))}));
        var zt: [sys.path_max:0]u8 = undefined;
        var zp: [sys.path_max:0]u8 = undefined;
        const zt_ = sys.z(&zt, target) catch return r.refuse("bad link target", .{});
        const zp_ = sys.z(&zp, path) catch return r.refuse("bad link path", .{});
        if (linux.errno(linux.symlinkat(zt_.ptr, linux.AT.FDCWD, zp_.ptr)) != .SUCCESS) return r.refuse("cannot create {s}", .{path});
        r.setItem("links_plan", i, "created", .{ .bool = true });
        r.setItem("links_plan", i, "status", Py.s("created"));
    }
}

fn removeCreatedLinks(r: *R) void {
    const links = r.list("links_plan");
    var i = links.len;
    while (i > 0) {
        i -= 1;
        const l = r.list("links_plan")[i];
        if (!pyfmt.truthy(pyfmt.get(l, "created"))) continue;
        const path = s(pyfmt.get(l, "live_path"));
        if (isKind(path, "symlink") and std.mem.eql(u8, readlinkText(r.a, path), s(pyfmt.get(l, "target")))) {
            sys.unlinkat(fsx.cwd, path, 0) catch {};
            atomic.fsyncDir(std.fs.path.dirname(path) orelse "/") catch {};
        }
        r.setItem("links_plan", i, "created", .{ .bool = false });
        r.setItem("links_plan", i, "status", Py.s("pending"));
    }
}

fn normalizeBaseline(r: *R) E!void {
    const run_id = r.str("run_id");
    const mods = r.list("modules_plan");
    var i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        const lr = treeRunId(r.a, live);
        const rr = treeRunId(r.a, retired);
        const live_is = lr != null and std.mem.eql(u8, lr.?, run_id);
        const retired_is = rr != null and std.mem.eql(u8, rr.?, run_id);
        if (live_is and !retired_is) {
            _ = try swapPair(r, live, retired, .{}, try r.a.print("recover:{s}", .{s(pyfmt.get(m, "rel"))}));
        } else if (!(!live_is and retired_is)) {
            return r.intervene("cannot identify old and staged trees for {s}: live run={s}, retired run={s}", .{ s(pyfmt.get(m, "rel")), reprOpt(r.a, lr), reprOpt(r.a, rr) });
        }
        try restoreOldModule(r, m, .{});
        r.setItem("modules_plan", i, "status", Py.s("pending"));
        r.setItem("modules_plan", i, "swap_method", .null);
        r.setItem("modules_plan", i, "published_at", .null);
    }
    removeCreatedLinks(r);
}

fn reprOpt(a: Allocator, v: ?[]const u8) []const u8 {
    const x = v orelse return "None";
    var out: std.ArrayList(u8) = .empty;
    pyfmt.reprStr(&out, a, x) catch return x;
    return out.items;
}

fn requirePublicationFields(r: *R) E!void {
    for (r.list("modules_plan")) |m| {
        const rel = s(pyfmt.get(m, "rel"));
        const prepared = s(pyfmt.get(m, "prepared"));
        const new_hash = s(pyfmt.get(m, "new_hash"));
        if (rel.len == 0 or prepared.len == 0 or new_hash.len == 0)
            return r.intervene("journal module plan does not record rel, prepared and new_hash", .{});
    }
}

fn validatePublicationSet(r: *R) E!void {
    try requirePublicationFields(r);
    for (r.list("modules_plan")) |m| {
        const rel = s(pyfmt.get(m, "rel"));
        const prepared = s(pyfmt.get(m, "prepared"));
        const new_hash = s(pyfmt.get(m, "new_hash"));
        const f = fsx.openRegular(fsx.cwd, prepared, true) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, main_mod.osError(r.a, err, prepared) });
        defer sys.close(f.fd);
        const bytes = fsx.readAll(r.a, f.fd, 1 << 30) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, main_mod.osError(r.a, err, prepared) });
        if (!std.mem.eql(u8, sha(r.a, bytes), new_hash)) return r.intervene("prepared module bytes for {s} changed", .{rel});
        var why: atomic.Why = .{};
        _ = atomic.readMetadata(r.a, prepared, &why) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, if (err == error.Refused) why.msg else main_mod.osError(r.a, err, prepared) });
    }
}

fn publish(r: *R) E!void {
    const a = r.a;
    for (r.list("modules_plan")) |m| {
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        if (live.len > 0 and retired.len > 0) try recoverPair(r, live, retired);
    }
    try validatePublicationSet(r);
    const run_id = r.str("run_id");
    for (r.list("modules_plan")) |m| {
        try prepareBackup(r, m);
        try prepareSwapSource(r, m, run_id);
    }
    try r.save("journal.prepared");
    r.set("state", Py.s("publishing"));
    try r.save("journal.publishing");
    publishSteps(r) catch |err| {
        const first = r.why;
        const kind_err = err;
        removeCreatedLinks(r);
        normalizeBaseline(r) catch {
            return r.intervene("publish failed and the old live tree could not be restored: {s}", .{r.why});
        };
        r.set("state", Py.s("built"));
        r.set("message", Py.s("publish interrupted; old live tree restored"));
        // As in Python, a failure to record the restore replaces the error.
        try r.saveRestore("journal.restored");
        r.why = first;
        return kind_err;
    };
    _ = a;
}

fn publishSteps(r: *R) E!void {
    const a = r.a;
    try normalizeBaseline(r);
    for (r.list("modules_plan"), 0..) |m, i| {
        const rel = s(pyfmt.get(m, "rel"));
        const method = try swapPair(r, s(pyfmt.get(m, "live_software")), s(pyfmt.get(m, "retired")), r.faults(), try a.print("swap:{s}", .{rel}));
        r.setItem("modules_plan", i, "status", Py.s("swapped"));
        r.setItem("modules_plan", i, "swap_method", Py.s(method));
        r.setItem("modules_plan", i, "published_at", .{ .float = now() });
        log("swapped {s} ({s})", .{ rel, method });
        try r.save(try a.print("journal.swap:{s}", .{rel}));
    }
    for (r.list("modules_plan"), 0..) |m, i| {
        const rel = s(pyfmt.get(m, "rel"));
        try writeNewModule(r, m, r.faults());
        r.setItem("modules_plan", i, "status", Py.s("published"));
        log("published module {s}", .{rel});
        try r.save(try a.print("journal.module:{s}", .{rel}));
    }
    try createLinks(r, r.faults());
    r.set("state", Py.s("complete"));
    r.set("completed_at", .{ .float = now() });
    r.set("message", Py.s("published"));
    try r.save("journal.complete");
}

// ---- preparation and the run as a whole ------------------------------------

fn preparationFailure(r: *R, message: []const u8, code: u8) E!u8 {
    r.set("message", Py.s(message));
    r.set("prepare_returncode", .{ .int = code });
    r.set("updated_at", .{ .float = now() });
    try r.save("journal.prepare_failed");
    log("{s}", .{message});
    return code;
}

fn preparePublication(r: *R) E!u8 {
    const a = r.a;
    const exe = r.spec.prepare_executable orelse return 0;
    if (exe.len == 0 or pyfmt.truthy(r.get("prepare_complete"))) return 0;
    const context_path = try std.fs.path.join(a, &.{ runDir(a, r.spec.state_root, r.str("run_id")), "prepare-context.json" });
    var mods: std.ArrayList(Py) = .empty;
    for (r.list("modules_plan")) |m| {
        var entries: std.ArrayList(Py.Entry) = .empty;
        for ([_][]const u8{ "rel", "staged", "live_software", "retired", "live_module", "prepared" }) |k| try entries.append(a, .{ .key = k, .value = pyfmt.get(m, k) orelse .null });
        try entries.append(a, .{ .key = "generation", .value = pyfmt.get(m, "staged") orelse .null });
        try mods.append(a, .{ .dict = entries.items });
    }
    const context = Py{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "run_id", .value = Py.s(r.str("run_id")) },
        .{ .key = "publication", .value = Py.s("directory-swap") },
        .{ .key = "generation_root", .value = Py.s(r.str("generation_root")) },
        .{ .key = "state_root", .value = Py.s(r.spec.state_root) },
        .{ .key = "live_software", .value = Py.s(r.spec.software) },
        .{ .key = "live_modules", .value = Py.s(r.spec.modules) },
        .{ .key = "stage_software", .value = Py.s(r.str("stage_software")) },
        .{ .key = "stage_modules", .value = Py.s(r.str("stage_modules")) },
        .{ .key = "modules", .value = .{ .list = mods.items } },
    }) };
    try write(r, context_path, try a.print("{s}\n", .{try json.dumps(a, context, 2, true)}), "prepare.context", null, r.faults());
    log("preparing publication permissions: {s} {s}", .{ exe, context_path });
    const rc = builders.runCommand(a, exe, &.{ exe, context_path }, r.env.envp, r.faults(), "prepare.invoke") catch |err| {
        if (err == error.Refused) return r.refuse("external commands require /proc to confirm their process group stopped", .{});
        if (builders.injected_at) |st| return preparationFailure(r, try a.print("publication preparation failed: [Errno 5] injected failure at {s}", .{st}), 2);
        const e: linux.E = if (err == error.ExecFailed) builders.exec_errno else .NOENT;
        return preparationFailure(r, try a.print("publication preparation failed: [Errno {d}] {s}: '{s}'", .{ @backingInt(e), builders.strerror(e), exe }), 2);
    };
    r.set("prepare_returncode", .{ .int = rc });
    if (rc != 0) return preparationFailure(r, try a.print("publication preparation exited {d}; the live directory was not swapped", .{rc}), rc);
    for (r.list("modules_plan")) |m| {
        if (!installComplete(a, s(pyfmt.get(m, "staged"))))
            return preparationFailure(r, try a.print("publication preparation left {s} incomplete at {s}; the live directory was not swapped", .{ s(pyfmt.get(m, "rel")), s(pyfmt.get(m, "staged")) }), 2);
    }
    validatePublicationSet(r) catch |err| switch (err) {
        error.Refused, error.NeedsIntervention => return preparationFailure(r, r.why, 2),
        else => |e| return e,
    };
    r.set("message", Py.s(""));
    r.set("prepare_complete", .{ .bool = true });
    try r.save("journal.prepared");
    return 0;
}

fn failRun(r: *R, message: []const u8, code: u8) E!Result {
    r.set("state", Py.s("failed"));
    r.set("message", Py.s(message));
    r.set("updated_at", .{ .float = now() });
    try r.save("journal.failed");
    log("{s}", .{message});
    return .{ .code = code, .message = message };
}

/// _publisher: the ledger configuration a journal recorded, checked against
/// the file it came from; null without a ledger.
fn publisher(r: *R) E!?Py {
    const path = r.str("ledger_config");
    if (path.len == 0) return null;
    const policy = r.get("ledger_policy");
    if (policy == null or policy.? == .null) return r.refuse("ledger policy is missing from the journal; inspect the publication before resuming", .{});
    var why: []const u8 = "";
    const now_config = publish_mod.loadConfig(r.a, path, &why) catch |err| return switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => r.refuse("{s}", .{why}),
    };
    if (!publish_mod.sameValue(r.a, now_config, policy.?)) return r.refuse("ledger policy changed; restore the recorded configuration before resuming", .{});
    return policy.?;
}

fn record(r: *R, config: Py, recs_in: []const Py, step: ?[]const u8) E![]const Py {
    const rdir = runDir(r.a, r.str("state_root"), r.str("run_id"));
    var why: []const u8 = "";
    if (recs_in.len > 0) publish_mod.enqueue(r.a, rdir, recs_in, r.faults(), step.?, &why) catch |err| return switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        error.NeedsIntervention => r.intervene("{s}", .{why}),
        error.Refused => r.refuse("{s}", .{why}),
    };
    return publish_mod.deliver(r.a, r.env.envp, rdir, config, r.faults(), &why) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        error.NeedsIntervention => r.intervene("{s}", .{why}),
        error.Refused => r.refuse("{s}", .{why}),
    };
}

fn records(r: *R, config: Py, stage: []const u8, corrects: ?*const publish_mod.Corrects) E![]const Py {
    var why: []const u8 = "";
    return publish_mod.publicationRecords(r.a, config, r.journal, stage, now(), corrects, &why) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => r.refuse("{s}", .{why}),
    };
}

fn required(config: Py) bool {
    return pyfmt.truthy(pyfmt.get(config, "required"));
}

/// _record_finished: deliver the finished records, then print the message
/// once, with what is left.
fn recordFinished(r: *R, config: ?Py, message: []const u8) E!Result {
    if (config) |c| {
        const left = try record(r, c, try records(r, c, "finished", null), "outbox.finished");
        if (left.len > 0) {
            const m = try r.a.print("{s}; {s}", .{ message, publish_mod.pendingMessage(r.a, left, "run resume") });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    }
    log("{s}", .{message});
    return .{ .code = 0, .message = message };
}

/// _publish_recorded: swap only after a required ledger acknowledged every
/// started record.
fn publishRecorded(r: *R) E!Result {
    try requirePublicationFields(r);
    const rc = try preparePublication(r);
    if (rc != 0) return .{ .code = rc, .message = r.str("message") };
    const config = try publisher(r);
    if (config) |c| {
        try validatePublicationSet(r);
        const left = try record(r, c, try records(r, c, "started", null), "outbox.started");
        if (left.len > 0 and required(c)) {
            const m = try r.a.print("the live directory was not swapped: {s}", .{publish_mod.pendingMessage(r.a, left, "run resume")});
            r.set("message", Py.s(m));
            try r.save("journal.ledger_pending");
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    }
    try publish(r);
    const message = try r.a.print("run {s} published {d} module(s)", .{ r.str("run_id"), r.list("modules_plan").len });
    return recordFinished(r, config, message);
}

fn runLocked(r: *R, fresh_build: bool) E!Result {
    if (fresh_build) {
        const rc = try build(r);
        if (rc != 0) return failRun(r, try r.a.print("eb exited {d}; live modules were not changed", .{rc}), rc);
        enforceManifest(r) catch |err| switch (err) {
            error.Refused => return failRun(r, r.why, 2),
            else => |e| return e,
        };
        plan(r) catch |err| switch (err) {
            error.Refused => return failRun(r, r.why, 2),
            else => |e| return e,
        };
        r.set("state", Py.s("built"));
        r.set("updated_at", .{ .float = now() });
        try r.save("journal.built");
    }
    return publishRecorded(r);
}

fn lockRoot(r: *R) E!state.RootLock {
    const ms: ?u32 = if (r.spec.lock_timeout) |t| @intFromFloat(@min(@max(t, 0) * 1000, 4.0e9)) else null;
    var detail: []const u8 = r.spec.state_root;
    return rootlock.acquire(r.a, r.spec.state_root, ms, &detail) catch |err| switch (err) {
        error.Busy => r.refuse("another eb-staged-rebuild holds the lock at {s}", .{rootlock.joinPath(r.a, r.spec.state_root, ".lock")}),
        error.NotPrivate => r.refuse("record lock requires a private regular file: {s}", .{rootlock.joinPath(r.a, r.spec.state_root, ".lock.posix")}),
        error.Gate => r.refuse("lock file must be a private regular file: {s}", .{rootlock.joinPath(r.a, r.spec.state_root, ".lock")}),
        error.Unsafe => r.refuse("state root is unsafe: {s} is a symlink", .{detail}),
        else => r.refuse("{s}", .{main_mod.osError(r.a, err, r.spec.state_root)}),
    };
}

const StatFs = extern struct {
    fs_type: isize,
    block_size: isize,
    blocks: isize,
    blocks_free: isize,
    blocks_available: isize,
    files: isize,
    files_free: isize,
    fs_id: [2]i32,
    name_length: isize,
    fragment_size: isize,
    flags: isize,
    spare: [4]isize,
};

fn availableBytes(info: StatFs) u128 {
    const blocks: u128 = if (info.blocks_available > 0) @intCast(info.blocks_available) else 0;
    const unit: u128 = if (info.fragment_size > 0)
        @intCast(info.fragment_size)
    else if (info.block_size > 0)
        @intCast(info.block_size)
    else
        0;
    return blocks * unit;
}

/// The refusal `--min-free-bytes` would print, or null when `path` has room.
/// `path` must already exist. The dry run passes the nearest existing
/// ancestor so it can answer without creating the state root.
pub fn freeSpaceRefusal(a: Allocator, path: []const u8, minimum: u128) ?[]const u8 {
    var path_buf: [sys.path_max:0]u8 = undefined;
    const zpath = sys.z(&path_buf, path) catch
        return a.print("path is too long: {s}", .{path}) catch "path is too long";
    var info: StatFs = undefined;
    const result = linux.syscall2(.statfs, @intFromPtr(zpath.ptr), @intFromPtr(&info));
    if (linux.errno(result) != .SUCCESS)
        return a.print("cannot inspect free space on {s}", .{path}) catch "cannot inspect free space";
    const available = availableBytes(info);
    if (available < minimum)
        return a.print(
            "generation filesystem has {d} bytes available; --min-free-bytes requires {d}",
            .{ available, minimum },
        ) catch "generation filesystem is short";
    return null;
}

fn checkFreeSpace(r: *R) E!void {
    const minimum = r.spec.min_free_bytes orelse return;
    if (freeSpaceRefusal(r.a, r.spec.state_root, minimum)) |msg|
        return r.refuse("{s}", .{msg});
}

/// run without --dry-run.
pub fn run(r: *R, probes: Py, ledger_policy: Py) E!Result {
    const a = r.a;
    if (std.mem.eql(u8, r.spec.backend, "bwrap") and !usable(probes, "bwrap")) {
        const b = pyfmt.get(probes, "bwrap") orelse Py{ .dict = &.{} };
        const reason = if (pyfmt.truthy(pyfmt.get(b, "reason"))) s(pyfmt.get(b, "reason")) else "bwrap is not usable";
        return r.refuse("{s}; refusing to rebuild in place", .{reason});
    }
    if (std.mem.eql(u8, r.spec.backend, "userns") and !usable(probes, "userns_sandbox")) {
        const u = pyfmt.get(probes, "userns_sandbox") orelse Py{ .dict = &.{} };
        const reason = if (pyfmt.truthy(pyfmt.get(u, "reason"))) s(pyfmt.get(u, "reason")) else "the user-namespace sandbox is not usable";
        return r.refuse("{s}; refusing to run the userns backend", .{reason});
    }
    const policy: Py = ledger_policy;
    const learned = try learnRels(r);
    const rels = learned.rels;
    for (rels) |rel| try checkInstallDir(r, rel);
    var run_id: []const u8 = r.spec.run_id orelse "";
    if (run_id.len == 0) {
        const base = try a.print("{s}-{d}", .{ nowStampRunId(a), linux.getpid() });
        run_id = base;
        var n: usize = 0;
        while (fsx.statAt(fsx.cwd, runDir(a, r.spec.state_root, run_id), true)) |_| {
            n += 1;
            run_id = try a.print("{s}-{d}", .{ base, n });
        } else |_| {}
    }
    const l = try lockRoot(r);
    defer l.release();
    try checkStageFilesystem(r, rels);
    try checkFreeSpace(r);
    r.path = try std.fs.path.join(a, &.{ runDir(a, r.spec.state_root, run_id), "journal.json" });
    if (fsx.statAt(fsx.cwd, r.path, true)) |_| return r.refuse("run {s} already exists; resume it or choose another --run-id", .{run_id}) else |_| {}
    atomic.makedirs(runDir(a, r.spec.state_root, run_id)) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, r.spec.state_root)});
    const t = now();
    var manifests: std.ArrayList(Py.Entry) = .empty;
    if (!r.spec.skip_live_check) for (rels) |rel| {
        const snap = livecheck.snapshot(a, try std.fs.path.join(a, &.{ r.spec.software, rel })) catch return r.refuse("cannot snapshot {s}", .{rel});
        try manifests.append(a, .{ .key = rel, .value = snap });
    };
    const gen = admin.abspath(a, try std.fs.path.join(a, &.{ r.spec.state_root, "generations", run_id }));
    var j_entries: std.ArrayList(Py.Entry) = .empty;
    try j_entries.appendSlice(a, &.{
        .{ .key = "schema", .value = .{ .int = 2 } },
        .{ .key = "run_id", .value = Py.s(run_id) },
        .{ .key = "state", .value = Py.s("created") },
        .{ .key = "backend", .value = Py.s(r.spec.backend) },
        .{ .key = "software", .value = Py.s(admin.abspath(a, r.spec.software)) },
        .{ .key = "modules", .value = Py.s(admin.abspath(a, r.spec.modules)) },
        .{ .key = "state_root", .value = Py.s(admin.abspath(a, r.spec.state_root)) },
        .{ .key = "generation_root", .value = Py.s(gen) },
        .{ .key = "eb_args", .value = pyList(a, r.spec.eb_args) },
        .{ .key = "eb_executable", .value = Py.s(r.spec.eb_executable) },
        .{ .key = "grace_hours", .value = .{ .float = r.spec.grace_hours } },
        .{ .key = "skip_live_check", .value = .{ .bool = r.spec.skip_live_check } },
        .{ .key = "require_exchange", .value = .{ .bool = r.spec.require_exchange } },
        .{ .key = "prepare_executable", .value = Py.opt(r.spec.prepare_executable) },
        .{ .key = "ledger_config", .value = Py.opt(r.spec.ledger_config) },
        .{ .key = "ledger_policy", .value = policy },
        .{ .key = "prepare_complete", .value = .{ .bool = false } },
        .{ .key = "prepare_returncode", .value = .null },
        .{ .key = "publication", .value = Py.s("directory-swap") },
        .{ .key = "module_rels", .value = pyList(a, rels) },
        .{ .key = "mount_points", .value = pyList(a, try mountPoints(r, learned.left_out)) },
        .{ .key = "created_at", .value = .{ .float = t } },
        .{ .key = "updated_at", .value = .{ .float = t } },
        .{ .key = "manifests", .value = .{ .dict = manifests.items } },
        .{ .key = "probes", .value = probes },
        .{ .key = "modules_plan", .value = .{ .list = &.{} } },
        .{ .key = "links_plan", .value = .{ .list = &.{} } },
        .{ .key = "message", .value = Py.s("") },
        .{ .key = "eb_returncode", .value = .null },
        .{ .key = "eb_command", .value = .null },
        .{ .key = "retired_at", .value = .null },
        .{ .key = "generation_removed_at", .value = .null },
    });
    if (r.spec.build_dir) |bd| try j_entries.append(a, .{ .key = "build_dir", .value = Py.s(admin.abspath(a, bd)) });
    r.journal = .{ .dict = j_entries.items };
    try r.save("journal.created");
    log("run {s} backend {s} generation {s}", .{ run_id, r.spec.backend, gen });
    return runLocked(r, true);
}

fn usable(probes: Py, key: []const u8) bool {
    const x = pyfmt.get(probes, key) orelse return false;
    return pyfmt.truthy(pyfmt.get(x, "usable"));
}

/// The spec a journal records, for resume and rollback.
fn specFromJournal(a: Allocator, j: Py, lock_timeout: ?f64) engine.Spec {
    var rels: std.ArrayList([]const u8) = .empty;
    if (pyfmt.get(j, "module_rels")) |v| if (v == .list) for (v.list) |x| if (x == .str) rels.append(a, x.str) catch oom();
    var args: std.ArrayList([]const u8) = .empty;
    if (pyfmt.get(j, "eb_args")) |v| if (v == .list) for (v.list) |x| if (x == .str) args.append(a, x.str) catch oom();
    const grace = pyfmt.get(j, "grace_hours");
    return .{
        .software = s(pyfmt.get(j, "software")),
        .modules = s(pyfmt.get(j, "modules")),
        .state_root = s(pyfmt.get(j, "state_root")),
        .eb_args = args.items,
        .backend = s(pyfmt.get(j, "backend")),
        .grace_hours = if (grace) |g| switch (g) {
            .float => |f| if (f != 0) f else admin.default_grace_hours,
            .int => |n| if (n != 0) @floatFromInt(n) else admin.default_grace_hours,
            else => admin.default_grace_hours,
        } else admin.default_grace_hours,
        .module_rels = rels.items,
        .eb_executable = if (s(pyfmt.get(j, "eb_executable")).len > 0) s(pyfmt.get(j, "eb_executable")) else "eb",
        .run_id = s(pyfmt.get(j, "run_id")),
        .skip_live_check = pyfmt.truthy(pyfmt.get(j, "skip_live_check")),
        .require_exchange = pyfmt.truthy(pyfmt.get(j, "require_exchange")),
        .lock_timeout = lock_timeout,
        .prepare_executable = if (pyfmt.get(j, "prepare_executable")) |p| (if (p == .str) p.str else null) else null,
        .ledger_config = if (pyfmt.get(j, "ledger_config")) |p| (if (p == .str and p.str.len > 0) p.str else null) else null,
        .build_dir = if (pyfmt.get(j, "build_dir")) |p| (if (p == .str and p.str.len > 0) p.str else null) else null,
    };
}

/// resume: finish or undo the run the journal names, as its state says,
/// under the state root's lock.
pub fn resumeRun(a: Allocator, env: probe.Env, ctx: *admin.Ctx, state_root: []const u8, run_id: []const u8, lock_timeout: ?f64) E!Result {
    if (!admin.plainRunId(run_id)) main_mod.refuse("run id is not a plain name: '{s}'", .{run_id});
    const path = try std.fs.path.join(a, &.{ state_root, "runs", run_id, "journal.json" });
    const st = fsx.statAt(fsx.cwd, path, false) catch {
        main_mod.refuse("no journal at {s}", .{path});
    };
    if (!st.isReg()) main_mod.refuse("no journal at {s}", .{path});
    var j = main_mod.loadJournal(a, path);
    if (!admin.matchingRunId(j, run_id))
        main_mod.refuse("journal run id does not match its directory", .{});
    if (std.mem.eql(u8, s(pyfmt.get(j, "kind")), "retire")) main_mod.refuse("run {s} is a retire record; rollback restores the module file", .{run_id});
    var r: R = .{ .a = a, .env = env, .ctx = ctx, .spec = specFromJournal(a, j, lock_timeout), .path = path };
    defer main_mod.last_why = r.why;
    engine.checkRoots(a, r.spec);
    const l = try lockRoot(&r);
    defer l.release();
    j = main_mod.loadJournal(a, path);
    if (!admin.matchingRunId(j, run_id))
        return r.intervene("journal run id does not match its directory", .{});
    r.journal = j;
    const state_name = r.str("state");
    var published = false;
    for (r.list("modules_plan")) |m| if (std.mem.eql(u8, s(pyfmt.get(m, "status")), "published")) {
        published = true;
    };
    if (std.mem.eql(u8, state_name, "complete")) {
        for (r.list("modules_plan")) |m| {
            if (std.mem.eql(u8, s(pyfmt.get(m, "status")), "restoring")) {
                return r.refuse("run {s} has an interrupted rollback; run rollback again", .{run_id});
            }
        }
        const m = try a.print("run {s} is already complete", .{run_id});
        return recordFinished(&r, try publisher(&r), m);
    }
    if (std.mem.eql(u8, state_name, "rolled_back")) return r.refuse("run {s} was rolled back; start a new run", .{run_id});
    if (std.mem.eql(u8, state_name, "created") or std.mem.eql(u8, state_name, "building") or std.mem.eql(u8, state_name, "failed")) {
        if (published) return r.intervene("run {s} is {s} but a module was already published", .{ run_id, state_name });
        log("resume {s} from {s}: rebuilding the unpublished generation", .{ run_id, state_name });
        return runLocked(&r, true);
    }
    if (std.mem.eql(u8, state_name, "built") or std.mem.eql(u8, state_name, "publishing")) {
        log("resume {s} from {s}: publishing", .{ run_id, state_name });
        return publishRecorded(&r);
    }
    return r.intervene("run {s} is in an unknown state {s}", .{ run_id, state_name });
}

/// rollback of a complete run journal, without a ledger.
pub fn rollbackRun(r: *R) E!Result {
    const a = r.a;
    const run_id = r.str("run_id");
    const config = try publisher(r);
    var finished: publish_mod.Corrects = .empty;
    var started = false;
    if (config) |c| {
        const left = try record(r, c, &.{}, null);
        if (left.len > 0 and required(c)) {
            const m = try a.print("run {s} was not rolled back: {s}", .{ run_id, publish_mod.pendingMessage(a, left, "run rollback again") });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
        var why: []const u8 = "";
        const data = publish_mod.load(a, runDir(a, r.str("state_root"), run_id), &why) catch |err| return switch (err) {
            error.OutOfMemory => error.OutOfMemory,
            error.NeedsIntervention => r.intervene("{s}", .{why}),
            error.Refused => r.refuse("{s}", .{why}),
        };
        for ((pyfmt.get(data, "events") orelse Py{ .list = &.{} }).list) |ev| {
            const rec = pyfmt.get(ev, "record") orelse continue;
            const stage = s(pyfmt.get(rec, "stage"));
            if (std.mem.eql(u8, stage, "started")) started = true;
            if (std.mem.eql(u8, s(pyfmt.get(ev, "status")), "acknowledged") and !std.mem.eql(u8, s(pyfmt.get(rec, "action")), "correction") and std.mem.eql(u8, stage, "finished")) {
                const mf = s(pyfmt.get(pyfmt.get(rec, "build") orelse Py{ .dict = &.{} }, "module_file"));
                const id = pyfmt.get(pyfmt.get(ev, "receipt") orelse Py{ .dict = &.{} }, "id") orelse .null;
                try finished.put(a, mf, id);
            }
        }
    }
    if (std.mem.eql(u8, r.str("state"), "rolled_back")) {
        var all = true;
        for (r.list("modules_plan")) |m| if (!std.mem.eql(u8, s(pyfmt.get(m, "status")), "restored")) {
            all = false;
        };
        if (all) {
            const m = try a.print("run {s} is already rolled back", .{run_id});
            return recordRollback(r, config, &finished, started, m);
        }
    }
    if (!std.mem.eql(u8, r.str("state"), "complete")) return r.refuse("run {s} is {s}; only a complete run can roll back", .{ run_id, r.str("state") });
    try checkLiveTrees(r);
    if (config) |c| if (finished.count() > 0) {
        const left = try record(r, c, try records(r, c, "started", &finished), "outbox.rollback_started");
        if (left.len > 0 and required(c)) {
            const m = try a.print("run {s} was not rolled back: {s}", .{ run_id, publish_mod.pendingMessage(a, left, "run rollback again") });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    };
    rollbackSteps(r) catch |err| {
        const first = r.why;
        restorePublished(r) catch return r.intervene("rollback failed and the published tree could not be restored: {s}", .{r.why});
        r.set("state", Py.s("complete"));
        r.set("message", Py.s("rollback interrupted; published tree restored"));
        r.saveRestore("journal.rollback-restored") catch
            return r.intervene("rollback failed and the published tree could not be restored: {s}", .{r.why});
        r.why = first;
        return err;
    };
    const m = try a.print("run {s} rolled back; replacement kept at its retired path", .{run_id});
    return recordRollback(r, config, &finished, started, m);
}

/// _check_live_trees: refuse to roll back a run whose tree is no longer the
/// live one. Each live tree has to be the run's, or the run's tree has to be
/// at its retired name already, where an exchange the journal did not
/// record put it. A later run that replaced the tree is rolled back first.
fn checkLiveTrees(r: *R) E!void {
    const a = r.a;
    const run_id = r.str("run_id");
    for (r.list("modules_plan")) |m| {
        const rel = s(pyfmt.get(m, "rel"));
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        const live_run = treeRunId(a, live);
        if (live_run != null and std.mem.eql(u8, live_run.?, run_id)) continue;
        const retired_run = treeRunId(a, retired);
        if (retired_run != null and std.mem.eql(u8, retired_run.?, run_id)) continue;
        if (live_run) |other| return r.refuse("run {s} cannot roll back {s}: {s} holds the tree of run {s}; roll back {s} first", .{ run_id, rel, live, other, other });
        return r.refuse("run {s} cannot roll back {s}: {s} is not the tree it published", .{ run_id, rel, live });
    }
}

/// _record_rollback: finish the correction, or abort a publication that
/// never finished. Then print the message once, with what is left.
fn recordRollback(r: *R, config: ?Py, finished: *const publish_mod.Corrects, started: bool, message: []const u8) E!Result {
    const c = config orelse {
        log("{s}", .{message});
        return .{ .code = 0, .message = message };
    };
    var recs: []const Py = &.{};
    if (finished.count() > 0) {
        recs = try records(r, c, "finished", finished);
    } else if (started) {
        recs = try records(r, c, "aborted", null);
    }
    const left = try record(r, c, recs, "outbox.rollback_finished");
    if (left.len > 0) {
        const m = try r.a.print("{s}; {s}", .{ message, publish_mod.pendingMessage(r.a, left, "run rollback again") });
        log("{s}", .{m});
        return .{ .code = publish_mod.pending_exit, .message = m };
    }
    log("{s}", .{message});
    return .{ .code = 0, .message = message };
}

fn rollbackSteps(r: *R) E!void {
    const a = r.a;
    const mods = r.list("modules_plan");
    var i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const rel = s(pyfmt.get(m, "rel"));
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        // An exchange the journal did not record already put the old tree
        // back: this run's tree is at the retired name.
        const lr = treeRunId(a, live);
        const rr = treeRunId(a, retired);
        const run_id = r.str("run_id");
        const done_already = !(lr != null and std.mem.eql(u8, lr.?, run_id)) and (rr != null and std.mem.eql(u8, rr.?, run_id));
        const method = if (done_already)
            (if (s(pyfmt.get(m, "rollback_method")).len > 0) s(pyfmt.get(m, "rollback_method")) else "recovered")
        else
            try swapPair(r, live, retired, r.faults(), try a.print("rollback.swap:{s}", .{rel}));
        r.setItem("modules_plan", i, "status", Py.s("restoring"));
        r.setItem("modules_plan", i, "rollback_method", Py.s(method));
        try r.save(try a.print("journal.rollback-swap:{s}", .{rel}));
    }
    i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const rel = s(pyfmt.get(m, "rel"));
        try restoreOldModule(r, m, r.faults());
        r.setItem("modules_plan", i, "status", Py.s("restored"));
        log("restored {s}", .{rel});
        try r.save(try a.print("journal.restore:{s}", .{rel}));
    }
    for (r.list("links_plan"), 0..) |l, k| {
        if (!pyfmt.truthy(pyfmt.get(l, "created"))) continue;
        const path = s(pyfmt.get(l, "live_path"));
        if (isKind(path, "symlink") and std.mem.eql(u8, readlinkText(a, path), s(pyfmt.get(l, "target")))) {
            try hit(r, r.faults(), try a.print("rollback.unlink:{s}", .{s(pyfmt.get(l, "rel"))}));
            sys.unlinkat(fsx.cwd, path, 0) catch {};
            atomic.fsyncDir(std.fs.path.dirname(path) orelse "/") catch {};
        }
        r.setItem("links_plan", k, "created", .{ .bool = false });
        r.setItem("links_plan", k, "status", Py.s("removed"));
    }
    r.set("state", Py.s("rolled_back"));
    r.set("rolled_back_at", .{ .float = now() });
    r.set("message", Py.s("rolled back"));
    try r.save("journal.rolled_back");
}

fn restorePublished(r: *R) E!void {
    const run_id = r.str("run_id");
    for (r.list("modules_plan"), 0..) |m, i| {
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        const lr = treeRunId(r.a, live);
        const rr = treeRunId(r.a, retired);
        const live_is = lr != null and std.mem.eql(u8, lr.?, run_id);
        const retired_is = rr != null and std.mem.eql(u8, rr.?, run_id);
        if (!live_is and retired_is) {
            _ = try swapPair(r, live, retired, .{}, try r.a.print("rollback-recover:{s}", .{s(pyfmt.get(m, "rel"))}));
        } else if (!(live_is and !retired_is)) {
            return r.intervene("cannot restore published tree for {s}", .{s(pyfmt.get(m, "rel"))});
        }
        try writeNewModule(r, m, .{});
        r.setItem("modules_plan", i, "status", Py.s("published"));
    }
    try createLinks(r, .{});
}

test "relsFromDryRun" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const rels = relsFromDryRun(a, " * [ ] /x/zlib-1.3.eb (module: zlib/1.3)\n * [R] /x/GCC.eb (module: Core | GCC/13)\nnoise\n * [F] /y.eb (module: a/b) \n");
    try std.testing.expectEqual(@as(usize, 3), rels.len);
    try std.testing.expectEqualStrings("Core/GCC/13", rels[1]);
}

// EasyBuild 5.4.2 on EESSI 2026.06, eb --dry-run [--robot] --rebuild
// pigz-2.8-GCCcore-15.2.0.eb; off a terminal, rich wraps at 80 columns.
const eessi_ecs = "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirerapids/software/EasyBuild/5.4.2/easybuild/easyconfigs";
const eessi_tail = "== Temporary log file(s) /tmp/eb-o62b5_19/easybuild-q30ibs1n.log* have been \nremoved.\n== Temporary directory /tmp/eb-o62b5_19 has been removed.\n";
const eessi_fold = "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/intel/sapphirera\npids/software/EasyBuild/5.4.2/easybuild/easyconfigs/";

test "relsFromDryRun reads a wrapped entry whole" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const text = "Dry run: printing build status of easyconfigs\n * [R] \n" ++ eessi_fold ++
        "p/pigz/pigz-2.8-GCCcore-15.2\n.0.eb (module: pigz/2.8-GCCcore-15.2.0)\n" ++ eessi_tail;
    const rels = relsFromDryRun(arena_state.allocator(), text);
    try std.testing.expectEqual(@as(usize, 1), rels.len);
    try std.testing.expectEqualStrings("pigz/2.8-GCCcore-15.2.0", rels[0]);
}

test "relsFromDryRun leaves out installed dependencies" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const header = "Dry run: printing build status of easyconfigs and dependencies\n";
    const whole = header ++
        " * [x] " ++ eessi_ecs ++ "/m/M4/M4-1.4.20.eb (module: M4/1.4.20)\n" ++
        " * [x] " ++ eessi_ecs ++ "/g/GCCcore/GCCcore-15.2.0.eb (module: GCCcore/15.2.0)\n" ++
        " * [R] " ++ eessi_ecs ++ "/p/pigz/pigz-2.8-GCCcore-15.2.0.eb (module: pigz/2.8-GCCcore-15.2.0)\n" ++ eessi_tail;
    const wrapped = header ++
        " * [x] \n" ++ eessi_fold ++ "m/M4/M4-1.4.20.eb (module: \nM4/1.4.20)\n" ++
        " * [x] \n" ++ eessi_fold ++ "g/GCCcore/GCCcore-15.2.0.eb \n(module: GCCcore/15.2.0)\n" ++
        " * [R] \n" ++ eessi_fold ++ "p/pigz/pigz-2.8-GCCcore-15.2\n.0.eb (module: pigz/2.8-GCCcore-15.2.0)\n" ++ eessi_tail;
    for ([_][]const u8{ whole, wrapped }) |text| {
        const rels = relsFromDryRun(a, text);
        try std.testing.expectEqual(@as(usize, 1), rels.len);
        try std.testing.expectEqualStrings("pigz/2.8-GCCcore-15.2.0", rels[0]);
    }
}

test "relsFromDryRun ends an entry at the next entry or log line" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const rels = relsFromDryRun(arena_state.allocator(), " * [R] /x/a.eb (module: a/1\n * [ ] /x/b.eb (module: \nb/1)\n * [F] /x/c.eb\n== done\n(module: c/1)\n");
    try std.testing.expectEqual(@as(usize, 1), rels.len);
    try std.testing.expectEqualStrings("b/1", rels[0]);
}

// EasyBuild 5.4.2, eb --extended-dry-run Top-1.0-GCCcore-15.2.0.eb
// --robot=$HOME/easyconfigs: --rebuild --stop=fetch --ignore-locks, where Top
// needs Dep and neither is installed: the output of each easyconfig up to its
// first step, and the summary; whole, and wrapped at 24 columns.
const extended_whole =
    "== resolving dependencies ...\n" ++
    "\n" ++
    "== processing EasyBuild easyconfig /home/alice/easyconfigs/Dep-1.0-GCCcore-15.2.0.eb\n" ++
    "\n" ++
    "Important note: the actual build & install procedure that will be performed may diverge\n" ++
    "(slightly) from what is outlined below, due to conditions in the easyblock which are\n" ++
    "incorrectly handled in a dry run.\n" ++
    "Any errors that may occur are ignored and reported as warnings, on a per-step basis.\n" ++
    "Please be aware of this, and only use the information below for quick debugging purposes.\n" ++
    "\n" ++
    "*** DRY RUN using 'Binary' easyblock (easybuild.easyblocks.generic.binary @ /opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/binary.py) ***\n" ++
    "\n" ++
    "== building and installing Dep/1.0-GCCcore-15.2.0...\n" ++
    "  >> installation prefix: /tmp/eb-0yw4vdhi/__ROOT__/opt/site/software/Dep/1.0-GCCcore-15.2.0\n" ++
    "fetching files and verifying checksums... [DRY RUN]\n" ++
    "\n" ++
    "== processing EasyBuild easyconfig /home/alice/easyconfigs/Top-1.0-GCCcore-15.2.0.eb\n" ++
    "\n" ++
    "Important note: the actual build & install procedure that will be performed may diverge\n" ++
    "(slightly) from what is outlined below, due to conditions in the easyblock which are\n" ++
    "incorrectly handled in a dry run.\n" ++
    "Any errors that may occur are ignored and reported as warnings, on a per-step basis.\n" ++
    "Please be aware of this, and only use the information below for quick debugging purposes.\n" ++
    "\n" ++
    "*** DRY RUN using 'Binary' easyblock (easybuild.easyblocks.generic.binary @ /opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/binary.py) ***\n" ++
    "\n" ++
    "== building and installing Top/1.0-GCCcore-15.2.0...\n" ++
    "  >> installation prefix: /tmp/eb-0yw4vdhi/__ROOT__/opt/site/software/Top/1.0-GCCcore-15.2.0\n" ++
    "fetching files and verifying checksums... [DRY RUN]\n" ++
    "\n" ++
    "== Build succeeded for 2 out of 2 (total: 0 secs)\n" ++
    "== Summary:\n" ++
    "   * [SUCCESS] Dep/1.0-GCCcore-15.2.0\n" ++
    "   * [SUCCESS] Top/1.0-GCCcore-15.2.0\n";
const extended_24 =
    "== resolving \ndependencies ...\n" ++
    "\n" ++
    "== processing EasyBuild \neasyconfig \n/home/alice/easyconfigs/\nDep-1.0-GCCcore-15.2.0.e\nb\n" ++
    "\n" ++
    "Important note: the \nactual build & install \nprocedure that will be \nperformed may diverge\n" ++
    "(slightly) from what is \noutlined below, due to \nconditions in the \neasyblock which are\n" ++
    "incorrectly handled in a \ndry run.\n" ++
    "Any errors that may \noccur are ignored and \nreported as warnings, on \na per-step basis.\n" ++
    "Please be aware of this, \nand only use the \ninformation below for \nquick debugging \npurposes.\n" ++
    "\n" ++
    "*** DRY RUN using \n'Binary' easyblock \n(easybuild.easyblocks.ge\nneric.binary @ \n/opt/easybuild/lib/pytho\nn3.12/site-packages/easy\nbuild/easyblocks/generic\n/binary.py) ***\n" ++
    "\n" ++
    "== building and \ninstalling \nDep/1.0-GCCcore-15.2.0..\n.\n" ++
    "  >> installation \nprefix: \n/tmp/eb-0yw4vdhi/__ROOT_\n_/opt/site/software/Dep/\n1.0-GCCcore-15.2.0\n" ++
    "fetching files and \nverifying checksums... \n[DRY RUN]\n" ++
    "\n" ++
    "== processing EasyBuild \neasyconfig \n/home/alice/easyconfigs/\nTop-1.0-GCCcore-15.2.0.e\nb\n" ++
    "\n" ++
    "Important note: the \nactual build & install \nprocedure that will be \nperformed may diverge\n" ++
    "(slightly) from what is \noutlined below, due to \nconditions in the \neasyblock which are\n" ++
    "incorrectly handled in a \ndry run.\n" ++
    "Any errors that may \noccur are ignored and \nreported as warnings, on \na per-step basis.\n" ++
    "Please be aware of this, \nand only use the \ninformation below for \nquick debugging \npurposes.\n" ++
    "\n" ++
    "*** DRY RUN using \n'Binary' easyblock \n(easybuild.easyblocks.ge\nneric.binary @ \n/opt/easybuild/lib/pytho\nn3.12/site-packages/easy\nbuild/easyblocks/generic\n/binary.py) ***\n" ++
    "\n" ++
    "== building and \ninstalling \nTop/1.0-GCCcore-15.2.0..\n.\n" ++
    "  >> installation \nprefix: \n/tmp/eb-0yw4vdhi/__ROOT_\n_/opt/site/software/Top/\n1.0-GCCcore-15.2.0\n" ++
    "fetching files and \nverifying checksums... \n[DRY RUN]\n" ++
    "\n" ++
    "== Build succeeded for 2 \nout of 2 (total: 0 secs)\n" ++
    "== Summary:\n" ++
    "   * [SUCCESS] \nDep/1.0-GCCcore-15.2.0\n" ++
    "   * [SUCCESS] \nTop/1.0-GCCcore-15.2.0\n";

test "builtByExtendedDryRun names the modules it builds" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    for ([_][]const u8{ extended_whole, extended_24 }) |text| {
        const built = builtByExtendedDryRun(a, text);
        try std.testing.expectEqual(@as(usize, 2), built.len);
        try std.testing.expectEqualStrings("Dep/1.0-GCCcore-15.2.0", built[0]);
        try std.testing.expectEqualStrings("Top/1.0-GCCcore-15.2.0", built[1]);
    }
    try std.testing.expectEqual(@as(usize, 0), builtByExtendedDryRun(a, "== building and installing a/1\n== done\n").len);
}

test "dryRunModules keeps the status of each entry" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const modules = dryRunModules(arena_state.allocator(), " * [x] /x/M4.eb (module: M4/1.4.20)\n * [ ] /x/zlib.eb (module: \nzlib/1.3.1)\n * [R] /x/a.eb (module: a/1)\n");
    try std.testing.expectEqual(@as(usize, 3), modules.len);
    try std.testing.expectEqual(@as(u8, 'x'), modules[0].status);
    try std.testing.expectEqual(@as(u8, ' '), modules[1].status);
    try std.testing.expectEqualStrings("zlib/1.3.1", modules[1].rel);
    try std.testing.expectEqual(@as(u8, 'R'), modules[2].status);
}

// EasyBuild 5.4.2 with rich 15.0.0, FORCE_COLOR=1 and TERM=xterm-256color,
// the output that tests/test_dry_run_lines.py has as COLOR_REBUILD,
// COLOR_ROBOT and COLOR_EXTENDED. Once FORCE_COLOR is set and not empty,
// rich writes ANSI escape sequences to a pipe too, and still wraps the lines
// at 80 columns.
const color_rebuild =
    "== Temporary log file in case of crash /tmp/eb-0uh_10us/easybuild-vkclmi_q.log\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "Dry run: printing build status of easyconfigs\n" ++
    " * \x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m \x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m\n" ++
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-0uh_10us/\x1b[0m\x1b[95measybuild-vkclmi_q.log\x1b[0m* have been \n" ++
    "removed.\n" ++
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-0uh_10us\x1b[0m has been removed.\n";
const color_robot =
    "== Temporary log file in case of crash /tmp/eb-x1zw5cqf/easybuild-4u5bzs64.log\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "Dry run: printing build status of easyconfigs and dependencies\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.20.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/z/zlib/\x1b[0m\x1b[95mzlib-1.3.1.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "zlib/\x1b[1;36m1.3\x1b[0m.\x1b[1;36m1\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.19.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m19\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/b/Bison/\x1b[0m\x1b[95mBison-3.8.2.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "Bison/\x1b[1;36m3.8\x1b[0m.\x1b[1;36m2\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/f/flex/\x1b[0m\x1b[95mflex-2.6.4.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "flex/\x1b[1;36m2.6\x1b[0m.\x1b[1;36m4\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/b/binutils/\x1b[0m\x1b[95mbinutils-2.45.eb\x1b[0m \x1b[1m(\x1b[0mmodule:\n" ++
    "binutils/\x1b[1;36m2.45\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0m \x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/g/GCCcore/\x1b[0m\x1b[95mGCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule:\n" ++
    "GCCcore/\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m\n" ++
    " * \x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m \x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m \x1b[1m(\x1b[0mmodule: \n" ++
    "Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m\n" ++
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-x1zw5cqf/\x1b[0m\x1b[95measybuild-4u5bzs64.log\x1b[0m* have been \n" ++
    "removed.\n" ++
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-x1zw5cqf\x1b[0m has been removed.\n";
const color_extended =
    "== Temporary log file in case of crash /tmp/eb-tz2pzp7y/easybuild-_v17l6i5.log\n" ++
    "file \x1b[35m/tmp/eb-tz2pzp7y/\x1b[0m\x1b[95mfake_vsc_mleiqnwz\x1b[0m removed\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m\n" ++
    "== resolving dependencies \x1b[33m...\x1b[0m\n" ++
    "\n" ++
    "== processing EasyBuild easyconfig \n" ++
    "\x1b[35m/opt/easybuild/easybuild/easyconfigs/m/M4/\x1b[0m\x1b[95mM4-1.4.20.eb\x1b[0m\n" ++
    "\n" ++
    "Important note: the actual build & install procedure that will be performed may \n" ++
    "diverge\n" ++
    "\x1b[1m(\x1b[0mslightly\x1b[1m)\x1b[0m from what is outlined below, due to conditions in the easyblock which\n" ++
    "are\n" ++
    "incorrectly handled in a dry run.\n" ++
    "Any errors that may occur are ignored and reported as warnings, on a per-step \n" ++
    "basis.\n" ++
    "Please be aware of this, and only use the information below for quick debugging \n" ++
    "purposes.\n" ++
    "\n" ++
    "*** DRY RUN using \x1b[32m'ConfigureMake'\x1b[0m easyblock \n" ++
    "\x1b[1m(\x1b[0measybuild.easyblocks.generic.configuremake @ \n" ++
    "\x1b[35m/opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/\x1b[0m\x1b[95mconfigu\x1b[0m\n" ++
    "\x1b[95mremake.py\x1b[0m\x1b[1m)\x1b[0m ***\n" ++
    "\n" ++
    "== building and installing M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m\x1b[33m...\x1b[0m\n" ++
    "  >> installation prefix: \x1b[35m/tmp/eb-tz2pzp7y/__ROOT__/opt/site/software/M4/\x1b[0m\x1b[95m1.4.20\x1b[0m\n" ++
    "fetching files and verifying checksums\x1b[33m...\x1b[0m \x1b[1m[\x1b[0mDRY RUN\x1b[1m]\x1b[0m\n" ++
    "== processing EasyBuild easyconfig \n" ++
    "\x1b[35m/home/alice/easyconfigs/\x1b[0m\x1b[95mExample-1.0-GCCcore-15.2.0.eb\x1b[0m\n" ++
    "\n" ++
    "Important note: the actual build & install procedure that will be performed may \n" ++
    "diverge\n" ++
    "\x1b[1m(\x1b[0mslightly\x1b[1m)\x1b[0m from what is outlined below, due to conditions in the easyblock which\n" ++
    "are\n" ++
    "incorrectly handled in a dry run.\n" ++
    "Any errors that may occur are ignored and reported as warnings, on a per-step \n" ++
    "basis.\n" ++
    "Please be aware of this, and only use the information below for quick debugging \n" ++
    "purposes.\n" ++
    "\n" ++
    "*** DRY RUN using \x1b[32m'Binary'\x1b[0m easyblock \x1b[1m(\x1b[0measybuild.easyblocks.generic.binary @ \n" ++
    "\x1b[35m/opt/easybuild/lib/python3.12/site-packages/easybuild/easyblocks/generic/\x1b[0m\x1b[95mbinary.\x1b[0m\n" ++
    "\x1b[95mpy\x1b[0m\x1b[1m)\x1b[0m ***\n" ++
    "\n" ++
    "== building and installing Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[33m...\x1b[0m\n" ++
    "  >> installation prefix: \n" ++
    "\x1b[35m/tmp/eb-tz2pzp7y/__ROOT__/opt/site/software/Example/\x1b[0m\x1b[95m1.0-GCCcore-15.2.0\x1b[0m\n" ++
    "fetching files and verifying checksums\x1b[33m...\x1b[0m \x1b[1m[\x1b[0mDRY RUN\x1b[1m]\x1b[0m\n" ++
    "== Build succeeded for \x1b[1;36m8\x1b[0m out of \x1b[1;36m8\x1b[0m \x1b[1m(\x1b[0mtotal: \x1b[1;36m7\x1b[0m mins \x1b[1;36m0\x1b[0m secs\x1b[1m)\x1b[0m\n" ++
    "== Summary:\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m20\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m zlib/\x1b[1;36m1.3\x1b[0m.\x1b[1;36m1\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m M4/\x1b[1;36m1.4\x1b[0m.\x1b[1;36m19\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m Bison/\x1b[1;36m3.8\x1b[0m.\x1b[1;36m2\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m flex/\x1b[1;36m2.6\x1b[0m.\x1b[1;36m4\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m binutils/\x1b[1;36m2.45\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m GCCcore/\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\n" ++
    "   * \x1b[1m[\x1b[0mSUCCESS\x1b[1m]\x1b[0m Example/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\n" ++
    "== Temporary log \x1b[1;35mfile\x1b[0m\x1b[1m(\x1b[0ms\x1b[1m)\x1b[0m \x1b[35m/tmp/eb-tz2pzp7y/\x1b[0m\x1b[95measybuild-_v17l6i5.log\x1b[0m* have been \n" ++
    "removed.\n" ++
    "== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-tz2pzp7y\x1b[0m has been removed.\n";

test "relsFromDryRun and builtByExtendedDryRun read rich's coloured output" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const rebuild = relsFromDryRun(a, color_rebuild);
    try std.testing.expectEqual(@as(usize, 1), rebuild.len);
    try std.testing.expectEqualStrings("Example/1.0-GCCcore-15.2.0", rebuild[0]);
    const want = [_][]const u8{ "M4/1.4.20", "zlib/1.3.1", "M4/1.4.19", "Bison/3.8.2", "flex/2.6.4", "binutils/2.45", "GCCcore/15.2.0", "Example/1.0-GCCcore-15.2.0" };
    const robot = dryRunModules(a, color_robot);
    try std.testing.expectEqual(want.len, robot.len);
    for (want, robot, 1..) |rel, m, n| {
        try std.testing.expectEqualStrings(rel, m.rel);
        try std.testing.expectEqual(@as(u8, if (n < want.len) ' ' else 'R'), m.status);
    }
    const built = builtByExtendedDryRun(a, color_extended);
    try std.testing.expectEqual(@as(usize, 2), built.len);
    try std.testing.expectEqualStrings("M4/1.4.20", built[0]);
    try std.testing.expectEqualStrings("Example/1.0-GCCcore-15.2.0", built[1]);
}

test "stripEscapes leaves out whole CSI and OSC sequences only" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    // The cases of ESCAPES in tests/test_dry_run_lines.py.
    const cases = [_][2][]const u8{
        .{ "", "" },
        .{ "plain", "plain" },
        .{ "\x1b[1m[\x1b[0mR\x1b[1m]\x1b[0m", "[R]" },
        .{ "\x1b[4;94mhttps://x\x1b[0m", "https://x" },
        .{ "a\x1b[ qb", "ab" },
        .{ "\x1b]8;;file:///a\x1b\\a\x1b]8;;\x1b\\", "a" },
        .{ "\x1b]0;title\x07x", "x" },
        .{ "a\x1b[12", "a\x1b[12" },
        .{ "a\x1b[1\nm", "a\x1b[1\nm" },
        .{ "a\x1b]8;;x", "a\x1b]8;;x" },
        .{ "\x1b]8;;x\ny\x07", "\x1b]8;;x\ny\x07" },
        .{ "\x1b(B", "\x1b(B" },
        .{ "\x1b", "\x1b" },
        .{ "\x1b\x1b[1mx", "\x1bx" },
        .{ "\x1b]x\x1b[1my\x07", "\x1b]xy\x07" },
        .{ "\x1b[1m\u{e9}\x1b[0m", "\u{e9}" },
    };
    for (cases) |c| try std.testing.expectEqualStrings(c[1], stripEscapes(a, c[0]));
    const rels = relsFromDryRun(a, " * [R] \x1b]8;;file:///x/a.eb\x1b\\/x/a.eb\x1b]8;;\x1b\\ (module: \x1b]8;;x\x07a/1\x1b]8;;\x07)\n");
    try std.testing.expectEqual(@as(usize, 1), rels.len);
    try std.testing.expectEqualStrings("a/1", rels[0]);
}

test "dryRunEnv asks EasyBuild for basic output, and the build keeps the caller's" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const T = struct {
        fn style(envp: common.proc.Envp) ?[]const u8 {
            var i: usize = 0;
            while (envp[i]) |entry| : (i += 1) {
                const line = std.mem.span(entry);
                if (std.mem.startsWith(u8, line, "EASYBUILD_OUTPUT_STYLE=")) return line["EASYBUILD_OUTPUT_STYLE=".len..];
            }
            return null;
        }
    };
    const parent: common.proc.Envp = &[_:null]?[*:0]const u8{ "PATH=/bin", "FORCE_COLOR=0", "EASYBUILD_OUTPUT_STYLE=rich" };
    const env: probe.Env = .{ .envp = parent };
    var ctx: admin.Ctx = .{ .a = a, .env = env, .pause = .{ .env = env } };
    var r: R = .{ .a = a, .env = env, .ctx = &ctx, .spec = .{ .software = "/s", .modules = "/m", .state_root = "/st", .eb_args = &.{} } };
    try std.testing.expectEqualStrings("basic", T.style(try dryRunEnv(&r)).?);
    try std.testing.expectEqualStrings("rich", T.style(try childEnv(&r, env, "/gen/software", "/gen/modules", "/m")).?);
}

test "childEnv puts the live module files first on MODULEPATH" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const T = struct {
        fn modulepath(alloc: Allocator, parent: common.proc.Envp, eb_args: []const []const u8) !?[]const u8 {
            const env: probe.Env = .{ .envp = parent };
            var ctx: admin.Ctx = .{ .a = alloc, .env = env, .pause = .{ .env = env } };
            var r: R = .{ .a = alloc, .env = env, .ctx = &ctx, .spec = .{ .software = "/s", .modules = "/live/modules", .state_root = "/st", .eb_args = eb_args } };
            const envp = try childEnv(&r, env, "/gen/software", "/gen/modules", "/live/modules");
            var i: usize = 0;
            while (envp[i]) |entry| : (i += 1) {
                const line = std.mem.span(entry);
                if (std.mem.startsWith(u8, line, "MODULEPATH=")) return line["MODULEPATH=".len..];
            }
            return null;
        }
    };
    const expect = std.testing.expectEqualStrings;
    try expect("/live/modules/all:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{"MODULEPATH=/m"}, &.{"demo-1.eb"})).?);
    try expect("/live/modules/all", (try T.modulepath(a, &[_:null]?[*:0]const u8{"PATH=/bin"}, &.{})).?);
    try expect("/live/modules/all:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{"MODULEPATH=/live/modules/all:/m"}, &.{})).?);
    try expect("/live/modules/mods:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{ "MODULEPATH=/m", "EASYBUILD_SUFFIX_MODULES_PATH=mods" }, &.{})).?);
    try expect("/live/modules:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{ "MODULEPATH=/m", "EASYBUILD_SUFFIX_MODULES_PATH=" }, &.{})).?);
    try expect("/abs/two:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{ "MODULEPATH=/m", "EASYBUILD_SUFFIX_MODULES_PATH=mods" }, &.{ "demo-1.eb", "--suffix-modules-path", "one", "--suffix-modules-path=/abs/two" })).?);
    try expect("/live/modules/one:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{"MODULEPATH=/m"}, &.{ "--suffix-modules-path", "one" })).?);
    // EasyBuild never sees this one: filter_eb_args drops it as the value of --prefix.
    try expect("/live/modules/all:/m", (try T.modulepath(a, &[_:null]?[*:0]const u8{"MODULEPATH=/m"}, &.{ "--prefix", "--suffix-modules-path=x" })).?);
}

test "childEnv fails rather than fall back to the parent environment" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const parent: common.proc.Envp = &[_:null]?[*:0]const u8{ "PATH=/bin", "EASYBUILD_PREFIX=/default", "MODULEPATH=/m" };
    const env: probe.Env = .{ .envp = parent };
    var ctx: admin.Ctx = .{ .a = arena_state.allocator(), .env = env, .pause = .{ .env = env } };
    // Every allocation failure point either fails the call or still yields
    // an environment that names the staged install paths.
    var fail_index: usize = 0;
    while (true) : (fail_index += 1) {
        var failing = std.testing.FailingAllocator.init(arena_state.allocator(), .{ .fail_index = fail_index });
        var r: R = .{ .a = failing.allocator(), .env = env, .ctx = &ctx, .spec = .{ .software = "/s", .modules = "/m2", .state_root = "/st", .eb_args = &.{} } };
        const envp = childEnv(&r, env, "/gen/software", "/gen/modules", "/live/modules") catch |err| {
            try std.testing.expectEqual(error.OutOfMemory, err);
            continue;
        };
        try std.testing.expect(envp != parent);
        var saw = false;
        var i: usize = 0;
        while (envp[i]) |entry| : (i += 1) {
            const line = std.mem.span(entry);
            if (std.mem.eql(u8, line, "EASYBUILD_INSTALLPATH_SOFTWARE=/gen/software")) saw = true;
            try std.testing.expect(!std.mem.startsWith(u8, line, "EASYBUILD_PREFIX="));
        }
        try std.testing.expect(saw);
        if (!failing.has_induced_failure) break;
    }
}

test "childEnvBuilder unsets EASYBUILD_BUILDPATH only when drop_buildpath is true" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const parent: common.proc.Envp = &[_:null]?[*:0]const u8{ "PATH=/bin", "EASYBUILD_BUILDPATH=/keep/me" };
    const env: probe.Env = .{ .envp = parent };
    var ctx: admin.Ctx = .{ .a = a, .env = env, .pause = .{ .env = env } };
    var r_keep: R = .{ .a = a, .env = env, .ctx = &ctx, .spec = .{ .software = "/s", .modules = "/m", .state_root = "/st", .eb_args = &.{} } };
    const b_keep = try childEnvBuilder(&r_keep, env, "/gen/software", "/gen/modules", "/m", false);
    try std.testing.expectEqualStrings("/keep/me", b_keep.get("EASYBUILD_BUILDPATH").?);
    const b_drop = try childEnvBuilder(&r_keep, env, "/gen/software", "/gen/modules", "/m", true);
    try std.testing.expect(b_drop.get("EASYBUILD_BUILDPATH") == null);
}

test "notADirectory says what the path is instead" {
    try std.testing.expectEqualStrings("missing", notADirectory("missing"));
    try std.testing.expectEqualStrings("a file", notADirectory("file"));
    try std.testing.expectEqualStrings("a symlink", notADirectory("symlink"));
    try std.testing.expectEqualStrings("a special file", notADirectory("other"));
}

test "availableBytes falls back to block_size and multiplies in u128 without overflow" {
    var info = std.mem.zeroes(StatFs);
    info.blocks_available = 10;
    info.block_size = 4096;
    info.fragment_size = 0;
    try std.testing.expectEqual(@as(u128, 40960), availableBytes(info));

    info.fragment_size = 1 << 30;
    info.blocks_available = 1 << 40;
    try std.testing.expectEqual(@as(u128, 1) << 70, availableBytes(info));
}
