//! eb-staged-rebuild: build EasyBuild software in a staged tree, then
//! exchange the complete tree with the stable live directory.
//!
//! The commands in one static binary: probe, run,
//! resume, verify, rollback, gc, retire, status, list and resolve.
//! COMPAT.md lists what differs from the earlier Python implementation.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");
const admin = @import("admin.zig");
const engine = @import("engine.zig");
const generations = @import("generations.zig");
const build_options = @import("build_options");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const cli = common.cli;
const argparse = common.argparse;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// The version build.zig.zon gives; --version prints it.
pub const version = build_options.version;
const prog = "eb-staged-rebuild";

/// What --version prints. Python prints the first two words; the parenthetical
/// is the build, and tests/differential.sh checks the prefix.
const version_text = cli.versionLine(prog, version, build_options.commit);

const top_usage =
    \\usage: eb-staged-rebuild [-h] [--version]
    \\                         {probe,run,resume,verify,rollback,gc,retire,status,list,resolve} ...
    \\
;

const top_help = top_usage ++
    \\
    \\Build EasyBuild software in a staged tree, then exchange the complete tree
    \\with the stable live directory.
    \\
    \\positional arguments:
    \\  {probe,run,resume,verify,rollback,gc,retire,status,list,resolve}
    \\    probe               report bwrap, user namespaces, RENAME_EXCHANGE, and
    \\                        EasyBuild
    \\    run                 build and atomically swap a staged tree
    \\    resume              continue a run after a crash
    \\    verify              check a built run's stage before it is published
    \\    rollback            switch back to the previous generation or a named one,
    \\                        or put a retired module file back
    \\    gc                  remove hidden retired siblings after the grace period
    \\    retire              move a module file out of the module tree after its
    \\                        package is published elsewhere
    \\    status              show run journals
    \\    list                show each install path's live tree and the generations
    \\                        kept beside it
    \\    resolve             print the install path a module file names
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --version             show program's version number and exit
    \\
;

const probe_usage =
    \\usage: eb-staged-rebuild probe [-h] [--path PATH] [--no-create] [--eb EB]
    \\                               [--require-eb] [--json]
    \\
;
const probe_help = probe_usage ++
    \\
    \\options:
    \\  -h, --help    show this help message and exit
    \\  --path PATH   directory on the filesystem to probe (default: .)
    \\  --no-create   refuse when --path does not exist instead of creating it for
    \\                the probe
    \\  --eb EB
    \\  --require-eb  refuse when the EasyBuild command is not available
    \\  --json
    \\
;
const status_usage = "usage: eb-staged-rebuild status [-h] --state-root STATE_ROOT [--json] [run_id]\n";
/// argparse wraps this once --state-root has a default; the required form above stays one line.
const status_usage_default =
    \\usage: eb-staged-rebuild status [-h] [--state-root STATE_ROOT] [--json]
    \\                                [run_id]
    \\
;
const status_options =
    \\
    \\positional arguments:
    \\  run_id
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --json                print one JSON document
    \\
;
const status_help = status_usage ++ status_options;
const status_help_default = status_usage_default ++ status_options;
const list_usage = "usage: eb-staged-rebuild list [-h] --state-root STATE_ROOT\n";
const list_help = list_usage ++
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\
;
const resolve_usage = "usage: eb-staged-rebuild resolve [-h] module\n";
const resolve_help = resolve_usage ++
    \\
    \\positional arguments:
    \\  module
    \\
    \\options:
    \\  -h, --help  show this help message and exit
    \\
;

/// "eb-staged-rebuild: MESSAGE" on stderr, status 2: a refusal or an OSError.
pub fn refuse(comptime f: []const u8, args: anytype) noreturn {
    cli.err(prog ++ ": " ++ f ++ "\n", args);
    std.process.exit(2);
}

fn quoted(gpa: Allocator, s: []const u8) Allocator.Error![]u8 {
    return argparse.reprToken(gpa, s);
}

/// --grace-hours: Python float(), then a number of hours, 0 or more.
fn validateGrace(gpa: Allocator, raw: []const u8) Allocator.Error!argparse.ValidationResult {
    const t = std.mem.trim(u8, raw, " \t\n\r\x0b\x0c");
    const hours = std.fmt.parseFloat(f64, t) catch {
        const q = try quoted(gpa, raw);
        defer gpa.free(q);
        return .{ .err = try std.fmt.allocPrint(gpa, "invalid float value: {s}", .{q}) };
    };
    if (!isHours(hours)) {
        const q = try quoted(gpa, raw);
        defer gpa.free(q);
        return .{ .err = try std.fmt.allocPrint(gpa, "not a number of hours, 0 or more: {s}", .{q}) };
    }
    return .ok;
}

/// --lock-timeout: a finite float. nan and infinity are rejected, negatives are not.
fn validateLock(gpa: Allocator, raw: []const u8) Allocator.Error!argparse.ValidationResult {
    _ = parseLockTimeout(raw) catch |err| switch (err) {
        error.InvalidFloat => {
            const q = try quoted(gpa, raw);
            defer gpa.free(q);
            return .{ .err = try std.fmt.allocPrint(gpa, "invalid float value: {s}", .{q}) };
        },
        error.NotFinite => return .{ .err = try gpa.dupe(u8, "--lock-timeout is not a finite number") },
    };
    return .ok;
}

/// --min-free-bytes: a Python-looking integer, 0 or more, within u128.
fn validateBytes(gpa: Allocator, raw: []const u8) Allocator.Error!argparse.ValidationResult {
    _ = parseBytes(raw) catch |err| switch (err) {
        error.NotAnInteger => {
            const q = try quoted(gpa, raw);
            defer gpa.free(q);
            return .{ .err = try std.fmt.allocPrint(gpa, "invalid integer value: {s}", .{q}) };
        },
        error.BelowZero => {
            const q = try quoted(gpa, raw);
            defer gpa.free(q);
            return .{ .err = try std.fmt.allocPrint(gpa, "not a number of bytes, 0 or more: {s}", .{q}) };
        },
    };
    return .ok;
}

const grace_opt: argparse.OptionSpec = .{ .name = "grace-hours", .kind = .value, .validate = validateGrace };
const lock_opt: argparse.OptionSpec = .{ .name = "lock-timeout", .kind = .value, .validate = validateLock };
const bytes_opt: argparse.OptionSpec = .{ .name = "min-free-bytes", .kind = .value, .validate = validateBytes };
const module_opt: argparse.OptionSpec = .{ .name = "module", .kind = .append };
const backend_opt: argparse.OptionSpec = .{ .name = "backend", .kind = .value, .choices = &backends };

fn stateOpt(has_default: bool) argparse.OptionSpec {
    return .{
        .name = "state-root",
        .kind = .value,
        .required = !has_default,
        .has_default = has_default,
    };
}

// ---- resolve -----------------------------------------------------------

/// lstat_kind: missing, symlink, dir, file or other.
pub fn lstatKind(path: []const u8) []const u8 {
    const st = fsx.statAt(fsx.cwd, path, false) catch return "missing";
    if (st.isLink()) return "symlink";
    if (st.isDir()) return "dir";
    if (st.isReg()) return "file";
    return "other";
}

