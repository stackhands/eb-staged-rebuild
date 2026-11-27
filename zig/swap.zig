//! Crash-aware exchange of two sibling directories: renameat2
//! RENAME_EXCHANGE, or, where the filesystem refuses rename flags, three
//! ordinary renames through a hold name, every caught failure of which
//! restores the original names. recoverInterrupted repairs a fallback that a
//! crash stopped half way.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");

const sys = common.sys;
const atomic = common.atomic;
const Allocator = std.mem.Allocator;

/// The suffix of the hold name, `<staged>.fallback-old`, at which the
/// three-rename fallback keeps the live tree between its renames.
pub const fallback_hold_suffix = ".fallback-old";

/// How a swap was made: one renameat2 RENAME_EXCHANGE, or three renames.
pub const Method = enum { @"renameat2-exchange", @"rename-fallback" };

/// Why a swap stopped; set before an error is returned.
pub const Why = struct { msg: []const u8 = "" };

/// Refused: the two paths are not sibling real directories; Failed: the
/// exchange or a rename failed, and the names are as they were;
/// NeedsIntervention: the names could not be put back.
pub const Error = error{ Refused, NeedsIntervention, Failed };

fn fail(a: Allocator, why: *Why, comptime kind: Error, comptime f: []const u8, args: anytype) Error {
    why.msg = a.print(f, args) catch "swap failed";
    return kind;
}

/// A step a test made fail: Python's OSError(EIO, "injected failure at STEP").
fn injectedText(a: Allocator, step: []const u8) []const u8 {
    return a.print("[Errno 5] injected failure at {s}", .{step}) catch "swap failed";
}

fn injected(a: Allocator, why: *Why, step: []const u8) Error {
    why.msg = injectedText(a, step);
    return error.Failed;
}

fn kindOf(p: []const u8) []const u8 {
    return main_mod.lstatKind(p);
}

/// The calls a swap makes. Replaceable in the same builds as atomic.ops
/// (tests, a -Dtest-ops artifact), so a test can fail one of them the way
/// the Python tests pass their own rename and exchange_call; constant real
/// calls in any other build.
pub const Ops = struct {
    exchange: *const fn (first: [:0]const u8, second: [:0]const u8) linux.E = realExchange,
    rename: *const fn (from: []const u8, to: []const u8) sys.Error!void = realRename,
    sync_dir: *const fn (path: []const u8) sys.Error!void = atomic.fsyncDir,
};

var test_ops: Ops = .{};
const real_ops: Ops = .{};
/// The calls in use: replaceable in a test build, the real ones otherwise.
pub const ops = if (atomic.replaceable) &test_ops else &real_ops;

fn realExchange(first: [:0]const u8, second: [:0]const u8) linux.E {
    return linux.errno(linux.renameat2(linux.AT.FDCWD, first.ptr, linux.AT.FDCWD, second.ptr, .{ .EXCHANGE = true }));
}

fn realRename(from: []const u8, to: []const u8) sys.Error!void {
    return sys.renameat(common.fsx.cwd, from, common.fsx.cwd, to);
}

fn renamePath(a: Allocator, from: []const u8, to: []const u8, why: *Why) Error!void {
    ops.rename(from, to) catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, from)});
}

fn syncParent(a: Allocator, live: []const u8, why: *Why) Error!void {
    const parent = std.fs.path.dirname(live) orelse "/";
    ops.sync_dir(parent) catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, parent)});
}

fn restoreFallback(a: Allocator, live: []const u8, staged: []const u8, hold: []const u8, why: *Why) Error!void {
    const lk = kindOf(live);
    const sk = kindOf(staged);
    const hk = kindOf(hold);
    if (std.mem.eql(u8, hk, "missing")) return;
    if (!std.mem.eql(u8, hk, "dir")) return fail(a, why, error.NeedsIntervention, "fallback recovery path is not a directory: {s}", .{hold});
    if (std.mem.eql(u8, lk, "missing") and std.mem.eql(u8, sk, "dir")) {
        // The old live tree was moved aside; the staged tree has not moved.
        try renamePath(a, hold, live, why);
    } else if (std.mem.eql(u8, lk, "dir") and std.mem.eql(u8, sk, "missing")) {
        // The staged tree became live; put it back, then the old tree.
        try renamePath(a, live, staged, why);
        try renamePath(a, hold, live, why);
    } else {
        return fail(a, why, error.NeedsIntervention, "cannot recover interrupted fallback swap: live={s}, staged={s}, hold={s}", .{ lk, sk, hk });
    }
    try syncParent(a, live, why);
}

