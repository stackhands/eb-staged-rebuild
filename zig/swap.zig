//! Crash-aware exchange of two sibling directories: renameat2
//! RENAME_EXCHANGE, or, where the filesystem refuses rename flags, three
//! ordinary renames through a hold name. A failure before a rename starts
//! restores the original names. Once a rename or an exchange has landed, a
//! failed close or flush keeps the new names and stops for intervention.
//! recoverInterrupted repairs a fallback that a crash stopped half way.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");

const sys = common.sys;
const fsx = common.fsx;
const atomic = common.atomic;
const run_mod = @import("run.zig");
const prefix_mod = @import("prefix.zig");
const Allocator = std.mem.Allocator;

/// The suffix of the hold name, `<staged>.fallback-old`, at which the
/// three-rename fallback keeps the live tree between its renames.
pub const fallback_hold_suffix = ".fallback-old";

/// How a swap was made: one renameat2 RENAME_EXCHANGE, or three renames.
pub const Method = enum { @"renameat2-exchange", @"rename-fallback" };

/// Why a swap stopped; set before an error is returned.
pub const Why = struct { msg: []const u8 = "" };

/// Refused: the two paths are not sibling real directories. Failed: the
/// exchange or a rename failed before it landed, and the names are as
/// they were. NeedsIntervention: a rename or an exchange landed and its
/// close or flush failed, or the names could not be recovered.
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
    sync_dir: *const fn (path: []const u8) sys.Error!void = fsyncPinned,
};

var test_ops: Ops = .{};
const real_ops: Ops = .{};
/// The calls in use: replaceable in a test build, the real ones otherwise.
pub const ops = if (atomic.replaceable) &test_ops else &real_ops;

/// The parent of `path`, pinned from `/` one component at a time the way
/// publish pins a destination. A symlink above a recorded software prefix
/// is followed. A symlink inside the software tree is `error.NotDir`.
fn pinnedParent(path: []const u8) sys.Error!run_mod.StageParent {
    return run_mod.pinStageParent("/", path) catch |err| switch (err) {
        error.Outside => error.NotDir,
        else => |e| e,
    };
}

/// `INVAL`, `NOSYS` and `OPNOTSUPP` would fall through to the ordinary
/// rename. A parent that cannot be pinned is a refusal, not that.
fn exchangePinErrno() linux.E {
    const e = sys.last_errno;
    if (e == .SUCCESS or e == .INVAL or e == .NOSYS or e == .OPNOTSUPP) return .NOTDIR;
    return e;
}

/// A test asks the next landed exchange or rename to fail one fsync or close.
const LandedFault = enum { none, exchange_fsync, exchange_close, rename_fsync, rename_close };
var landed_fault: LandedFault = .none;
var landed_fault_rename: usize = 0;
var rename_n: usize = 0;
var landed_needs_intervention: bool = false;
/// Set when a rename or exchange has landed and its close or flush failed.
/// Callers stop instead of putting the old names back.
var landed_stop: bool = false;

pub fn landedStop() bool {
    return landed_stop;
}

pub fn clearLandedStop() void {
    landed_stop = false;
}

fn markLanded() void {
    landed_needs_intervention = true;
    landed_stop = true;
}

const HeldKind = enum { exchange, rename };
const Held = struct {
    left: sys.fd_t,
    right: sys.fd_t,
    kind: HeldKind,
    from: []const u8,
    to: []const u8,
};
/// Parents kept open after a landed rename or exchange so the caller's
/// flush uses those descriptors instead of walking the path again.
var held: ?Held = null;

fn errnoToError(e: linux.E) sys.Error {
    const raw: usize = @bitCast(-@as(isize, @backingInt(e)));
    _ = sys.result(raw) catch |err| return err;
    return error.Unexpected;
}

fn landedFsyncFault() bool {
    if (comptime !atomic.replaceable) return false;
    switch (landed_fault) {
        .exchange_fsync => {
            landed_fault = .none;
            return true;
        },
        .rename_fsync => {
            if (landed_fault_rename != 0 and rename_n != landed_fault_rename) return false;
            landed_fault = .none;
            return true;
        },
        else => return false,
    }
}

fn landedCloseFault() bool {
    if (comptime !atomic.replaceable) return false;
    switch (landed_fault) {
        .exchange_close => {
            landed_fault = .none;
            return true;
        },
        .rename_close => {
            if (landed_fault_rename != 0 and rename_n != landed_fault_rename) return false;
            landed_fault = .none;
            return true;
        },
        else => return false,
    }
}