/// generation_complete: a real directory with PAYLOAD, the tool's stamp or
/// an easybuild/*.eb file.
fn generationComplete(a: Allocator, path: []const u8) bool {
    if (!std.mem.eql(u8, lstatKind(path), "dir")) return false;
    for ([_][]const u8{ "PAYLOAD", ".eb-staged-rebuild-complete" }) |n| {
        const p = std.fs.path.join(a, &.{ path, n }) catch return false;
        if (std.mem.eql(u8, lstatKind(p), "file")) return true;
    }
    const meta = std.fs.path.join(a, &.{ path, "easybuild" }) catch return false;
    if (!std.mem.eql(u8, lstatKind(meta), "dir")) return false;
    const fd = fsx.openDir(fsx.cwd, meta, false) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    while (it.next() catch return false) |e| {
        if (!std.mem.endsWith(u8, e.name, ".eb")) continue;
        const p = std.fs.path.join(a, &.{ meta, e.name }) catch continue;
        if (std.mem.eql(u8, lstatKind(p), "file")) return true;
    }
    return false;
}

/// Python's str() of an errno with a filename: "[Errno N] text: 'path'",
/// the path as repr() gives it.
pub fn errnoPath(a: Allocator, err: linux.E, path: []const u8) []const u8 {
    return common.pytext.osErrorErrno(a, err, path) catch oom();
}

/// Python's str() of an OSError with a filename. Other errors are named.
pub fn osError(a: Allocator, err: anyerror, path: []const u8) []const u8 {
    return common.pytext.osError(a, err, path) catch oom();
}

fn cmdResolve(a: Allocator, sub: *const argparse.Parsed) u8 {
    const path = sub.positional(0).?;
    const kind = lstatKind(path);
    var described: std.ArrayList(Py.Entry) = .empty;
    described.append(a, .{ .key = "path", .value = Py.s(path) }) catch oom();
    described.append(a, .{ .key = "kind", .value = Py.s(kind) }) catch oom();
    var complete = false;
    if (!std.mem.eql(u8, kind, "file")) {
        described.append(a, .{ .key = "complete", .value = .{ .bool = false } }) catch oom();
        described.append(a, .{ .key = "root", .value = .null }) catch oom();
    } else {
        // Opened as Python's open() does: following a link, which lstat
        // above has already ruled out.
        const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("{s}", .{osError(a, err, path)});
        defer sys.close(f.fd);
        const data = fsx.readAll(a, f.fd, 64 << 20) catch |err| refuse("{s}", .{osError(a, err, path)});
        if (!std.unicode.utf8ValidateSlice(data)) refuse("{s} is not UTF-8 text", .{path});
        const root = modulefile.parseRoot(data);
        complete = root != null and root.?.path.len > 0 and generationComplete(a, root.?.path);
        described.append(a, .{ .key = "syntax", .value = if (root) |r| Py.s(@tagName(r.syntax)) else .null }) catch oom();
        described.append(a, .{ .key = "root", .value = if (root) |r| Py.s(r.path) else .null }) catch oom();
        described.append(a, .{ .key = "root_kind", .value = if (root) |r| (if (r.path.len > 0) Py.s(lstatKind(r.path)) else .null) else .null }) catch oom();
        described.append(a, .{ .key = "complete", .value = .{ .bool = complete } }) catch oom();
        if (root == null) described.append(a, .{ .key = "error", .value = Py.s("module file has no root assignment") }) catch oom();
    }
    const out = json.dumps(a, .{ .dict = described.items }, 2, true) catch return 1;
    cli.put(1, out);
    cli.put(1, "\n");
    return if (std.mem.eql(u8, kind, "file") and complete) 0 else 1;
}

// ---- status ------------------------------------------------------------

const schema = 2;
const ledger_schema = 4;

fn needsIntervention(comptime f: []const u8, args: anytype) noreturn {
    refuse(f, args);
}

/// load_journal with its schema checks.
pub fn loadJournal(a: Allocator, path: []const u8) Py {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| needsIntervention("journal cannot be read: {s}: {s}", .{ path, osError(a, err, path) });
    defer sys.close(f.fd);
    const data = fsx.readAll(a, f.fd, 256 << 20) catch |err| needsIntervention("journal cannot be read: {s}: {s}", .{ path, osError(a, err, path) });
    if (!std.unicode.utf8ValidateSlice(data)) needsIntervention("journal cannot be read: {s}: not UTF-8 text", .{path});
    const parsed = json.parse(a, data) catch needsIntervention("journal cannot be read: {s}: not valid JSON", .{path});
    const j = json.toPy(a, parsed) catch needsIntervention("journal cannot be read: {s}: not valid JSON", .{path});
    if (j != .dict) needsIntervention("journal is not a JSON object: {s}", .{path});
    const sch = pyfmt.get(j, "schema");
    const sch_int: ?i64 = if (sch) |s| switch (s) {
        .int => |n| n,
        .bool => |b| @intFromBool(b),
        .float => |x| if (x == @floor(x) and @abs(x) < 1e15) @intFromFloat(x) else null,
        else => null,
    } else null;
    if (sch_int == 1 or sch_int == 3) {
        needsIntervention("journal schema {d} is a generation-pointer record{s}; this program publishes by directory swap and will not apply it", .{ sch_int.?, if (sch_int == 3) " and carries that line's ledger policy" else "" });
    }
    var shown: std.ArrayList(u8) = .empty;
    pyfmt.repr(&shown, a, sch orelse .null) catch oom();
    if (sch_int == ledger_schema) {
        const publ = pyfmt.get(j, "publication");
        const is_swap = publ != null and publ.? == .str and std.mem.eql(u8, publ.?.str, "directory-swap");
        const lp = pyfmt.get(j, "ledger_policy");
        if (!is_swap or lp == null or lp.? != .dict) needsIntervention("schema 4 journal is not a frozen directory-swap ledger record", .{});
    } else if (sch_int != schema) {
        needsIntervention("journal schema {s} is not {d} or {d}", .{ shown.items, schema, ledger_schema });
    }
    requireDirectorySwap(a, j);
    return j;
}

/// _require_directory_swap: a journal records a directory swap: its
/// publication, when set, is directory-swap, and every module it plans has
/// a live and a retired tree. A retire journal passes.
pub fn requireDirectorySwap(a: Allocator, j: Py) void {
    if (pyfmt.get(j, "publication")) |p| {
        const ok = p == .null or (p == .str and std.mem.eql(u8, p.str, "directory-swap"));
        if (!ok) {
            var shown: std.ArrayList(u8) = .empty;
            pyfmt.repr(&shown, a, p) catch oom();
            needsIntervention("journal publication {s} is not a directory swap", .{shown.items});
        }
    }
    if (pyfmt.get(j, "kind")) |k| if (k == .str and std.mem.eql(u8, k.str, "retire")) return;
    const plan = pyfmt.get(j, "modules_plan");
    if (!pyfmt.truthy(plan)) return;
    const items = switch (plan.?) {
        .list => |l| l,
        else => needsIntervention("journal modules_plan is not a list", .{}),
    };
    for (items) |m| {
        if (m != .dict) needsIntervention("journal modules_plan entry is not an object", .{});
        if (pyfmt.get(m, "live_software") == null or pyfmt.get(m, "retired") == null)
            needsIntervention("journal does not record a directory swap; a generation-pointer plan is not applied", .{});
    }
}

fn typeError(msg: []const u8) noreturn {
    cli.err("Traceback: TypeError: {s}\n", .{msg});
    std.process.exit(1);
}

fn lessName(_: void, x: []const u8, y: []const u8) bool {
    return json.pyTextLess(x, y);
}

/// list_journals: the journals under the state root's runs directory, in
/// name order; an entry without a regular journal.json is left out.
pub fn listJournals(a: Allocator, state_root: []const u8) []const Py {
    return journalsUnder(a, state_root, false);
}

