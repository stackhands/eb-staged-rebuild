//! EB_STAGED_REBUILD_NOW for a replaceable build only.
//!
//! The release binary does not import this file. tests/now_cli.py is the
//! Python side. A number of seconds since the epoch, from 0 through
//! 253402300799, replaces the wall clock. Anything else is refused before
//! the command runs.
const std = @import("std");
const common = @import("zig_common");
const admin = @import("admin.zig");
const probe = @import("probe.zig");

const cli = common.cli;
const pytext = common.pytext;
const Allocator = std.mem.Allocator;

const space = " \t\n\r\x0b\x0c";
/// 10000-01-01T00:00:00Z. The second before it is 9999-12-31T23:59:59Z.
const limit: f64 = 253402300800;

var fixed: ?f64 = null;

fn die(a: Allocator, comptime why: []const u8, raw: []const u8) noreturn {
    var quoted: std.ArrayList(u8) = .empty;
    pytext.reprStr(&quoted, a, raw) catch {
        cli.err("eb-staged-rebuild: EB_STAGED_REBUILD_NOW is not a representable timestamp\n", .{});
        std.process.exit(2);
    };
    cli.err("eb-staged-rebuild: EB_STAGED_REBUILD_NOW " ++ why ++ ": {s}\n", .{quoted.items});
    std.process.exit(2);
}

pub fn bind(a: Allocator, env: probe.Env) void {
    const raw = env.get("EB_STAGED_REBUILD_NOW") orelse return;
    const text = std.mem.trim(u8, raw, space);
    if (text.len == 0) return;
    const stamp = std.fmt.parseFloat(f64, text) catch die(a, "is not a number", raw);
    if (!std.math.isFinite(stamp) or stamp < 0 or stamp >= limit) {
        if (!std.math.isFinite(stamp)) die(a, "is not a finite number", raw);
        die(a, "is not a representable timestamp", raw);
    }
    const secs: u64 = @intFromFloat(@floor(stamp));
    const year = (std.time.epoch.EpochSeconds{ .secs = secs }).getEpochDay().calculateYearDay().year;
    if (year < 1970 or year > 9999) die(a, "is not a representable timestamp", raw);
    fixed = stamp;
}

pub fn now() f64 {
    return fixed orelse admin.nowSeconds();
}