/// Invert a completed three-rename swap and flush the parent. A rename that
/// stops halfway is repaired by `restoreFallback`, which knows those hold
/// states. A failure leaves the caller to report it.
fn undoCompletedFallback(a: Allocator, live: []const u8, staged: []const u8, hold: []const u8, why: *Why) Error!void {
    renamePath(a, staged, hold, why) catch |err| return err;
    if (renamePath(a, live, staged, why)) |_| {} else |_| return restoreFallback(a, live, staged, hold, why);
    if (renamePath(a, hold, live, why)) |_| {} else |_| return restoreFallback(a, live, staged, hold, why);
    try syncParent(a, live, why);
}

/// recover_interrupted_swap: restore an interrupted fallback, if any.
pub fn recoverInterrupted(a: Allocator, live_in: []const u8, staged_in: []const u8, why: *Why) Error!void {
    const live = admin.abspath(a, live_in);
    const staged = admin.abspath(a, staged_in);
    const hold = a.print("{s}" ++ fallback_hold_suffix, .{staged}) catch return error.Failed;
    try restoreFallback(a, live, staged, hold, why);
    if (!std.mem.eql(u8, kindOf(hold), "missing")) return fail(a, why, error.NeedsIntervention, "fallback recovery left its hold path behind: {s}", .{hold});
}

fn exchange(a: Allocator, first: []const u8, second: []const u8) linux.E {
    var za: [sys.path_max:0]u8 = undefined;
    var zb: [sys.path_max:0]u8 = undefined;
    const x = sys.z(&za, first) catch return .NAMETOOLONG;
    const y = sys.z(&zb, second) catch return .NAMETOOLONG;
    _ = a;
    return ops.exchange(x, y);
}

fn strerror(e: linux.E) []const u8 {
    return common.sys.errnoText(e) orelse @tagName(e);
}

/// The exchange error an operator sees. A full, read-only or over-quota
/// filesystem names the live directory; other errnos stay as they were.
fn exchangeFailure(a: Allocator, e: linux.E, live: []const u8) []const u8 {
    const base = a.print("[Errno {d}] renameat2(RENAME_EXCHANGE): {s}", .{ @backingInt(e), strerror(e) }) catch "swap failed";
    if (e != .NOSPC and e != .ROFS and e != .DQUOT) return base;
    const described = main_mod.errnoPath(a, e, live);
    const close = std.mem.indexOf(u8, described, "] ") orelse return base;
    return a.print(
        "{s}] renameat2(RENAME_EXCHANGE): {s}",
        .{ described[0..close], described[close + 2 ..] },
    ) catch base;
}