/// run_journals: the journals listJournals returns, each checked as
/// admin.loadRunJournal checks one. A command that chooses among the runs
/// refuses them all if one names another run or state root or lacks a
/// field, before it chooses.
pub fn runJournals(a: Allocator, state_root: []const u8) []const Py {
    return journalsUnder(a, state_root, true);
}

fn journalsUnder(a: Allocator, state_root: []const u8, checked: bool) []const Py {
    var found: std.ArrayList(Py) = .empty;
    const runs = std.fs.path.join(a, &.{ state_root, "runs" }) catch oom();
    const runs_st = fsx.statAt(fsx.cwd, runs, true) catch return &.{};
    if (!runs_st.isDir()) return &.{};
    const fd = fsx.openDir(fsx.cwd, runs, true) catch |err| refuse("{s}", .{osError(a, err, runs)});
    defer sys.close(fd);
    var names: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(a, fd) catch oom();
    while (it.next() catch |err| refuse("{s}", .{osError(a, err, runs)})) |e| names.append(a, a.dupe(u8, e.name) catch continue) catch oom();
    std.mem.sort([]const u8, names.items, {}, lessName);
    for (names.items) |n| {
        const p = std.fs.path.join(a, &.{ runs, n, "journal.json" }) catch continue;
        const st = fsx.statAt(fsx.cwd, p, false) catch continue;
        if (!st.isReg()) continue;
        const j = loadJournal(a, p);
        if (checked) admin.checkRunJournal(a, state_root, n, j);
        found.append(a, j) catch oom();
    }
    return found.items;
}

fn line(a: Allocator, buf: *std.ArrayList(u8)) void {
    buf.append(a, '\n') catch oom();
    cli.put(1, buf.items);
    buf.clearRetainingCapacity();
}

fn fieldOrNull(v: Py, k: []const u8) Py {
    return pyfmt.get(v, k) orelse .null;
}

fn statusModule(a: Allocator, m: Py) Py {
    if (m != .dict) typeError("a modules_plan entry is not an object");
    var row: std.ArrayList(Py.Entry) = .empty;
    inline for ([_][]const u8{ "live_software", "rel", "retired", "status" }) |k| {
        row.append(a, .{ .key = k, .value = fieldOrNull(m, k) }) catch oom();
    }
    return .{ .dict = row.items };
}

/// The fields status prints, as the JSON document: one object per journal.
/// A missing kind is "rebuild". An empty list is a document, not "no runs".
fn statusDocument(a: Allocator, journals: []const Py) Py {
    var runs: std.ArrayList(Py) = .empty;
    for (journals) |j| {
        const kind = pyfmt.get(j, "kind");
        if (kind != null and kind.? == .str and std.mem.eql(u8, kind.?.str, "retire")) {
            var row: std.ArrayList(Py.Entry) = .empty;
            row.append(a, .{ .key = "kind", .value = Py.s("retire") }) catch oom();
            row.append(a, .{ .key = "module_path", .value = fieldOrNull(j, "module_path") }) catch oom();
            row.append(a, .{ .key = "run_id", .value = fieldOrNull(j, "run_id") }) catch oom();
            row.append(a, .{ .key = "software_dir", .value = fieldOrNull(j, "software_dir") }) catch oom();
            row.append(a, .{ .key = "state", .value = fieldOrNull(j, "state") }) catch oom();
            runs.append(a, .{ .dict = row.items }) catch oom();
            continue;
        }
        const plan = pyfmt.get(j, "modules_plan");
        const mods: []const Py = if (pyfmt.truthy(plan)) switch (plan.?) {
            .list => |l| l,
            else => typeError("modules_plan is not a list"),
        } else &.{};
        var rows: std.ArrayList(Py) = .empty;
        var published: i64 = 0;
        for (mods) |m| {
            if (pyfmt.get(m, "status")) |s| {
                if (s == .str and std.mem.eql(u8, s.str, "published")) published += 1;
            }
            rows.append(a, statusModule(a, m)) catch oom();
        }
        var row: std.ArrayList(Py.Entry) = .empty;
        row.append(a, .{ .key = "backend", .value = fieldOrNull(j, "backend") }) catch oom();
        row.append(a, .{ .key = "kind", .value = if (pyfmt.truthy(kind)) kind.? else Py.s("rebuild") }) catch oom();
        row.append(a, .{ .key = "message", .value = fieldOrNull(j, "message") }) catch oom();
        row.append(a, .{ .key = "modules", .value = .{ .list = rows.items } }) catch oom();
        row.append(a, .{ .key = "modules_published", .value = .{ .int = published } }) catch oom();
        row.append(a, .{ .key = "modules_total", .value = .{ .int = @intCast(mods.len) } }) catch oom();
        row.append(a, .{ .key = "run_id", .value = fieldOrNull(j, "run_id") }) catch oom();
        row.append(a, .{ .key = "state", .value = fieldOrNull(j, "state") }) catch oom();
        runs.append(a, .{ .dict = row.items }) catch oom();
    }
    var top: std.ArrayList(Py.Entry) = .empty;
    top.append(a, .{ .key = "runs", .value = .{ .list = runs.items } }) catch oom();
    return .{ .dict = top.items };
}

fn cmdStatus(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, status_usage, status_help);
    const state_root = sub.value("state-root") orelse sr.default.?;
    var journals = listJournals(a, state_root);
    if (sub.positional(0)) |want| {
        var keep: std.ArrayList(Py) = .empty;
        for (journals) |j| if (pyfmt.get(j, "run_id")) |r| if (r == .str and std.mem.eql(u8, r.str, want)) keep.append(a, j) catch oom();
        journals = keep.items;
        if (journals.len == 0) refuse("no journal for {s}", .{want});
    }
    if (sub.seen("json")) {
        const out = json.dumps(a, statusDocument(a, journals), 2, true) catch return 1;
        cli.put(1, out);
        cli.put(1, "\n");
        return 0;
    }
    if (journals.len == 0) {
        cli.put(1, "no runs\n");
        return 0;
    }
    var b: std.ArrayList(u8) = .empty;
    for (journals) |j| {
        const kind = pyfmt.get(j, "kind");
        if (kind != null and kind.? == .str and std.mem.eql(u8, kind.?.str, "retire")) {
            pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch oom();
            b.appendSlice(a, "  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "state")) catch oom();
            b.appendSlice(a, "  retire  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "module_path")) catch oom();
            line(a, &b);
            b.appendSlice(a, "  software directory ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "software_dir")) catch oom();
            line(a, &b);
            continue;
        }
        const plan = pyfmt.get(j, "modules_plan");
        const mods: []const Py = if (pyfmt.truthy(plan)) switch (plan.?) {
            .list => |l| l,
            else => typeError("modules_plan is not a list"),
        } else &.{};
        var published: usize = 0;
        for (mods) |m| {
            if (m != .dict) typeError("a modules_plan entry is not an object");
            if (pyfmt.get(m, "status")) |s| if (s == .str and std.mem.eql(u8, s.str, "published")) {
                published += 1;
            };
        }
        pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch oom();
        b.appendSlice(a, "  ") catch oom();
        pyfmt.str(&b, a, pyfmt.get(j, "state")) catch oom();
        b.appendSlice(a, "  backend=") catch oom();
        pyfmt.str(&b, a, pyfmt.get(j, "backend")) catch oom();
        b.print(a, "  modules={d}/{d}", .{ published, mods.len }) catch oom();
        line(a, &b);
        if (pyfmt.truthy(pyfmt.get(j, "message"))) {
            b.appendSlice(a, "  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "message")) catch oom();
            line(a, &b);
        }
        for (mods) |m| {
            b.appendSlice(a, "  ") catch oom();
            pyfmt.leftPad(&b, a, pyfmt.get(m, "status"), 10) catch typeError("unsupported format string passed to the status value's __format__");
            b.append(a, ' ') catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "rel")) catch oom();
            b.appendSlice(a, "  live=") catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "live_software")) catch oom();
            b.appendSlice(a, " retired=") catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "retired")) catch oom();
            line(a, &b);
        }
    }
    return 0;
}