fn fsyncErrno(fd: sys.fd_t) linux.E {
    if (comptime atomic.replaceable) {
        if (landedFsyncFault()) {
            sys.last_errno = .IO;
            return .IO;
        }
    }
    sys.fsync(fd) catch return sys.last_errno;
    return .SUCCESS;
}

fn closeQuiet(fd: sys.fd_t) void {
    prefix_mod.closeChecked(fd) catch {};
}

fn closeOne(fd: sys.fd_t) sys.Error!void {
    if (comptime atomic.replaceable) {
        if (landedCloseFault()) {
            // Linux frees the descriptor even when close fails.
            sys.close(fd);
            return errnoToError(.IO);
        }
    }
    return prefix_mod.closeChecked(fd);
}

fn closePair(left: sys.fd_t, right: sys.fd_t) sys.Error!void {
    const first = closeOne(left);
    const second = closeOne(right);
    first catch |err| return err;
    return second;
}

fn dropHeld() void {
    const h = held orelse return;
    held = null;
    closeQuiet(h.left);
    closeQuiet(h.right);
}

/// Close parents kept after a landed rename or exchange. A failed close
/// leaves the new names in place.
fn releaseHeld() sys.Error!void {
    const h = held orelse return;
    held = null;
    closeOne(h.left) catch |err| {
        closeQuiet(h.right);
        markLanded();
        return err;
    };
    closeOne(h.right) catch |err| {
        markLanded();
        return err;
    };
}

/// fsync both pinned parents. On failure the exchange stays landed and the
/// descriptors are closed. On success they stay open so the caller's flush
/// uses them; a later close failure leaves the new names as well.
fn sealExchanged(left: sys.fd_t, right: sys.fd_t) linux.E {
    const e1 = fsyncErrno(left);
    const e2 = fsyncErrno(right);
    if (e1 != .SUCCESS or e2 != .SUCCESS) {
        const flush_err: linux.E = if (e1 != .SUCCESS) e1 else e2;
        closeQuiet(left);
        closeQuiet(right);
        markLanded();
        return flush_err;
    }
    held = .{ .left = left, .right = right, .kind = .exchange, .from = "", .to = "" };
    return .SUCCESS;
}

fn realExchange(first: [:0]const u8, second: [:0]const u8) linux.E {
    const left = pinnedParent(first) catch return exchangePinErrno();
    const right = pinnedParent(second) catch {
        sys.close(left.fd);
        return exchangePinErrno();
    };
    var za: [sys.path_max:0]u8 = undefined;
    var zb: [sys.path_max:0]u8 = undefined;
    const x = sys.z(&za, left.name) catch {
        sys.close(left.fd);
        sys.close(right.fd);
        return .NAMETOOLONG;
    };
    const y = sys.z(&zb, right.name) catch {
        sys.close(left.fd);
        sys.close(right.fd);
        return .NAMETOOLONG;
    };
    const rc = linux.errno(linux.renameat2(left.fd, x.ptr, right.fd, y.ptr, .{ .EXCHANGE = true }));
    if (rc != .SUCCESS) {
        sys.close(left.fd);
        sys.close(right.fd);
        return rc;
    }
    return sealExchanged(left.fd, right.fd);
}

fn realRename(from: []const u8, to: []const u8) sys.Error!void {
    try releaseHeld();
    const src = try pinnedParent(from);
    const dst = pinnedParent(to) catch |err| {
        closeQuiet(src.fd);
        return err;
    };
    sys.renameat(src.fd, src.name, dst.fd, dst.name) catch |err| {
        closeQuiet(src.fd);
        closeQuiet(dst.fd);
        return err;
    };
    rename_n += 1;
    const e1 = fsyncErrno(src.fd);
    const e2 = fsyncErrno(dst.fd);
    if (e1 != .SUCCESS or e2 != .SUCCESS) {
        // The rename has landed. A failed flush does not put the name back.
        closeQuiet(src.fd);
        closeQuiet(dst.fd);
        markLanded();
        return errnoToError(if (e1 != .SUCCESS) e1 else e2);
    }
    // Leave both parents open. The flush after the swap fsyncs these
    // descriptors; closing them here would make that flush walk the path.
    held = .{ .left = src.fd, .right = dst.fd, .kind = .rename, .from = from, .to = to };
}

