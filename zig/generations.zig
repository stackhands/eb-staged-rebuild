//! The generations of each install path: its live tree and the hidden trees
//! beside it, as generations.py finds and prints them.
//!
//! A publication swaps the staged tree in and keeps the tree it replaced at
//! the run's hidden sibling (`.<version>.<run-id>`); a rollback swaps them
//! back and keeps the rolled-back tree there. Each tree names the run that
//! published it in its run marker. A tree without a marker predates
//! eb-staged-rebuild. A staged tree that was never published is not a
//! generation. Journal entries without the paths this needs are skipped.
const std = @import("std");
const common = @import("zig_common");
const oom = @import("oom.zig").oom;
const pyfmt = @import("pyfmt.zig");
const admin = @import("admin.zig");
const main_mod = @import("main.zig");
const run_mod = @import("run.zig");
const modulefile = @import("modulefile.zig");

const json = common.json;
const Allocator = std.mem.Allocator;
const Py = json.Py;

const checks = [_][]const u8{ "elf", "references", "modules", "access" };

/// One generation on disk: where the tree is, the run its marker names, its
/// state, when that run published it, and the verify record of that run.
pub const Row = struct {
    path: []const u8,
    run: ?[]const u8,
    state: []const u8,
    published_at: ?f64,
    verify: ?Py,
};

/// One install path, relative to the software tree and absolute, with its
/// generations: the live tree first, then the newest first.
pub const Group = struct { install: []const u8, live: []const u8, rows: []const Row };

/// One install path the journals name: relative to the software tree, and the
/// hidden siblings they record for it.
pub const Entry = struct { install: []const u8, hidden: std.ArrayList([]const u8) };

fn text(v: ?Py) ?[]const u8 {
    const s = admin.str(v) orelse return null;
    return if (s.len > 0) s else null;
}

fn number(v: ?Py) ?f64 {
    const x = v orelse return null;
    return switch (x) {
        .int => |n| @floatFromInt(n),
        .float => |f| f,
        else => null,
    };
}

fn published(state: ?[]const u8) ?[]const u8 {
    const s = state orelse return null;
    if (std.mem.eql(u8, s, "complete")) return "replaced";
    if (std.mem.eql(u8, s, "rolled_back")) return "rolled back";
    return null;
}

fn plan(a: Allocator, j: Py) []const Py {
    const v = pyfmt.get(j, "modules_plan") orelse return &.{};
    if (v != .list) return &.{};
    var out: std.ArrayList(Py) = .empty;
    for (v.list) |m| if (m == .dict) out.append(a, m) catch oom();
    return out.items;
}

const Paths = struct { live: []const u8, retired: []const u8 };

/// A plan entry's live path and hidden sibling, or null when it lacks either.
fn paths(a: Allocator, m: Py) ?Paths {
    const live = text(pyfmt.get(m, "live_software")) orelse return null;
    const retired = text(pyfmt.get(m, "retired")) orelse return null;
    return .{ .live = admin.abspath(a, live), .retired = admin.abspath(a, retired) };
}

fn isRun(j: Py) bool {
    return !admin.eqlStr(pyfmt.get(j, "kind"), "retire") and text(pyfmt.get(j, "software")) != null;
}

fn module(a: Allocator, j: Py, live: []const u8) ?Py {
    for (plan(a, j)) |m| if (paths(a, m)) |p| if (std.mem.eql(u8, p.live, live)) return m;
    return null;
}

/// Each install path the run journals name, with the hidden siblings they
/// record for it, in journal order.
pub fn installPaths(a: Allocator, journals: []const Py) std.StringArrayHashMapUnmanaged(Entry) {
    var found: std.StringArrayHashMapUnmanaged(Entry) = .empty;
    for (journals) |j| {
        if (!isRun(j)) continue;
        for (plan(a, j)) |m| {
            const p = paths(a, m) orelse continue;
            const got = found.getOrPut(a, p.live) catch oom();
            if (!got.found_existing) {
                const software = admin.abspath(a, text(pyfmt.get(j, "software")).?);
                got.value_ptr.* = .{ .install = modulefile.relpath(a, p.live, software), .hidden = .empty };
            }
            var seen = false;
            for (got.value_ptr.hidden.items) |h| seen = seen or std.mem.eql(u8, h, p.retired);
            if (!seen) got.value_ptr.hidden.append(a, p.retired) catch oom();
        }
    }
    return found;
}

fn rowLess(_: void, x: Row, y: Row) bool {
    const xl = !std.mem.eql(u8, x.state, "live");
    const yl = !std.mem.eql(u8, y.state, "live");
    if (xl != yl) return !xl;
    if ((x.published_at == null) != (y.published_at == null)) return x.published_at != null;
    const xp = x.published_at orelse 0;
    const yp = y.published_at orelse 0;
    if (xp != yp) return xp > yp;
    return json.pyTextLess(x.path, y.path);
}