const retire_usage =
    \\usage: eb-staged-rebuild retire [-h] --state-root STATE_ROOT
    \\                                --module-file MODULE_FILE [--reason REASON]
    \\                                [--dry-run] [--usage-log USAGE_LOG]
    \\                                [--eb-keep-logs EB_KEEP_LOGS]
    \\                                [--lock-timeout LOCK_TIMEOUT]
    \\
;
const retire_help = retire_usage ++
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --module-file MODULE_FILE
    \\                        module file to retire
    \\  --reason REASON
    \\  --dry-run
    \\  --usage-log USAGE_LOG
    \\                        usage log checked with eb-keep-logs used --require-
    \\                        none
    \\  --eb-keep-logs EB_KEEP_LOGS
    \\                        eb-keep-logs executable
    \\  --lock-timeout LOCK_TIMEOUT
    \\                        seconds to wait for the lock (default: refuse at once)
    \\
;
const rollback_usage =
    \\usage: eb-staged-rebuild rollback [-h] --state-root STATE_ROOT
    \\                                  [--lock-timeout LOCK_TIMEOUT] [--to GEN]
    \\                                  [--latest]
    \\                                  run_id
    \\
;
const rollback_help = rollback_usage ++
    \\
    \\positional arguments:
    \\  run_id                the run to roll back, unless --latest or --to is given
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --lock-timeout LOCK_TIMEOUT
    \\  --to GEN              roll back every run published over run GEN
    \\  --latest              roll back the newest complete run
    \\
;
const gc_usage =
    \\usage: eb-staged-rebuild gc [-h] --state-root STATE_ROOT
    \\                            [--grace-hours GRACE_HOURS] [--dry-run]
    \\                            [--lock-timeout LOCK_TIMEOUT]
    \\                            [--usage-log USAGE_LOG]
    \\                            [--eb-keep-logs EB_KEEP_LOGS]
    \\                            [--metrics-file PATH] [--include-failed] [--json]
    \\                            [--keep N] [--check-in-use]
    \\
;
const gc_help = gc_usage ++
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --grace-hours GRACE_HOURS
    \\  --dry-run
    \\  --lock-timeout LOCK_TIMEOUT
    \\  --usage-log USAGE_LOG
    \\                        usage log checked with eb-keep-logs used --require-
    \\                        none
    \\  --eb-keep-logs EB_KEEP_LOGS
    \\                        eb-keep-logs executable
    \\  --metrics-file PATH   atomically write Prometheus metrics after garbage
    \\                        collection
    \\  --include-failed      also remove the hidden trees and generation of a
    \\                        failed run that published nothing, after the grace
    \\                        period
    \\  --json                write collection actions as JSON
    \\  --keep N              keep each install path's N newest generations that
    \\                        rollback can switch back to
    \\  --check-in-use        keep a tree or generation that a process or a mount on
    \\                        this node uses
    \\
;

/// The shared lock-timeout check: a finite float, or the reason it is not.
fn parseLockTimeout(v: []const u8) error{ InvalidFloat, NotFinite }!f64 {
    const t = std.mem.trim(u8, v, " \t\n\r\x0b\x0c");
    const value = std.fmt.parseFloat(f64, t) catch return error.InvalidFloat;
    if (!std.math.isFinite(value)) return error.NotFinite;
    return value;
}

fn parseBytes(v: []const u8) error{ NotAnInteger, BelowZero }!u128 {
    const t = std.mem.trim(u8, v, " \t\n\r\x0b\x0c");
    if (t.len > 0 and t[0] == '-') {
        // "-0" is zero, as Python's int() reads it. Any other negative
        // integer is below zero, however many digits it has.
        const digits = t[1..];
        if (digits.len == 0 or digits[0] == '_' or digits[digits.len - 1] == '_') return error.NotAnInteger;
        var nonzero = false;
        for (digits) |ch| {
            if (ch == '_') continue;
            if (!std.ascii.isDigit(ch)) return error.NotAnInteger;
            if (ch != '0') nonzero = true;
        }
        return if (nonzero) error.BelowZero else 0;
    }
    return std.fmt.parseInt(u128, t, 10) catch error.NotAnInteger;
}

fn repr(a: Allocator, s: []const u8) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    common.pytext.reprStr(&out, a, s) catch oom();
    return out.items;
}

fn isHours(hours: f64) bool {
    return std.math.isFinite(hours) and hours >= 0;
}

fn parsedFloat(v: []const u8) f64 {
    return std.fmt.parseFloat(f64, std.mem.trim(u8, v, " \t\n\r\x0b\x0c")) catch unreachable;
}

/// EB_STAGED_REBUILD_GRACE_HOURS, or the default when it is not set.
fn envGrace(a: Allocator, env: probe.Env) f64 {
    const e = env.get("EB_STAGED_REBUILD_GRACE_HOURS") orelse return admin.default_grace_hours;
    const hours = std.fmt.parseFloat(f64, std.mem.trim(u8, e, " \t\n\r\x0b\x0c")) catch refuse("EB_STAGED_REBUILD_GRACE_HOURS is not a number: {s}", .{repr(a, e)});
    if (!isHours(hours)) refuse("EB_STAGED_REBUILD_GRACE_HOURS is not a number of hours, 0 or more: {s}", .{repr(a, e)});
    return hours;
}

/// EB_STAGED_REBUILD_MIN_FREE_BYTES supplies run's default free-space policy.
fn envMinFreeBytes(a: Allocator, env: probe.Env) ?u64 {
    const e = env.get("EB_STAGED_REBUILD_MIN_FREE_BYTES") orelse return null;
    const t = std.mem.trim(u8, e, " \t\n\r\x0b\x0c");
    const minimum = std.fmt.parseInt(i128, t, 10) catch
        refuse("EB_STAGED_REBUILD_MIN_FREE_BYTES is not an integer: {s}", .{repr(a, e)});
    if (minimum < 0 or minimum > std.math.maxInt(u64))
        refuse(
            "EB_STAGED_REBUILD_MIN_FREE_BYTES is not a number of bytes, 0 through {d}: {s}",
            .{ std.math.maxInt(u64), repr(a, e) },
        );
    return @intCast(minimum);
}

test "envMinFreeBytes trims ASCII whitespace like Python int()" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const entries = [_:null]?[*:0]const u8{"EB_STAGED_REBUILD_MIN_FREE_BYTES= \t4096\n"};
    try std.testing.expectEqual(@as(?u64, 4096), envMinFreeBytes(arena.allocator(), .{ .envp = &entries }));
}

/// --keep: ASCII digits only, saturating at the largest u64.
fn parseCount(v: []const u8) ?admin.Keep {
    if (v.len == 0) return null;
    var value: u64 = 0;
    for (v) |ch| {
        if (!std.ascii.isDigit(ch)) return null;
        value = std.math.mul(u64, value, 10) catch std.math.maxInt(u64);
        value = std.math.add(u64, value, ch - '0') catch std.math.maxInt(u64);
    }
    const trimmed = std.mem.trimStart(u8, v, "0");
    return .{ .value = value, .text = if (trimmed.len == 0) "0" else trimmed };
}