/// Flush the directory `path`. When a rename or an exchange still holds its
/// parent descriptors, fsync those and close them. A failed close or fsync
/// leaves the landed names in place. With nothing held, pin `path` from `/`.
/// A symlink component is `error.NotDir`, not a flush of the link's target.
fn fsyncPinned(path: []const u8) sys.Error!void {
    const h = held orelse {
        const fd = try openPinned(path);
        return prefix_mod.fsyncCloseFd(fd);
    };
    held = null;
    const e1 = fsyncErrno(h.left);
    const e2 = fsyncErrno(h.right);
    const close_err: ?sys.Error = if (closePair(h.left, h.right)) null else |err| err;
    if (e1 != .SUCCESS or e2 != .SUCCESS or close_err != null) markLanded();
    if (e1 != .SUCCESS) return errnoToError(e1);
    if (e2 != .SUCCESS) return errnoToError(e2);
    if (close_err) |err| return err;
}

fn openPinned(path: []const u8) sys.Error!sys.fd_t {
    return run_mod.openPinnedDir(path);
}

fn renamePath(a: Allocator, from: []const u8, to: []const u8, why: *Why) Error!void {
    landed_needs_intervention = false;
    ops.rename(from, to) catch |err| {
        if (landed_needs_intervention) return fail(a, why, error.NeedsIntervention, "rename landed but its close or flush failed for {s}: {s}", .{ from, main_mod.osError(a, err, from) });
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, from)});
    };
}

fn syncParent(a: Allocator, live: []const u8, why: *Why) Error!void {
    const parent = std.fs.path.dirname(live) orelse "/";
    // A test may replace the flush. The real flush uses the pinned
    // descriptors when this swap still holds them.
    if (held != null and ops.sync_dir != fsyncPinned) dropHeld();
    landed_needs_intervention = false;
    ops.sync_dir(parent) catch |err| {
        landed_stop = true;
        if (landed_needs_intervention) return fail(a, why, error.NeedsIntervention, "swap landed but its close or flush failed for {s}: {s}", .{ live, main_mod.osError(a, err, parent) });
        return fail(a, why, error.NeedsIntervention, "{s}", .{main_mod.osError(a, err, parent)});
    };
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

/// recover_interrupted_swap: restore an interrupted fallback, if any.
pub fn recoverInterrupted(a: Allocator, live_in: []const u8, staged_in: []const u8, why: *Why) Error!void {
    defer dropHeld();
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
    landed_needs_intervention = false;
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
    defer dropHeld();
    rename_n = 0;
    landed_stop = false;
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
        if (landed_needs_intervention) {
            const text = a.print("[Errno {d}] renameat2(RENAME_EXCHANGE): {s}", .{ @backingInt(e), strerror(e) }) catch "swap failed";
            return fail(a, why, error.NeedsIntervention, "RENAME_EXCHANGE landed but its close or flush failed for {s}: {s}", .{ live, text });
        }
        if (e == .SUCCESS) {
            const after = a.print("{s}.exchange.after", .{step_prefix}) catch step_prefix;
            const synced = if (faults.fails(after)) blk: {
                dropHeld();
                break :blk injected(a, why, after);
            } else syncParent(a, live, why);
            synced catch |err| {
                // The exchange has landed. Leave the new names and stop.
                landed_stop = true;
                if (err == error.NeedsIntervention) return err;
                return fail(a, why, error.NeedsIntervention, "{s}", .{why.msg});
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
        renamePath(a, live, hold, why) catch |err| {
            if (err == error.NeedsIntervention) return err;
            break :fallback;
        };
        const staged_to_live = a.print("{s}.fallback.staged-to-live", .{step_prefix}) catch step_prefix;
        if (faults.fails(staged_to_live)) {
            why.msg = injectedText(a, staged_to_live);
            break :fallback;
        }
        renamePath(a, staged, live, why) catch |err| {
            if (err == error.NeedsIntervention) return err;
            break :fallback;
        };
        const hold_to_staged = a.print("{s}.fallback.hold-to-staged", .{step_prefix}) catch step_prefix;
        if (faults.fails(hold_to_staged)) {
            why.msg = injectedText(a, hold_to_staged);
            break :fallback;
        }
        renamePath(a, hold, staged, why) catch |err| {
            if (err == error.NeedsIntervention) return err;
            break :fallback;
        };
        try syncParent(a, live, why);
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

fn expectSwapped(a: Allocator, p: Pair) !void {
    try testing.expectEqualStrings("new\n", try payload(a, p.live));
    try testing.expectEqualStrings("old\n", try payload(a, p.staged));
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
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    const parent = std.fs.path.dirname(p.live).?;
    try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, p.live) == null);
    ops.* = .{};
    try expectSwapped(a, p);
}

test "an exchange whose sync fails keeps the landed names" {
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
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    ops.* = .{};
    try expectSwapped(a, p);
}

test "a parent sync that fails after an exchange stops for intervention" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "undo-flush-fail");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer ops.* = .{};
    calls = 0;
    failing_call = 1;
    ops.sync_dir = failingSync;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, true, .{}, "swap", &why));
    const parent = std.fs.path.dirname(p.live).?;
    try testing.expect(std.mem.indexOf(u8, why.msg, parent) != null);
    try testing.expect(std.mem.indexOf(u8, why.msg, p.live) == null);
    ops.* = .{};
    try expectSwapped(a, p);
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