const Keyed = struct { live: []const u8, entry: Entry };

fn groupLess(_: void, x: Keyed, y: Keyed) bool {
    if (!std.mem.eql(u8, x.entry.install, y.entry.install)) return json.pyTextLess(x.entry.install, y.entry.install);
    return json.pyTextLess(x.live, y.live);
}

/// Install paths by name, each with its generations on disk: the live tree,
/// then newest first.
pub fn collect(a: Allocator, journals: []const Py) []const Group {
    var runs: std.StringHashMapUnmanaged(Py) = .empty;
    for (journals) |j| {
        if (!isRun(j)) continue;
        const id = text(pyfmt.get(j, "run_id")) orelse continue;
        runs.put(a, id, j) catch oom();
    }
    const installs = installPaths(a, journals);
    var keyed: std.ArrayList(Keyed) = .empty;
    for (installs.keys(), installs.values()) |live, entry| keyed.append(a, .{ .live = live, .entry = entry }) catch oom();
    std.mem.sort(Keyed, keyed.items, {}, groupLess);
    var found: std.ArrayList(Group) = .empty;
    for (keyed.items) |k| {
        var rows: std.ArrayList(Row) = .empty;
        var candidates: std.ArrayList([]const u8) = .empty;
        candidates.append(a, k.live) catch oom();
        candidates.appendSlice(a, k.entry.hidden.items) catch oom();
        for (candidates.items) |path| {
            if (!std.mem.eql(u8, main_mod.lstatKind(path), "dir")) continue;
            const stamp = run_mod.treeRunId(a, path);
            const run: ?[]const u8 = if (stamp != null and stamp.?.len > 0) stamp else null;
            const source: ?Py = if (run) |r| runs.get(r) else null;
            const mod: ?Py = if (source) |s| module(a, s, k.live) else null;
            const when: ?f64 = if (mod) |m| number(pyfmt.get(m, "published_at")) else null;
            var state: []const u8 = undefined;
            if (std.mem.eql(u8, path, k.live)) {
                state = "live";
            } else if (run == null) {
                state = "original";
            } else if (mod == null) {
                state = "unknown";
            } else if (published(text(pyfmt.get(source.?, "state")))) |s| {
                if (when == null) continue;
                state = s;
            } else continue;
            rows.append(a, .{
                .path = path,
                .run = run,
                .state = state,
                .published_at = when,
                .verify = if (source) |s| pyfmt.get(s, "verify") else null,
            }) catch oom();
        }
        std.mem.sort(Row, rows.items, {}, rowLess);
        found.append(a, .{ .install = k.entry.install, .live = k.live, .rows = rows.items }) catch oom();
    }
    return found.items;
}

/// The source run's verify result: pass, fail or forced, with its problem
/// count.
pub fn verifyText(a: Allocator, record: ?Py) []const u8 {
    const r = record orelse return "-";
    if (r != .dict or r.dict.len == 0) return "-";
    const c = pyfmt.get(r, "checks");
    if (admin.eqlStr(pyfmt.get(r, "result"), "pass")) {
        var skipped: std.ArrayList([]const u8) = .empty;
        if (c != null and c.? == .dict) for (checks) |name| if (pyfmt.get(c.?, name)) |v| if (v == .null) skipped.append(a, name) catch oom();
        if (skipped.items.len == 0) return "pass";
        return a.print("pass ({s} skipped)", .{std.mem.join(a, ", ", skipped.items) catch oom()}) catch oom();
    }
    var problems: i64 = 0;
    if (c != null and c.? == .dict) for (checks) |name| {
        if (pyfmt.get(c.?, name)) |v| if (v == .int) {
            problems += v.int;
        };
    };
    const noun = if (problems == 1) "problem" else "problems";
    const forced = pyfmt.get(r, "forced");
    const word = if (forced != null and forced.? == .bool and forced.?.bool) "forced" else "fail";
    return a.print("{s} ({d} {s})", .{ word, problems, noun }) catch oom();
}

fn iso(a: Allocator, when: ?f64) []const u8 {
    return if (when) |w| admin.gmtimeStamp(a, w, true) else "-";
}

/// The lines `list` prints, as generations.py's format_generations prints
/// them: each install path, then one line for each of its generations.
pub fn print(a: Allocator, found: []const Group) []const u8 {
    if (found.len == 0) return "no generations\n";
    var out: std.ArrayList(u8) = .empty;
    for (found) |g| {
        out.print(a, "{s}  {s}\n", .{ g.install, g.live }) catch oom();
        for (g.rows) |row| {
            out.print(a, "  {s:<12} {s:<20}  {s}  verify {s}", .{ row.state, iso(a, row.published_at), row.run orelse "-", verifyText(a, row.verify) }) catch oom();
            if (!std.mem.eql(u8, row.state, "live")) out.print(a, "  {s}", .{row.path}) catch oom();
            out.append(a, '\n') catch oom();
        }
    }
    return out.items;
}