fn validateCount(gpa: Allocator, raw: []const u8) Allocator.Error!argparse.ValidationResult {
    if (parseCount(raw) != null) return .ok;
    const q = try quoted(gpa, raw);
    defer gpa.free(q);
    return .{ .err = try std.fmt.allocPrint(gpa, "invalid count value: {s}", .{q}) };
}

/// argparse's required-arguments error for a check that depends on more
/// than one argument: the usage, then the message, status 2.
fn requiredError(usage: []const u8, who: []const u8, names: []const []const u8, a: Allocator) noreturn {
    cli.put(2, usage);
    cli.err("{s}: error: the following arguments are required: {s}\n", .{ who, std.mem.join(a, ", ", names) catch "" });
    std.process.exit(2);
}

fn adminCtx(a: Allocator, env: probe.Env) admin.Ctx {
    return .{ .a = a, .env = env, .pause = .{ .env = env } };
}

fn stateRootOf(sub: *const argparse.Parsed, sr: StateRoot) []const u8 {
    return sub.value("state-root") orelse sr.default.?;
}

fn cmdRetire(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, retire_usage, retire_help);
    const timeout: f64 = if (sub.value("lock-timeout")) |v| parseLockTimeout(v) catch unreachable else 0;
    var ctx = adminCtx(a, env);
    return admin.retire(
        &ctx,
        stateRootOf(sub, sr),
        sub.value("module-file").?,
        sub.value("reason"),
        sub.seen("dry-run"),
        timeout,
        sub.value("usage-log"),
        sub.value("eb-keep-logs") orelse "eb-keep-logs",
    );
}

fn cmdRollback(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, rollback_usage, rollback_help);
    // --latest and --to name the runs instead of the run id.
    const run_id = sub.positional(0);
    const to = sub.value("to");
    const latest = sub.seen("latest");
    var need: std.ArrayList([]const u8) = .empty;
    if (sub.value("state-root") == null and sr.default == null) need.append(a, "--state-root") catch oom();
    if (run_id == null and to == null and !latest) need.append(a, "run_id") catch oom();
    if (need.items.len > 0) requiredError(sr.usage, prog ++ " rollback", need.items, a);
    const timeout: ?f64 = if (sub.value("lock-timeout")) |v| parseLockTimeout(v) catch unreachable else null;
    var ctx = adminCtx(a, env);
    return admin.rollback(&ctx, stateRootOf(sub, sr), run_id, to, latest, timeout);
}

fn cmdGc(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, gc_usage, gc_help);
    const grace_cli: ?f64 = if (sub.value("grace-hours")) |v| parsedFloat(v) else null;
    const timeout: ?f64 = if (sub.value("lock-timeout")) |v| parseLockTimeout(v) catch unreachable else null;
    const keep: ?admin.Keep = if (sub.value("keep")) |v| parseCount(v).? else null;
    const grace = grace_cli orelse envGrace(a, env);
    var ctx = adminCtx(a, env);
    return admin.gc(
        &ctx,
        stateRootOf(sub, sr),
        grace,
        sub.seen("dry-run"),
        sub.seen("include-failed"),
        timeout,
        sub.value("grace-hours") == null,
        sub.value("usage-log"),
        sub.value("eb-keep-logs") orelse "eb-keep-logs",
        sub.value("metrics-file"),
        sub.seen("json"),
        keep,
        sub.seen("check-in-use"),
    );
}

const run_usage = @embedFile("run_usage.txt");
const run_help = @embedFile("run_help.txt");
const backends = [_][]const u8{ "auto", "bwrap", "userns", "installpath" };
/// run's easyconfig words are argparse REMAINDER: the first non-option, or
/// "--", and everything after it.
fn cmdRun(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const grace_cli: ?f64 = if (sub.value("grace-hours")) |v| parsedFloat(v) else null;
    const lock_timeout: ?f64 = if (sub.value("lock-timeout")) |v| parseLockTimeout(v) catch unreachable else null;
    const min_free_cli: ?u128 = if (sub.value("min-free-bytes")) |v| parseBytes(v) catch unreachable else null;

    const software = nonEmpty(sub.value("software")) orelse nonEmpty(env.get("EASYBUILD_INSTALLPATH_SOFTWARE"));
    const modules = nonEmpty(sub.value("modules")) orelse nonEmpty(env.get("EASYBUILD_INSTALLPATH_MODULES"));
    if (software == null or modules == null)
        refuse("--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required", .{});
    const state_root = nonEmpty(sub.value("state-root")) orelse nonEmpty(env.get("EB_STAGED_REBUILD_STATE_ROOT")) orelse
        (std.fs.path.join(a, &.{ std.fs.path.dirname(admin.abspath(a, software.?)) orelse "/", ".eb-staged-rebuild" }) catch "");
    var eb_args = sub.remainder;
    if (eb_args.len > 0 and std.mem.eql(u8, eb_args[0], "--")) eb_args = eb_args[1..];
    const has_positional = for (eb_args) |x| {
        if (x.len == 0 or x[0] != '-') break true;
    } else false;
    if (!has_positional) refuse("pass the easyconfig after --, for example: eb-staged-rebuild run --software S --modules M -- file.eb", .{});
    const grace = grace_cli orelse envGrace(a, env);
    const min_free_bytes: ?u128 = if (min_free_cli) |v| v else if (envMinFreeBytes(a, env)) |v| v else null;
    if (sub.value("prepare")) |p| if (p.len == 0) refuse("--prepare must name a program; it is empty", .{});
    if (sub.value("build-dir")) |bd| if (bd.len == 0) refuse("--build-dir must name a directory; it is empty", .{});
    if (sub.seen("force") and !sub.seen("verify")) refuse("--force needs --verify; without it, nothing checks the stage before the swap", .{});
    return engine.run(a, env, .{
        .software = software.?,
        .modules = modules.?,
        .state_root = state_root,
        .eb_args = eb_args,
        .backend = sub.value("backend") orelse "auto",
        .grace_hours = grace,
        .dry_run = sub.seen("dry-run"),
        .module_rels = sub.values("module"),
        .eb_executable = sub.value("eb") orelse (env.get("EB_STAGED_REBUILD_EB") orelse "eb"),
        .run_id = sub.value("run-id"),
        .skip_live_check = sub.seen("skip-live-check"),
        .lock_timeout = lock_timeout,
        .prepare_executable = sub.value("prepare"),
        .ledger_config = sub.value("ledger-config"),
        .build_dir = sub.value("build-dir"),
        .min_free_bytes = min_free_bytes,
        .require_exchange = sub.seen("require-exchange"),
        .verify = sub.seen("verify"),
        .force = sub.seen("force"),
    });
}

fn nonEmpty(v: ?[]const u8) ?[]const u8 {
    const x = v orelse return null;
    return if (x.len > 0) x else null;
}