/// swap_directories: exchange `live` and `staged` (siblings, both real
/// directories). Afterwards `staged` holds the retired live tree.
pub fn swapDirectories(a: Allocator, live_in: []const u8, staged_in: []const u8, allow_exchange: bool, allow_fallback: bool, faults: atomic.Faults, step_prefix: []const u8, why: *Why) Error!Method {
    const live = admin.abspath(a, live_in);
    const staged = admin.abspath(a, staged_in);
    try recoverInterrupted(a, live, staged, why);
    if (!std.mem.eql(u8, std.fs.path.dirname(live) orelse "", std.fs.path.dirname(staged) orelse ""))
        return fail(a, why, error.Refused, "the live and staged directories must be siblings", .{});
    for ([_][2][]const u8{ .{ "live", live }, .{ "staged", staged } }) |pair| {
        const k = kindOf(pair[1]);
        if (!std.mem.eql(u8, k, "dir")) return fail(a, why, error.Refused, "{s} swap path is {s}, not a real directory: {s}", .{ pair[0], k, pair[1] });
    }
    if (allow_exchange) {
        const before = a.print("{s}.exchange.before", .{step_prefix}) catch step_prefix;
        if (faults.fails(before)) return injected(a, why, before);
        const e = exchange(a, live, staged);
        if (e == .SUCCESS) {
            const after = a.print("{s}.exchange.after", .{step_prefix}) catch step_prefix;
            const synced = if (faults.fails(after)) injected(a, why, after) else syncParent(a, live, why);
            synced catch |err| {
                // The exchange landed. Put the names back and flush that
                // undo before reporting the failure, or a crash keeps the
                // exchanged names after publish or rollback says it failed.
                const undone = exchange(a, live, staged);
                if (undone != .SUCCESS) {
                    const text = a.print("[Errno {d}] renameat2(RENAME_EXCHANGE): {s}", .{ @backingInt(undone), strerror(undone) }) catch "swap failed";
                    return fail(a, why, error.NeedsIntervention, "RENAME_EXCHANGE succeeded but its rollback failed for {s}: {s}", .{ live, text });
                }
                syncParent(a, live, why) catch {
                    return fail(a, why, error.NeedsIntervention, "RENAME_EXCHANGE succeeded but its rollback failed for {s}: {s}", .{ live, why.msg });
                };
                return err;
            };
            return .@"renameat2-exchange";
        }
        if (e != .INVAL and e != .NOSYS and e != .OPNOTSUPP)
            return fail(a, why, error.Failed, "{s}", .{exchangeFailure(a, e, live)});
        if (!allow_fallback)
            return fail(a, why, error.Failed, "{s}", .{exchangeFailure(a, e, live)});
    }
    if (!allow_fallback)
        return fail(a, why, error.Failed, "RENAME_EXCHANGE is required", .{});
    const hold = a.print("{s}" ++ fallback_hold_suffix, .{staged}) catch return error.Failed;
    if (!std.mem.eql(u8, kindOf(hold), "missing")) try recoverInterrupted(a, live, staged, why);
    fallback: {
        const live_to_hold = a.print("{s}.fallback.live-to-hold", .{step_prefix}) catch step_prefix;
        if (faults.fails(live_to_hold)) {
            why.msg = injectedText(a, live_to_hold);
            break :fallback;
        }
        renamePath(a, live, hold, why) catch break :fallback;
        const staged_to_live = a.print("{s}.fallback.staged-to-live", .{step_prefix}) catch step_prefix;
        if (faults.fails(staged_to_live)) {
            why.msg = injectedText(a, staged_to_live);
            break :fallback;
        }
        renamePath(a, staged, live, why) catch break :fallback;
        const hold_to_staged = a.print("{s}.fallback.hold-to-staged", .{step_prefix}) catch step_prefix;
        if (faults.fails(hold_to_staged)) {
            why.msg = injectedText(a, hold_to_staged);
            break :fallback;
        }
        renamePath(a, hold, staged, why) catch break :fallback;
        syncParent(a, live, why) catch |err| {
            // The three renames landed. Put the names back and flush that
            // undo before reporting the failure, or a crash keeps the
            // swapped names after publish or rollback says it failed.
            const reported = why.msg;
            undoCompletedFallback(a, live, staged, hold, why) catch {
                return fail(a, why, error.NeedsIntervention, "fallback swap succeeded but its rollback failed for {s}: {s}", .{ live, why.msg });
            };
            why.msg = reported;
            return err;
        };
        return .@"rename-fallback";
    }
    const first = why.msg;
    var inner: Why = .{};
    restoreFallback(a, live, staged, hold, &inner) catch
        return fail(a, why, error.NeedsIntervention, "fallback swap failed and could not restore {s}: {s}", .{ live, inner.msg });
    why.msg = first;
    return error.Failed;
}

test "swap exchanges two sibling directories and back" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try a.print("/tmp/esr-swap-{d}", .{linux.getpid()});
    defer common.fsx.deleteTree(a, common.fsx.cwd, base) catch {};
    const live = try a.print("{s}/live", .{base});
    const staged = try a.print("{s}/.live.new", .{base});
    try atomic.makedirs(try a.print("{s}/L", .{live}));
    try atomic.makedirs(try a.print("{s}/S", .{staged}));
    var why: Why = .{};
    for ([_]bool{ true, false }) |allow| {
        _ = try swapDirectories(a, live, staged, allow, true, .{}, "swap", &why);
        try std.testing.expectEqualStrings("dir", kindOf(try a.print("{s}/S", .{live})));
        _ = try swapDirectories(a, live, staged, allow, true, .{}, "swap", &why);
        try std.testing.expectEqualStrings("dir", kindOf(try a.print("{s}/L", .{live})));
    }
    try std.testing.expectError(error.Refused, swapDirectories(a, live, base, true, true, .{}, "swap", &why));
}

// Ports of tests/test_swap.py. Each failure the Python test raises through
// its own rename or exchange_call (or a FaultInjector before that call) is
// the same call failing here through `ops`.
const testing = std.testing;

const Pair = struct { base: []const u8, live: []const u8, staged: []const u8 };

fn testPair(a: Allocator, name: []const u8) !Pair {
    const base = try a.print("/tmp/esr-swap-{s}-{d}", .{ name, linux.getpid() });
    common.fsx.deleteTree(a, common.fsx.cwd, base) catch {};
    const live = try a.print("{s}/live", .{base});
    const staged = try a.print("{s}/.live.staged", .{base});
    try atomic.makedirs(live);
    try atomic.makedirs(staged);
    var why: atomic.Why = .{};
    try atomic.atomicWrite(a, try a.print("{s}/PAYLOAD", .{live}), "old\n", .{}, &why);
    try atomic.atomicWrite(a, try a.print("{s}/PAYLOAD", .{staged}), "new\n", .{}, &why);
    return .{ .base = base, .live = live, .staged = staged };
}

