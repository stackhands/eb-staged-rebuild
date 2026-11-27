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
    // RENAME_NOREPLACE refuses a path that appears after renamePath's
    // check. A filesystem that refuses the flag gets an ordinary rename:
    // a non-empty directory stays (ENOTEMPTY) and a symlink is not followed.
    var za: [sys.path_max:0]u8 = undefined;
    var zb: [sys.path_max:0]u8 = undefined;
    const oldp = try sys.z(&za, from);
    const newp = try sys.z(&zb, to);
    _ = sys.call(linux.renameat2, .{
        linux.AT.FDCWD,
        oldp.ptr,
        linux.AT.FDCWD,
        newp.ptr,
        linux.RENAME{ .NOREPLACE = true },
    }) catch |err| switch (err) {
        error.InvalidArgument, error.Unsupported => return sys.renameat(common.fsx.cwd, from, common.fsx.cwd, to),
        else => return err,
    };
}

fn renamePath(a: Allocator, from: []const u8, to: []const u8, why: *Why) Error!void {
    // The fallback names are empty before each rename. Replacing a path that
    // is already there would delete a kept generation or a symlink.
    if (!std.mem.eql(u8, kindOf(to), "missing"))
        return fail(a, why, error.Failed, "refusing to replace an existing path: {s}", .{to});
    ops.rename(from, to) catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, from)});
}

fn syncParent(a: Allocator, live: []const u8, why: *Why) Error!void {
    const parent = std.fs.path.dirname(live) orelse "/";
    ops.sync_dir(parent) catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, parent)});
}

const Fallback = enum { none, aside, swapped };

/// The two states a crash can leave. Any other combination, including a
/// directory or a symlink already using the hold name, is not one of them.
fn fallbackState(live: []const u8, staged: []const u8, hold: []const u8) Fallback {
    const lk = kindOf(live);
    const sk = kindOf(staged);
    const hk = kindOf(hold);
    if (!std.mem.eql(u8, hk, "dir")) return .none;
    if (std.mem.eql(u8, lk, "missing") and std.mem.eql(u8, sk, "dir")) return .aside;
    if (std.mem.eql(u8, lk, "dir") and std.mem.eql(u8, sk, "missing")) return .swapped;
    return .none;
}

fn restoreFallback(a: Allocator, live: []const u8, staged: []const u8, hold: []const u8, why: *Why) Error!void {
    const hk = kindOf(hold);
    if (std.mem.eql(u8, hk, "missing")) return;
    switch (fallbackState(live, staged, hold)) {
        .aside => try renamePath(a, hold, live, why),
        .swapped => {
            try renamePath(a, live, staged, why);
            try renamePath(a, hold, live, why);
        },
        .none => {
            if (!std.mem.eql(u8, hk, "dir")) return fail(a, why, error.NeedsIntervention, "fallback recovery path is not a directory: {s}", .{hold});
            return fail(a, why, error.NeedsIntervention, "cannot recover interrupted fallback swap: live={s}, staged={s}, hold={s}", .{ kindOf(live), kindOf(staged), hk });
        },
    }
    try syncParent(a, live, why);
}

/// recover_interrupted_swap: restore an interrupted fallback, if any.
pub fn recoverInterrupted(a: Allocator, live_in: []const u8, staged_in: []const u8, why: *Why) Error!void {
    const live = admin.abspath(a, live_in);
    const staged = admin.abspath(a, staged_in);
    const hold = a.print("{s}" ++ fallback_hold_suffix, .{staged}) catch return error.Failed;
    const state = fallbackState(live, staged, hold);
    // Both trees are still directories. A kept generation or a symlink may
    // use the hold name. Leave it, so the exchange can run.
    if (state == .none and std.mem.eql(u8, kindOf(live), "dir") and std.mem.eql(u8, kindOf(staged), "dir")) return;
    try restoreFallback(a, live, staged, hold, why);
    if (state == .none) return;
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
        syncParent(a, live, why) catch break :fallback;
        return .@"rename-fallback";
    }
    // A half-finished fallback has one name missing. When both are
    // directories, no rename ran or the hold is already gone. An occupied
    // hold is a path the exchange refused, not a swap to undo.
    if (std.mem.eql(u8, kindOf(live), "dir") and std.mem.eql(u8, kindOf(staged), "dir"))
        return error.Failed;
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
        if (k == 4) calls = 3;
        var why: Why = .{};
        try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
        ops.* = .{};
        try expectOriginal(a, p);
    }
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