/// --state-root of resume, rollback, gc, retire and status: its default,
/// EB_STAGED_REBUILD_STATE_ROOT when set and not empty, and the command's
/// usage and help, which show the option in brackets once it has one.
const StateRoot = struct {
    default: ?[]const u8,
    usage: []const u8,
    help: []const u8,

    fn of(env: probe.Env, comptime usage: []const u8, comptime help: []const u8) StateRoot {
        const default = nonEmpty(env.get("EB_STAGED_REBUILD_STATE_ROOT")) orelse
            return .{ .default = null, .usage = usage, .help = help };
        // status --json makes the default usage longer than argparse's width,
        // so the wrapped form is written out rather than derived by bracketing.
        if (comptime std.mem.eql(u8, usage, status_usage)) {
            return .{ .default = default, .usage = status_usage_default, .help = status_help_default };
        }
        return .{ .default = default, .usage = bracketed(usage), .help = bracketed(help) };
    }

    /// The text with its required --state-root in brackets, the lines
    /// wrapped as before, as argparse prints them for these commands.
    fn bracketed(comptime text: []const u8) []const u8 {
        return comptime blk: {
            const required = "] --state-root STATE_ROOT";
            const at = std.mem.find(u8, text, required) orelse @compileError("no required --state-root in: " ++ text);
            break :blk text[0 .. at + 2] ++ "[--state-root STATE_ROOT]" ++ text[at + required.len ..];
        };
    }
};

const resume_usage =
    \\usage: eb-staged-rebuild resume [-h] --state-root STATE_ROOT
    \\                                [--lock-timeout LOCK_TIMEOUT] [--verify]
    \\                                [--force]
    \\                                run_id
    \\
;
const resume_help = resume_usage ++
    \\
    \\positional arguments:
    \\  run_id
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --lock-timeout LOCK_TIMEOUT
    \\  --verify              check the stage before the swap, as run --verify does
    \\  --force               with --verify, publish a stage that fails; the journal
    \\                        records it
    \\
;

fn cmdResume(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, resume_usage, resume_help);
    const timeout: ?f64 = if (sub.value("lock-timeout")) |v| parseLockTimeout(v) catch unreachable else null;
    var ctx = adminCtx(a, env);
    const run_mod = @import("run.zig");
    const res = run_mod.resumeRun(a, env, &ctx, stateRootOf(sub, sr), sub.positional(0).?, timeout, sub.seen("verify"), sub.seen("force")) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{last_why}),
    };
    return res.code;
}

const verify_usage =
    \\usage: eb-staged-rebuild verify [-h] --state-root STATE_ROOT
    \\                                [--lock-timeout LOCK_TIMEOUT]
    \\                                STAGE
    \\
;
const verify_help = verify_usage ++
    \\
    \\positional arguments:
    \\  STAGE                 the run id that names the stage
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --lock-timeout LOCK_TIMEOUT
    \\
;

fn cmdVerify(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, verify_usage, verify_help);
    const timeout: ?f64 = if (sub.value("lock-timeout")) |x| parseLockTimeout(x) catch unreachable else null;
    var ctx = adminCtx(a, env);
    return admin.verify(&ctx, stateRootOf(sub, sr), sub.positional(0).?, timeout);
}

fn cmdList(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const sr = StateRoot.of(env, list_usage, list_help);
    cli.put(1, generations.print(a, generations.collect(a, listJournals(a, stateRootOf(sub, sr)))));
    return 0;
}

// ---- probe -------------------------------------------------------------

fn cmdProbe(a: Allocator, sub: *const argparse.Parsed, env: probe.Env) u8 {
    const eb = sub.value("eb") orelse (env.get("EB_STAGED_REBUILD_EB") orelse "eb");
    const directory = blk: {
        if (sub.value("path")) |path| if (path.len > 0) break :blk path;
        var rb: [sys.path_max]u8 = undefined;
        break :blk a.dupe(u8, fsx.realpath(fsx.cwd, ".", &rb) catch ".") catch ".";
    };
    if (sub.seen("no-create")) {
        _ = fsx.statAt(fsx.cwd, directory, true) catch {
            cli.err("probe path does not exist: {s}\n", .{directory});
            return 1;
        };
    }
    const report = probe.probeAll(a, env, directory, eb) catch |err| refuse("{s}", .{osError(a, err, directory)});
    if (sub.seen("json")) {
        const out = json.dumps(a, report, 2, true) catch return 1;
        cli.put(1, out);
        cli.put(1, "\n");
    } else {
        probe.printText(a, report);
    }
    if (sub.seen("require-eb")) {
        const easybuild = pyfmt.get(report, "easybuild");
        const found = if (easybuild) |eb_report| pyfmt.truthy(pyfmt.get(eb_report, "found")) else false;
        if (!found) {
            cli.err("probe requires EasyBuild, but it was not found: {s}\n", .{eb});
            return 1;
        }
    }
    return 0;
}

fn exitParse(a: Allocator, outcome: *argparse.ParseOutcome) noreturn {
    switch (outcome.*) {
        .action_done => |done| {
            cli.put(1, done.stdout_text);
            std.process.exit(0);
        },
        .usage_error => |*uerr| {
            const txt = uerr.formatStderr(a) catch oom();
            cli.put(2, txt);
            std.process.exit(2);
        },
        .ok => unreachable,
    }
}

