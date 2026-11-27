//! str() and repr() of the JSON values a journal holds, as Python prints
//! them in an f-string.
const std = @import("std");
const common = @import("zig_common");
const json = common.json;
const pytext = common.pytext;

const Allocator = std.mem.Allocator;
const Py = json.Py;

fn appendFloat(out: *std.ArrayList(u8), a: Allocator, f: f64) Allocator.Error!void {
    if (std.math.isNan(f)) return out.appendSlice(a, "nan");
    if (std.math.isInf(f)) return out.appendSlice(a, if (f > 0) "inf" else "-inf");
    // repr(float) through json's writer, which prints NaN and Infinity the
    // JSON way; those cases are handled above.
    const s = try json.dumps(a, .{ .float = f }, 0, false);
    try out.appendSlice(a, s);
}

/// repr(v): None, True and False, quoted text, and lists and dicts, as
/// Python prints them.
pub fn repr(out: *std.ArrayList(u8), a: Allocator, v: Py) Allocator.Error!void {
    switch (v) {
        .null => try out.appendSlice(a, "None"),
        .bool => |b| try out.appendSlice(a, if (b) "True" else "False"),
        .int => |n| try out.print(a, "{d}", .{n}),
        .float => |f| try appendFloat(out, a, f),
        .str => |s| try pytext.reprStr(out, a, s),
        .list => |items| {
            try out.append(a, '[');
            for (items, 0..) |it, i| {
                if (i > 0) try out.appendSlice(a, ", ");
                try repr(out, a, it);
            }
            try out.append(a, ']');
        },
        .dict => |entries| {
            try out.append(a, '{');
            for (entries, 0..) |e, i| {
                if (i > 0) try out.appendSlice(a, ", ");
                try pytext.reprStr(out, a, e.key);
                try out.appendSlice(a, ": ");
                try repr(out, a, e.value);
            }
            try out.append(a, '}');
        },
    }
}

/// str(v): text as it is, everything else as repr.
pub fn str(out: *std.ArrayList(u8), a: Allocator, v: ?Py) Allocator.Error!void {
    const x = v orelse return out.appendSlice(a, "None");
    switch (x) {
        .str => |s| try out.appendSlice(a, s),
        else => try repr(out, a, x),
    }
}

/// Python truthiness.
pub fn truthy(v: ?Py) bool {
    const x = v orelse return false;
    return switch (x) {
        .null => false,
        .bool => |b| b,
        .int => |n| n != 0,
        .float => |f| f != 0,
        .str => |s| s.len > 0,
        .list => |l| l.len > 0,
        .dict => |d| d.len > 0,
    };
}

/// Out of memory, or TypeError for a value Python would not format.
pub const FormatError = Allocator.Error || error{TypeError};

/// f"{v:<width}": text, int, float and bool take a width; None, lists and
/// dicts raise TypeError, as their __format__ refuses a format spec.
pub fn leftPad(out: *std.ArrayList(u8), a: Allocator, v: ?Py, width: usize) FormatError!void {
    const x = v orelse return error.TypeError;
    const start = out.items.len;
    switch (x) {
        .null, .list, .dict => return error.TypeError,
        .bool => |b| try out.appendSlice(a, if (b) "1" else "0"),
        .int, .float, .str => try str(out, a, x),
    }
    var chars: usize = 0;
    var i: usize = start;
    while (i < out.items.len) : (chars += 1) i += pytext.nextChar(out.items[i..]).len;
    if (chars < width) try out.appendNTimes(a, ' ', width - chars);
}

/// The value of `key` in a dict, or null.
pub fn get(v: Py, key: []const u8) ?Py {
    if (v != .dict) return null;
    for (v.dict) |e| if (std.mem.eql(u8, e.key, key)) return e.value;
    return null;
}

test "repr and str" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var out: std.ArrayList(u8) = .empty;
    try repr(&out, a, .{ .list = &.{ .{ .str = "it's" }, .{ .int = 3 }, .null, .{ .float = 1.5 }, .{ .str = "a\nb\xff" } } });
    try std.testing.expectEqualStrings("[\"it's\", 3, None, 1.5, 'a\\nb\\udcff']", out.items);
    out.clearRetainingCapacity();
    try leftPad(&out, a, .{ .str = "ok" }, 5);
    try std.testing.expectEqualStrings("ok   ", out.items);
    try std.testing.expectError(error.TypeError, leftPad(&out, a, .null, 5));
}