test "a parent flush uses the pinned directory and does not follow a symlink" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try a.print("/tmp/esr-flush-pin-{d}", .{linux.getpid()});
    defer common.fsx.deleteTree(a, common.fsx.cwd, base) catch {};
    const real = try a.print("{s}/real", .{base});
    const other = try a.print("{s}/other", .{base});
    const link = try a.print("{s}/link", .{base});
    try atomic.makedirs(try a.print("{s}/child", .{real}));
    try atomic.makedirs(other);
    var zt: [sys.path_max:0]u8 = undefined;
    var zl: [sys.path_max:0]u8 = undefined;
    _ = try sys.call(linux.symlinkat, .{ (try sys.z(&zt, real)).ptr, linux.AT.FDCWD, (try sys.z(&zl, link)).ptr });
    try testing.expectError(error.NotDir, fsyncPinned(link));
    try fsyncPinned(real);
    try testing.expectEqualStrings("dir", kindOf(try a.print("{s}/child", .{real})));
    try testing.expectEqualStrings("dir", kindOf(other));
}

test "a directory flush returns an error when close fails" {
    try testing.expectError(error.InvalidArgument, prefix_mod.closeChecked(-1));
}

fn clearLandedFaults() void {
    landed_fault = .none;
    landed_fault_rename = 0;
    rename_n = 0;
    landed_needs_intervention = false;
    landed_stop = false;
    dropHeld();
    ops.* = .{};
}

/// Fail the fsync of the Nth landed rename (1-based). The rename itself
/// stays in place. Used by the landed-fallback tests instead of FUSE.
pub fn armLandedRenameFlush(nth: usize) void {
    landed_fault = .rename_fsync;
    landed_fault_rename = nth;
    rename_n = 0;
    landed_needs_intervention = false;
    landed_stop = false;
    dropHeld();
    if (comptime atomic.replaceable) ops.* = .{};
}

pub fn disarmLandedFault() void {
    landed_fault = .none;
    landed_fault_rename = 0;
    rename_n = 0;
    landed_needs_intervention = false;
    landed_stop = false;
    dropHeld();
    if (comptime atomic.replaceable) ops.* = .{};
}

test "a close after a landed exchange keeps the names" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "ex-close");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer clearLandedFaults();
    landed_fault = .exchange_close;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, false, .{}, "swap", &why));
    try expectSwapped(a, p);
}

test "a flush after a landed exchange stops for intervention" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "ex-flush");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer clearLandedFaults();
    landed_fault = .exchange_fsync;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, true, false, .{}, "swap", &why));
    try expectSwapped(a, p);
}

test "a close after a landed fallback rename keeps the names" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "fb-close");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer clearLandedFaults();
    landed_fault = .rename_close;
    landed_fault_rename = 3;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    try expectSwapped(a, p);
}

test "a flush after a landed fallback rename stops for intervention" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const p = try testPair(a, "fb-flush");
    defer common.fsx.deleteTree(a, common.fsx.cwd, p.base) catch {};
    defer clearLandedFaults();
    landed_fault = .rename_fsync;
    landed_fault_rename = 3;
    var why: Why = .{};
    try testing.expectError(error.NeedsIntervention, swapDirectories(a, p.live, p.staged, false, true, .{}, "swap", &why));
    try expectSwapped(a, p);
}