fn payload(a: Allocator, dir: []const u8) ![]const u8 {
    const f = try common.fsx.openRegular(common.fsx.cwd, try a.print("{s}/PAYLOAD", .{dir}), false);
    defer sys.close(f.fd);
    return common.fsx.readAll(a, f.fd, 64);
}

fn expectOriginal(a: Allocator, p: Pair) !void {
    try testing.expectEqualStrings("old\n", try payload(a, p.live));
    try testing.expectEqualStrings("new\n", try payload(a, p.staged));
    try testing.expectEqualStrings("missing", kindOf(try a.print("{s}" ++ fallback_hold_suffix, .{p.staged})));
}

var failing_call: usize = 0;
var calls: usize = 0;

fn failingRename(from: []const u8, to: []const u8) sys.Error!void {
    calls += 1;
    if (calls == failing_call) return error.Unexpected;
    return realRename(from, to);
}

fn failingSync(path: []const u8) sys.Error!void {
    calls += 1;
    if (calls == failing_call) return error.Unexpected;
    return atomic.fsyncDir(path);
}

var undo_flushes: usize = 0;

fn failFirstFlush(path: []const u8) sys.Error!void {
    undo_flushes += 1;
    if (undo_flushes == 1) return error.Unexpected;
    return atomic.fsyncDir(path);
}

var fallback_renames: usize = 0;

fn failFourthRename(from: []const u8, to: []const u8) sys.Error!void {
    fallback_renames += 1;
    if (fallback_renames == 4) return error.Unexpected;
    return realRename(from, to);
}

fn failEveryFlush(_: []const u8) sys.Error!void {
    return error.Unexpected;
}

var undo_exchanges: usize = 0;

fn exchangeOnceThenDeny(first: [:0]const u8, second: [:0]const u8) linux.E {
    undo_exchanges += 1;
    if (undo_exchanges == 1) return realExchange(first, second);
    return .PERM;
}

fn deniedExchange(_: [:0]const u8, _: [:0]const u8) linux.E {
    return .PERM;
}

var exchange_errno: linux.E = .NOSPC;

fn configuredExchange(_: [:0]const u8, _: [:0]const u8) linux.E {
    return exchange_errno;
}

fn unsupportedExchange(_: [:0]const u8, _: [:0]const u8) linux.E {
    return .OPNOTSUPP;
}

test "a failed parent flush names the parent directory" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "parent");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    calls = 0;
    failing_call = 1;
    ops.sync_dir = failingSync;
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    const parent = std.fs.path.dirname(p.live).?;
    try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, p.live) == null);
    ops.* = .{};
    try expectOriginal(a, p);
}

test "an exchange whose sync fails is exchanged back" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "after");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    calls = 0;
    failing_call = 1;
    ops.sync_dir = failingSync;
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    ops.* = .{};
    try expectOriginal(a, p);
}

test "an undone exchange is flushed and a failed undo is reported" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    defer ops.* = .{};

    {
        const p = try testPair(a, "undo-flush");
        defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
        undo_flushes = 0;
        ops.* = .{};
        ops.sync_dir = failFirstFlush;
        var why: Why = .{};
        try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
        try testing.expectEqual(@as(usize, 2), undo_flushes);
        ops.* = .{};
        try expectOriginal(a, p);
    }
    {
        const p = try testPair(a, "undo-flush-fail");
        defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
        ops.* = .{};
        ops.sync_dir = failEveryFlush;
        var why: Why = .{};
        try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
        const parent = std.fs.path.dirname(p.live).?;
        try testing.expect(std.mem.indexOf(u8, why.msg, "RENAME_EXCHANGE succeeded but its rollback failed for ") != null);
        try testing.expect(std.mem.indexOf(u8, why.msg, p.live) != null);
        try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
        ops.* = .{};
        try expectOriginal(a, p);
    }
    {
        const p = try testPair(a, "undo-exchange-fail");
        defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
        undo_exchanges = 0;
        ops.* = .{};
        ops.exchange = exchangeOnceThenDeny;
        ops.sync_dir = failEveryFlush;
        var why: Why = .{};
        try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
        try testing.expect(std.mem.indexOf(u8, why.msg, "Operation not permitted") != null);
        try testing.expect(std.mem.indexOf(u8, why.msg, p.live) != null);
        ops.* = .{};
        try testing.expectEqualStrings("new\n", try payload(a, p.live));
        try testing.expectEqualStrings("old\n", try payload(a, p.staged));
    }
}