fn runCli(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    const status_sr = StateRoot.of(env, status_usage, status_help);
    const resume_sr = StateRoot.of(env, resume_usage, resume_help);
    const rollback_sr = StateRoot.of(env, rollback_usage, rollback_help);
    const gc_sr = StateRoot.of(env, gc_usage, gc_help);
    const retire_sr = StateRoot.of(env, retire_usage, retire_help);
    const verify_sr = StateRoot.of(env, verify_usage, verify_help);
    const list_sr = StateRoot.of(env, list_usage, list_help);

    const probe_opts = [_]argparse.OptionSpec{
        .{ .name = "path", .kind = .value },
        .{ .name = "no-create", .kind = .flag },
        .{ .name = "eb", .kind = .value },
        .{ .name = "require-eb", .kind = .flag },
        .{ .name = "json", .kind = .flag },
    };
    const probe_spec = argparse.CommandSpec{
        .prog = prog ++ " probe",
        .usage = probe_usage,
        .help = probe_help,
        .options = &probe_opts,
    };

    const run_pos = [_]argparse.PositionalSpec{.{ .name = "eb_args", .arity = .remainder }};
    const run_opts = [_]argparse.OptionSpec{
        .{ .name = "software", .kind = .value },
        .{ .name = "modules", .kind = .value },
        .{ .name = "state-root", .kind = .value },
        .{ .name = "eb", .kind = .value },
        backend_opt,
        grace_opt,
        .{ .name = "dry-run", .kind = .flag },
        module_opt,
        .{ .name = "run-id", .kind = .value },
        lock_opt,
        .{ .name = "skip-live-check", .kind = .flag },
        .{ .name = "prepare", .kind = .value },
        .{ .name = "ledger-config", .kind = .value },
        .{ .name = "build-dir", .kind = .value },
        bytes_opt,
        .{ .name = "require-exchange", .kind = .flag },
        .{ .name = "verify", .kind = .flag },
        .{ .name = "force", .kind = .flag },
    };
    const run_spec = argparse.CommandSpec{
        .prog = prog ++ " run",
        .usage = run_usage,
        .help = run_help,
        .options = &run_opts,
        .positionals = &run_pos,
    };

    const resume_opts = [_]argparse.OptionSpec{
        stateOpt(resume_sr.default != null),
        lock_opt,
        .{ .name = "verify", .kind = .flag },
        .{ .name = "force", .kind = .flag },
    };
    const resume_pos = [_]argparse.PositionalSpec{.{ .name = "run_id" }};
    const resume_spec = argparse.CommandSpec{
        .prog = prog ++ " resume",
        .usage = resume_sr.usage,
        .help = resume_sr.help,
        .options = &resume_opts,
        .positionals = &resume_pos,
    };

    // --latest and --to stand in for run_id, so cmdRollback checks the
    // required arguments itself, in argparse's order: --state-root, run_id.
    const rollback_opts = [_]argparse.OptionSpec{
        .{ .name = "state-root", .kind = .value, .has_default = true },
        lock_opt,
        .{ .name = "to", .kind = .value },
        .{ .name = "latest", .kind = .flag },
    };
    const rollback_pos = [_]argparse.PositionalSpec{.{ .name = "run_id", .arity = .optional_one }};
    const rollback_spec = argparse.CommandSpec{
        .prog = prog ++ " rollback",
        .usage = rollback_sr.usage,
        .help = rollback_sr.help,
        .options = &rollback_opts,
        .positionals = &rollback_pos,
    };

    const gc_opts = [_]argparse.OptionSpec{
        stateOpt(gc_sr.default != null),
        grace_opt,
        .{ .name = "dry-run", .kind = .flag },
        lock_opt,
        .{ .name = "usage-log", .kind = .value },
        .{ .name = "eb-keep-logs", .kind = .value },
        .{ .name = "metrics-file", .kind = .value },
        .{ .name = "include-failed", .kind = .flag },
        .{ .name = "json", .kind = .flag },
        .{ .name = "keep", .kind = .value, .validate = validateCount },
        .{ .name = "check-in-use", .kind = .flag },
    };
    const gc_spec = argparse.CommandSpec{
        .prog = prog ++ " gc",
        .usage = gc_sr.usage,
        .help = gc_sr.help,
        .options = &gc_opts,
    };

    const retire_opts = [_]argparse.OptionSpec{
        stateOpt(retire_sr.default != null),
        .{ .name = "module-file", .kind = .value, .required = true },
        .{ .name = "reason", .kind = .value },
        .{ .name = "dry-run", .kind = .flag },
        .{ .name = "usage-log", .kind = .value },
        .{ .name = "eb-keep-logs", .kind = .value },
        lock_opt,
    };
    const retire_spec = argparse.CommandSpec{
        .prog = prog ++ " retire",
        .usage = retire_sr.usage,
        .help = retire_sr.help,
        .options = &retire_opts,
    };

    const status_opts = [_]argparse.OptionSpec{
        stateOpt(status_sr.default != null),
        .{ .name = "json", .kind = .flag },
    };
    const status_pos = [_]argparse.PositionalSpec{.{ .name = "run_id", .arity = .optional_one }};
    const status_spec = argparse.CommandSpec{
        .prog = prog ++ " status",
        .usage = status_sr.usage,
        .help = status_sr.help,
        .options = &status_opts,
        .positionals = &status_pos,
    };

    const verify_opts = [_]argparse.OptionSpec{ stateOpt(verify_sr.default != null), lock_opt };
    const verify_pos = [_]argparse.PositionalSpec{.{ .name = "STAGE" }};
    const verify_spec = argparse.CommandSpec{
        .prog = prog ++ " verify",
        .usage = verify_sr.usage,
        .help = verify_sr.help,
        .options = &verify_opts,
        .positionals = &verify_pos,
    };

    const list_opts = [_]argparse.OptionSpec{stateOpt(list_sr.default != null)};
    const list_spec = argparse.CommandSpec{
        .prog = prog ++ " list",
        .usage = list_sr.usage,
        .help = list_sr.help,
        .options = &list_opts,
    };

    const resolve_pos = [_]argparse.PositionalSpec{.{ .name = "module" }};
    const resolve_spec = argparse.CommandSpec{
        .prog = prog ++ " resolve",
        .usage = resolve_usage,
        .help = resolve_help,
        .positionals = &resolve_pos,
    };

    const subs = [_]argparse.SubcommandSpec{
        .{ .name = "probe", .command = &probe_spec },
        .{ .name = "run", .command = &run_spec },
        .{ .name = "resume", .command = &resume_spec },
        .{ .name = "verify", .command = &verify_spec },
        .{ .name = "rollback", .command = &rollback_spec },
        .{ .name = "gc", .command = &gc_spec },
        .{ .name = "retire", .command = &retire_spec },
        .{ .name = "status", .command = &status_spec },
        .{ .name = "list", .command = &list_spec },
        .{ .name = "resolve", .command = &resolve_spec },
    };
    const top = argparse.CommandSpec{
        .prog = prog,
        .usage = top_usage,
        .help = top_help,
        .version = version_text,
        .subcommands = &subs,
        .subcommand_dest = "command",
    };

    var outcome = argparse.parse(a, &top, args) catch oom();
    switch (outcome) {
        .action_done, .usage_error => exitParse(a, &outcome),
        .ok => |*parsed| {
            defer parsed.deinit(a);
            const name = parsed.subcommand_name.?;
            const sub = parsed.subcommand().?;
            if (std.mem.eql(u8, name, "resolve")) return cmdResolve(a, sub);
            if (std.mem.eql(u8, name, "status")) return cmdStatus(a, sub, env);
            if (std.mem.eql(u8, name, "probe")) return cmdProbe(a, sub, env);
            if (std.mem.eql(u8, name, "retire")) return cmdRetire(a, sub, env);
            if (std.mem.eql(u8, name, "rollback")) return cmdRollback(a, sub, env);
            if (std.mem.eql(u8, name, "gc")) return cmdGc(a, sub, env);
            if (std.mem.eql(u8, name, "run")) return cmdRun(a, sub, env);
            if (std.mem.eql(u8, name, "resume")) return cmdResume(a, sub, env);
            if (std.mem.eql(u8, name, "verify")) return cmdVerify(a, sub, env);
            if (std.mem.eql(u8, name, "list")) return cmdList(a, sub, env);
            unreachable;
        },
    }
}

/// The entry point: run the command the arguments name, and return its
/// exit status.
pub fn main(init: std.process.Init.Minimal) u8 {
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    const a = arena_state.allocator();
    const all = init.args.toSlice(a) catch return 2;
    const args: []const []const u8 = if (all.len > 1) @ptrCast(all[1..]) else &.{};
    const env: probe.Env = .{ .envp = init.environ.block.slice.ptr };
    // Only a replaceable build reads the test clock. The release binary
    // does not import that file, so a leaked value cannot move a run id
    // or a journal timestamp.
    if (comptime common.atomic.replaceable) @import("testnow.zig").bind(a, env);
    // Only the -Dtest-ops test artifact can emulate a file system's ACLs.
    if (comptime common.atomic.replaceable) @import("aclmodel.zig").installFromEnv(env);

    // Internal: the live-tree snapshot as JSON, for the golden tests.
    if (args.len == 2 and std.mem.eql(u8, args[0], "__snapshot")) {
        const snap = @import("livecheck.zig").snapshot(a, args[1]) catch |err| refuse("{s}", .{osError(a, err, args[1])});
        cli.put(1, json.dumps(a, snap, 2, true) catch return 1);
        cli.put(1, "\n");
        return 0;
    }
    // The user-namespace sandbox re-executes this program.
    if (args.len > 0 and std.mem.eql(u8, args[0], probe.sandbox_command)) return probe.sandboxMain(a, env, args[1..]);

    return runCli(a, args, env);
}

pub var last_why: []const u8 = "";

test {
    _ = @import("run.zig");
    _ = @import("publish.zig");
    _ = @import("swap.zig");
    _ = @import("livecheck.zig");
    _ = @import("discover.zig");
    _ = @import("builders.zig");
    _ = @import("verify.zig");
    _ = @import("inuse.zig");
    _ = admin;
    _ = engine;
    _ = modulefile;
    _ = pyfmt;
    _ = probe;
}