fn linkTo(target: []const u8, link: []const u8) !void {
    var zt: [sys.path_max:0]u8 = undefined;
    var zl: [sys.path_max:0]u8 = undefined;
    const left = try sys.z(&zt, target);
    const right = try sys.z(&zl, link);
    try testing.expect(linux.errno(linux.symlinkat(left.ptr, linux.AT.FDCWD, right.ptr)) == .SUCCESS);
}

fn linkText(a: Allocator, path: []const u8) ![]const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = try sys.z(&zb, path);
    const n = try sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len });
    return a.dupe(u8, buf[0..n]);
}

test "an occupied fallback hold is not an interrupted swap" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const outside = try a.print("/tmp/esr-outside-{d}", .{linux.getpid()});
    defer common.fsx.deleteTree(a, common.fsx.cwd, outside) catch {};
    try atomic.makedirs(outside);
    var note: atomic.Why = .{};
    try atomic.atomicWrite(a, try a.print("{s}/PAYLOAD", .{outside}), "kept\n", .{}, &note);

    const linked = try testPair(a, "hold-link");
    defer common.fsx.deleteTree(a, common.fsx.cwd, linked.base) catch {};
    const link_hold = try a.print("{s}" ++ fallback_hold_suffix, .{linked.staged});
    try linkTo(outside, link_hold);
    var why: Why = .{};
    try testing.expectEqual(Method.@"renameat2-exchange", try swapDirectories(a, linked.live, linked.staged, true, true, .{}, "swap", &why));
    try testing.expectEqualStrings("new\n", try payload(a, linked.live));
    try testing.expectEqualStrings("old\n", try payload(a, linked.staged));
    try testing.expectEqualStrings(outside, try linkText(a, link_hold));
    try testing.expectEqualStrings("kept\n", try payload(a, outside));

    const parked = try testPair(a, "hold-dir");
    defer common.fsx.deleteTree(a, common.fsx.cwd, parked.base) catch {};
    const dir_hold = try a.print("{s}" ++ fallback_hold_suffix, .{parked.staged});
    try atomic.makedirs(dir_hold);
    try atomic.atomicWrite(a, try a.print("{s}/PAYLOAD", .{dir_hold}), "kept\n", .{}, &note);
    try testing.expectEqual(Method.@"renameat2-exchange", try swapDirectories(a, parked.live, parked.staged, true, true, .{}, "swap", &why));
    try testing.expectEqualStrings("kept\n", try payload(a, dir_hold));
    try testing.expectEqualStrings("new\n", try payload(a, parked.live));

    const blocked = try testPair(a, "hold-fallback");
    defer common.fsx.deleteTree(a, common.fsx.cwd, blocked.base) catch {};
    const blocked_hold = try a.print("{s}" ++ fallback_hold_suffix, .{blocked.staged});
    try linkTo(outside, blocked_hold);
    try testing.expectError(error.Failed, swapDirectories(a, blocked.live, blocked.staged, false, true, .{}, "swap", &why));
    try testing.expect(std.mem.startsWith(u8, why.msg, "refusing to replace an existing path: "));
    try testing.expect(std.mem.indexOf(u8, why.msg, blocked_hold) != null);
    try testing.expectEqualStrings("old\n", try payload(a, blocked.live));
    try testing.expectEqualStrings("new\n", try payload(a, blocked.staged));
    try testing.expectEqualStrings(outside, try linkText(a, blocked_hold));
    try testing.expectEqualStrings("kept\n", try payload(a, outside));
}

test "an occupied directory hold fails the fallback before any rename" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "hold-refuse");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    const hold = try a.print("{s}" ++ fallback_hold_suffix, .{p.staged});
    try atomic.makedirs(hold);
    var note: atomic.Why = .{};
    try atomic.atomicWrite(a, try a.print("{s}/PAYLOAD", .{hold}), "kept\n", .{}, &note);
    var why: Why = .{};
    try testing.expectError(error.Failed, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    try testing.expect(std.mem.startsWith(u8, why.msg, "refusing to replace an existing path: "));
    try testing.expect(std.mem.indexOf(u8, why.msg, hold) != null);
    try testing.expectEqualStrings("old\n", try payload(a, p.live));
    try testing.expectEqualStrings("new\n", try payload(a, p.staged));
    try testing.expectEqualStrings("kept\n", try payload(a, hold));
    try testing.expectEqualStrings("dir", kindOf(p.live));
    try testing.expectEqualStrings("dir", kindOf(p.staged));
}