test "an exchange error other than unsupported leaves live unchanged" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "denied");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    ops.exchange = deniedExchange;
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    try testing.expectEqualStrings("[Errno 1] renameat2(RENAME_EXCHANGE): Operation not permitted", why.msg);
    ops.* = .{};
    try expectOriginal(a, p);
}

test "a full or read-only exchange names the live directory" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    defer ops.* = .{};
    const cases = [_]linux.E{ .NOSPC, .ROFS, .DQUOT };
    for (cases) |errno_value| {
        const p = try testPair(a, "full");
        defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
        exchange_errno = errno_value;
        ops.exchange = configuredExchange;
        var why: Why = .{};
        try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
        try testing.expect(std.mem.indexOf(u8, why.msg, "[Errno ") != null);
        try testing.expect(std.mem.indexOf(u8, why.msg, "renameat2(RENAME_EXCHANGE): ") != null);
        try testing.expect(std.mem.indexOf(u8, why.msg, p.live) != null);
        try testing.expect(std.mem.endsWith(u8, why.msg, "'"));
        ops.* = .{};
        try expectOriginal(a, p);
    }
}

test "an unsupported exchange uses the fallback" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "unsupported");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    ops.exchange = unsupportedExchange;
    var why: Why = .{};
    try testing.expectEqual(Method.@"rename-fallback", try swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    ops.* = .{};
    try testing.expectEqualStrings("new\n", try payload(a, p.live));
    try testing.expectEqualStrings("old\n", try payload(a, p.staged));
}

test "an unsupported exchange is refused when fallback is disabled" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "required");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    ops.exchange = unsupportedExchange;
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, true, false, .{}, "swap", &why));
    try testing.expectEqualStrings("[Errno 95] renameat2(RENAME_EXCHANGE): Operation not supported", why.msg);
    ops.* = .{};
    try expectOriginal(a, p);
}

test "every failed fallback rename or sync restores live" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    defer ops.* = .{};
    // Calls 1-3 are the three renames, call 4 the parent sync.
    for (1..5) |k| {
        const p = try testPair(a, "fallback");
        defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
        calls = 0;
        failing_call = k;
        ops.rename = failingRename;
        ops.sync_dir = if (k == 4) failingSync else atomic.fsyncDir;
        var why: Why = .{};
        try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
        ops.* = .{};
        try expectOriginal(a, p);
    }
}

test "a failed parent flush after the fallback renames restores live" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "fallback-sync");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    undo_flushes = 0;
    ops.sync_dir = failFirstFlush;
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    try testing.expectEqual(@as(usize, 2), undo_flushes);
    const parent = std.fs.path.dirname(p.live).?;
    try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
    ops.* = .{};
    try expectOriginal(a, p);
}

test "a fallback undo that cannot be flushed is reported" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "fallback-undo-flush");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    ops.sync_dir = failEveryFlush;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    const parent = std.fs.path.dirname(p.live).?;
    try testing.expect(std.mem.indexOf(u8, why.msg, "fallback swap succeeded but its rollback failed for ") != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, p.live) != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
    ops.* = .{};
    try expectOriginal(a, p);
}

test "a fallback undo whose rename fails leaves the swapped names" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "fallback-undo-rename");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    fallback_renames = 0;
    undo_flushes = 0;
    ops.rename = failFourthRename;
    ops.sync_dir = failFirstFlush;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    try testing.expect(std.mem.indexOf(u8, why.msg, "fallback swap succeeded but its rollback failed for ") != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, p.live) != null);
    ops.* = .{};
    try testing.expectEqualStrings("new\n", try payload(a, p.live));
    try testing.expectEqualStrings("old\n", try payload(a, p.staged));
}

test "recovery after the first fallback rename" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "recover1");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    const hold = try a.print("{s}" ++ fallback_hold_suffix, .{p.staged});
    try realRename(p.live, hold);
    var why: Why = .{};
    try recoverInterrupted(a, p.live, p.staged, &why);
    try expectOriginal(a, p);
}

test "recovery after the second fallback rename" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "recover2");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    const hold = try a.print("{s}" ++ fallback_hold_suffix, .{p.staged});
    try realRename(p.live, hold);
    try realRename(p.staged, p.live);
    var why: Why = .{};
    try recoverInterrupted(a, p.live, p.staged, &why);
    try expectOriginal(a, p);
}