test "optionLike takes a negative number for a value, as argparse 3.14 does" {
    for ([_][]const u8{ "--grace-hours", "--", "-h", "-x", "-.", "-.x", "--5", "-inf" }) |w|
        try std.testing.expect(argparse.optionLike(w, .py314));
    for ([_][]const u8{ "", "-", "5", "-5", "-0", "-.5", "-1e3", "-5x" }) |w|
        try std.testing.expect(!argparse.optionLike(w, .py314));
}

test "with a default, --state-root is in brackets and the usage wraps as argparse 3.14 prints it" {
    const expect = std.testing.expectEqualStrings;
    try expect(status_usage_default, status_help_default[0..status_usage_default.len]);
    try expect("usage: eb-staged-rebuild list [-h] [--state-root STATE_ROOT]\n", StateRoot.bracketed(list_usage));
    try expect(
        \\usage: eb-staged-rebuild resume [-h] [--state-root STATE_ROOT]
        \\                                [--lock-timeout LOCK_TIMEOUT] [--verify]
        \\                                [--force]
        \\                                run_id
        \\
    , StateRoot.bracketed(resume_usage));
    try expect(
        \\usage: eb-staged-rebuild verify [-h] [--state-root STATE_ROOT]
        \\                                [--lock-timeout LOCK_TIMEOUT]
        \\                                STAGE
        \\
    , StateRoot.bracketed(verify_usage));
    try expect(
        \\usage: eb-staged-rebuild rollback [-h] [--state-root STATE_ROOT]
        \\                                  [--lock-timeout LOCK_TIMEOUT] [--to GEN]
        \\                                  [--latest]
        \\                                  run_id
        \\
    , StateRoot.bracketed(rollback_usage));
    try expect(
        \\usage: eb-staged-rebuild gc [-h] [--state-root STATE_ROOT]
        \\                            [--grace-hours GRACE_HOURS] [--dry-run]
        \\                            [--lock-timeout LOCK_TIMEOUT]
        \\                            [--usage-log USAGE_LOG]
        \\                            [--eb-keep-logs EB_KEEP_LOGS]
        \\                            [--metrics-file PATH] [--include-failed] [--json]
        \\                            [--keep N] [--check-in-use]
        \\
    , StateRoot.bracketed(gc_usage));
    try expect(
        \\usage: eb-staged-rebuild retire [-h] [--state-root STATE_ROOT]
        \\                                --module-file MODULE_FILE [--reason REASON]
        \\                                [--dry-run] [--usage-log USAGE_LOG]
        \\                                [--eb-keep-logs EB_KEEP_LOGS]
        \\                                [--lock-timeout LOCK_TIMEOUT]
        \\
    , StateRoot.bracketed(retire_usage));
    try expect(comptime StateRoot.bracketed(gc_usage) ++ gc_help[gc_usage.len..], StateRoot.bracketed(gc_help));
}

test "run help includes --build-dir" {
    try std.testing.expect(std.mem.indexOf(u8, run_usage, "[--build-dir DIR]") != null);
    try std.testing.expect(std.mem.indexOf(u8, run_help, "--build-dir DIR") != null);
}

test "a lock timeout is a finite float" {
    try std.testing.expectEqual(@as(f64, 0), try parseLockTimeout("0"));
    try std.testing.expectEqual(@as(f64, 1.5), try parseLockTimeout(" 1.5 "));
    try std.testing.expectEqual(@as(f64, -1), try parseLockTimeout("-1"));
    try std.testing.expectEqual(@as(f64, 2), try parseLockTimeout("2"));
    try std.testing.expectError(error.NotFinite, parseLockTimeout("nan"));
    try std.testing.expectError(error.NotFinite, parseLockTimeout("inf"));
    try std.testing.expectError(error.NotFinite, parseLockTimeout("-inf"));
    try std.testing.expectError(error.InvalidFloat, parseLockTimeout("nope"));

    const a = std.testing.allocator;
    const opts = [_]argparse.OptionSpec{lock_opt};
    const spec = argparse.CommandSpec{
        .prog = "eb-staged-rebuild",
        .usage = "usage\n",
        .help = "",
        .options = &opts,
    };
    // `-inf` is option-like, so argparse only hands it to the validator as `--lock-timeout=-inf`.
    const cases = [_]struct { argv: []const []const u8, raw: []const u8, detail: ?[]const u8 }{
        .{ .argv = &.{ "--lock-timeout", "0" }, .raw = "0", .detail = null },
        .{ .argv = &.{ "--lock-timeout", " 1.5 " }, .raw = " 1.5 ", .detail = null },
        .{ .argv = &.{ "--lock-timeout", "-1" }, .raw = "-1", .detail = null },
        .{ .argv = &.{ "--lock-timeout", "2" }, .raw = "2", .detail = null },
        .{ .argv = &.{ "--lock-timeout", "nan" }, .raw = "nan", .detail = "--lock-timeout is not a finite number" },
        .{ .argv = &.{ "--lock-timeout", "inf" }, .raw = "inf", .detail = "--lock-timeout is not a finite number" },
        .{ .argv = &.{"--lock-timeout=-inf"}, .raw = "-inf", .detail = "--lock-timeout is not a finite number" },
        .{ .argv = &.{ "--lock-timeout", "nope" }, .raw = "nope", .detail = "invalid float value: 'nope'" },
    };
    for (cases) |case| {
        var outcome = try argparse.parse(a, &spec, case.argv);
        defer outcome.deinit(a);
        if (case.detail) |detail| {
            switch (outcome) {
                .usage_error => |*err| {
                    const prefix = "argument --lock-timeout: ";
                    try std.testing.expect(std.mem.startsWith(u8, err.message, prefix));
                    try std.testing.expectEqualStrings(detail, err.message[prefix.len..]);
                },
                else => return error.TestUnexpectedResult,
            }
        } else {
            switch (outcome) {
                .ok => |*parsed| try std.testing.expectEqualStrings(case.raw, parsed.value("lock-timeout").?),
                else => return error.TestUnexpectedResult,
            }
        }
    }
}

test "parseBytes trims whitespace and accepts every u128 value" {
    try std.testing.expectEqual(@as(u128, 4096), try parseBytes(" \t4096\n"));
    try std.testing.expectEqual(@as(u128, 18446744073709551616), try parseBytes("18446744073709551616"));
    try std.testing.expectEqual(std.math.maxInt(u128), try parseBytes("340282366920938463463374607431768211455"));
    try std.testing.expectError(error.NotAnInteger, parseBytes("340282366920938463463374607431768211456"));
    try std.testing.expectEqual(@as(u128, 0), try parseBytes("-0"));
    try std.testing.expectError(error.BelowZero, parseBytes("-1"));
    try std.testing.expectError(error.BelowZero, parseBytes("-340282366920938463463374607431768211456000"));
    try std.testing.expectError(error.NotAnInteger, parseBytes("-"));
    try std.testing.expectError(error.NotAnInteger, parseBytes("-1x"));
    try std.testing.expectError(error.BelowZero, parseBytes(" -1 "));
    try std.testing.expectError(error.NotAnInteger, parseBytes("   "));
}

test "probe help includes --no-create" {
    try std.testing.expect(std.mem.indexOf(u8, probe_usage, "[--no-create]") != null);
    try std.testing.expect(std.mem.indexOf(u8, probe_help, "--no-create") != null);
    try std.testing.expect(std.mem.indexOf(u8, probe_usage, "[--require-eb]") != null);
    try std.testing.expect(std.mem.indexOf(u8, probe_help, "--require-eb") != null);
}

test "osError names a non-regular file without an errno" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    try std.testing.expectEqualStrings("not a regular file: '/x'", osError(arena.allocator(), error.NotRegularFile, "/x"));
}
