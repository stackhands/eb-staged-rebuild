//! verify: check a built run's staged trees before they are published, as
//! eb_staged_rebuild/verify.py does.
//!
//! elf: each DT_NEEDED entry of each dynamic ELF object is found the way
//! the dynamic loader searches (DT_RPATH when there is no DT_RUNPATH, then
//! DT_RUNPATH, then the loader's system directories) and lies inside the
//! stage, the live software tree or EESSI. LD_LIBRARY_PATH and ld.so.cache
//! are not consulted.
//! references: no file outside EasyBuild's own easybuild/ records names the
//! build directory or an EasyBuild temporary directory, and no RPATH or
//! RUNPATH entry, symbolic link or prepared module file names a path under
//! a temporary directory.
//! modules: $LMOD_CMD loads each staged module file with the stage first on
//! MODULEPATH, and each EBROOT* directory the load sets exists.
//! access: the publishing user owns the staged tree and can write where
//! publication renames and writes. Without a --prepare program, the staged
//! tree has the live tree's owner, group, mode bits and ACLs, and nothing in
//! it is less readable than its top directory.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const oom = @import("oom.zig").oom;
const admin = @import("admin.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");

const sys = common.sys;
const fsx = common.fsx;
const atomic = common.atomic;
const json = common.json;
const proc = common.proc;
const pytext = common.pytext;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// The checks, in the order the report lists them: verify.py's CHECKS.
pub const checks = [_][]const u8{ "elf", "references", "modules", "access" };
/// The report's name in the run's directory: verify.py's REPORT.
pub const report_name = "verify.json";
const shown_limit = 50;
const stamp = ".eb-staged-rebuild-complete";
const scan_limit: u64 = 64 << 20;
const lmod_timeout_s = 120;
const loader_timeout_s = 30;
const system_suffix = "(system search path)";
const elf_check = 0;
const references_check = 1;
const modules_check = 2;
const access_check = 3;

/// A path ends at the first byte that is none of these.
fn pathByte(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '+' or c == '-' or c == '/';
}

/// EasyBuild's temporary directories are tempfile.mkdtemp(prefix="eb-") names.
fn tmpNameByte(c: u8) bool {
    return (c >= 'a' and c <= 'z') or (c >= '0' and c <= '9') or c == '_';
}

/// The loader a shared object without PT_INTERP is loaded by, per e_machine.
fn defaultLoader(machine: u16) ?[]const u8 {
    return switch (machine) {
        21 => "lib64/ld64.so.2",
        62 => "lib64/ld-linux-x86-64.so.2",
        183 => "lib/ld-linux-aarch64.so.1",
        243 => "lib/ld-linux-riscv64-lp64d.so.1",
        else => null,
    };
}

// ---- paths as posixpath gives them ---------------------------------------

fn pjoin(a: Allocator, base: []const u8, tail: []const u8) []const u8 {
    if (tail.len > 0 and tail[0] == '/') return tail;
    if (base.len == 0 or base[base.len - 1] == '/') return a.print("{s}{s}", .{ base, tail }) catch oom();
    return a.print("{s}/{s}", .{ base, tail }) catch oom();
}

fn pdirname(p: []const u8) []const u8 {
    const i = if (std.mem.findScalarLast(u8, p, '/')) |k| k + 1 else 0;
    const head = p[0..i];
    for (head) |c| if (c != '/') return std.mem.trimEnd(u8, head, "/");
    return head;
}

fn pbasename(p: []const u8) []const u8 {
    const i = if (std.mem.findScalarLast(u8, p, '/')) |k| k + 1 else 0;
    return p[i..];
}

fn exists(p: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, p, true) catch return false;
    return true;
}

fn isDir(p: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, p, true) catch return false;
    return st.isDir();
}

fn under(path: []const u8, roots: []const []const u8) bool {
    for (roots) |root| {
        if (std.mem.eql(u8, root, "/") or std.mem.eql(u8, path, root)) return true;
        if (path.len > root.len and std.mem.startsWith(u8, path, root) and path[root.len] == '/') return true;
    }
    return false;
}

/// os.strerror of the errno behind `err`.
fn strerror(err: anyerror) []const u8 {
    const e = sys.asError(err) orelse return @errorName(err);
    const text = sys.describe(e);
    const at = std.mem.find(u8, text, "] ") orelse return text;
    return text[at + 2 ..];
}

// ---- the verify context a run records ------------------------------------

fn option(args: []const []const u8, name: []const u8) ?[]const u8 {
    var found: ?[]const u8 = null;
    for (args, 0..) |arg, i| {
        if (arg.len >= name.len + 3 and std.mem.startsWith(u8, arg, "--") and std.mem.startsWith(u8, arg[2..], name) and arg[2 + name.len] == '=') {
            found = arg[name.len + 3 ..];
        } else if (arg.len == name.len + 2 and std.mem.startsWith(u8, arg, "--") and std.mem.eql(u8, arg[2..], name) and i + 1 < args.len) {
            found = args[i + 1];
        }
    }
    return found;
}

fn remember(a: Allocator, dirs: *std.ArrayList([]const u8), value: ?[]const u8) void {
    const v = value orelse return;
    if (v.len == 0) return;
    const p = admin.abspath(a, v);
    for (dirs.items) |d| if (std.mem.eql(u8, d, p)) return;
    dirs.append(a, p) catch oom();
}

fn strList(a: Allocator, items: []const []const u8) Py {
    const out = a.alloc(Py, items.len) catch oom();
    for (items, 0..) |x, i| out[i] = Py.s(x);
    return .{ .list = out };
}

fn entry(k: []const u8, v: Py) Py.Entry {
    return .{ .key = k, .value = v };
}

/// The build and temporary directories and EESSI roots a run's stage is
/// judged against.
pub fn context(a: Allocator, eb_args: []const []const u8, env: probe.Env) Py {
    var build: std.ArrayList([]const u8) = .empty;
    var tmp: std.ArrayList([]const u8) = .empty;
    remember(a, &build, option(eb_args, "buildpath"));
    remember(a, &build, env.get("EASYBUILD_BUILDPATH"));
    if (env.get("EASYBUILD_PREFIX")) |p| if (p.len > 0) remember(a, &build, pjoin(a, p, "build"));
    if (env.get("HOME")) |h| if (h.len > 0) remember(a, &build, pjoin(a, pjoin(a, pjoin(a, h, ".local"), "easybuild"), "build"));
    remember(a, &tmp, option(eb_args, "tmpdir"));
    remember(a, &tmp, env.get("EASYBUILD_TMPDIR"));
    remember(a, &tmp, env.get("TMPDIR"));
    remember(a, &tmp, "/tmp");
    const eessi = env.get("EESSI_CVMFS_REPO");
    const eprefix = env.get("EESSI_EPREFIX");
    return .{ .dict = a.dupe(Py.Entry, &.{
        entry("build_dirs", strList(a, build.items)),
        entry("tmp_dirs", strList(a, tmp.items)),
        entry("eessi", if (eessi != null and eessi.?.len > 0) Py.s(admin.abspath(a, eessi.?)) else .null),
        entry("eessi_prefix", if (eprefix != null and eprefix.?.len > 0) Py.s(admin.abspath(a, eprefix.?)) else .null),
    }) catch oom() };
}

// ---- ELF ------------------------------------------------------------------

/// The dynamic-linking entries of one ELF object, as verify.py's Elf holds
/// them: its machine, its interpreter, its DT_NEEDED names, and its
/// DT_RPATH and DT_RUNPATH.
pub const Elf = struct {
    machine: u16,
    interp: []const u8,
    needed: []const []const u8,
    rpath: ?[]const u8,
    runpath: ?[]const u8,
};

const ElfError = sys.Error || error{Failed};

/// Up to `n` bytes at `off`; fewer at the end of the file.
fn readAt(a: Allocator, fd: sys.fd_t, off: u64, n: u64, size: u64) sys.Error![]const u8 {
    if (off > std.math.maxInt(i64)) return error.InvalidArgument;
    const want: usize = @intCast(if (off >= size) 0 else @min(n, size - off));
    const buf = a.alloc(u8, want) catch oom();
    var got: usize = 0;
    while (got < want) {
        const k = (try sys.result(linux.pread(fd, buf[got..].ptr, want - got, @intCast(off + got)))) orelse continue;
        if (k == 0) break;
        got += k;
    }
    return buf[0..got];
}

fn rd(comptime T: type, buf: []const u8, off: usize, little: bool) T {
    return std.mem.readInt(T, buf[off..][0..@sizeOf(T)], if (little) .little else .big);
}

fn cstr(strings: []const u8, off: u64) error{Failed}![]const u8 {
    if (off >= strings.len) return error.Failed;
    const start: usize = @intCast(off);
    const end = std.mem.findScalarPos(u8, strings, start, 0) orelse return error.Failed;
    return strings[start..end];
}

/// The dynamic-linking entries of an ELF executable or shared object, or
/// null.
pub fn readElf(a: Allocator, fd: sys.fd_t, size: u64) ?Elf {
    return readElfOrFail(a, fd, size) catch null;
}

fn readElfOrFail(a: Allocator, fd: sys.fd_t, size: u64) ElfError!?Elf {
    const head = try readAt(a, fd, 0, 64, size);
    if (head.len < 52 or !std.mem.eql(u8, head[0..4], "\x7fELF") or (head[4] != 1 and head[4] != 2) or (head[5] != 1 and head[5] != 2)) return null;
    const wide = head[4] == 2;
    const little = head[5] == 1;
    if (wide and head.len < 64) return null;
    const kind = rd(u16, head, 16, little);
    const machine = rd(u16, head, 18, little);
    if (kind != 2 and kind != 3) return null;
    const phoff: u64 = if (wide) rd(u64, head, 32, little) else rd(u32, head, 28, little);
    const phsize: u64 = rd(u16, head, if (wide) 54 else 42, little);
    const count: u64 = rd(u16, head, if (wide) 56 else 44, little);
    if (phsize < @as(u64, if (wide) 56 else 32) or count == 0) return null;
    const table = try readAt(a, fd, phoff, phsize * count, size);
    if (table.len < phsize * count) return null;
    const Load = struct { vaddr: u64, offset: u64, filesz: u64 };
    var loads: std.ArrayList(Load) = .empty;
    var dynamic: ?[2]u64 = null;
    var interp_at: ?[2]u64 = null;
    var i: usize = 0;
    while (i < count) : (i += 1) {
        const at: usize = @intCast(i * phsize);
        const ptype = rd(u32, table, at, little);
        const offset: u64 = if (wide) rd(u64, table, at + 8, little) else rd(u32, table, at + 4, little);
        const vaddr: u64 = if (wide) rd(u64, table, at + 16, little) else rd(u32, table, at + 8, little);
        const filesz: u64 = if (wide) rd(u64, table, at + 32, little) else rd(u32, table, at + 16, little);
        switch (ptype) {
            1 => loads.append(a, .{ .vaddr = vaddr, .offset = offset, .filesz = filesz }) catch oom(),
            2 => dynamic = .{ offset, filesz },
            3 => interp_at = .{ offset, filesz },
            else => {},
        }
    }
    const dyn = dynamic orelse return null;
    const entries = try readAt(a, fd, dyn[0], @min(dyn[1], 1 << 20), size);
    const step: usize = if (wide) 16 else 8;
    var needed: std.ArrayList(u64) = .empty;
    var strtab: ?u64 = null;
    var strsz: ?u64 = null;
    var rpath: ?u64 = null;
    var runpath: ?u64 = null;
    var k: usize = 0;
    while (k < entries.len / step) : (k += 1) {
        const tag: i64 = if (wide) rd(i64, entries, k * step, little) else rd(i32, entries, k * step, little);
        const value: u64 = if (wide) rd(u64, entries, k * step + 8, little) else rd(u32, entries, k * step + 4, little);
        switch (tag) {
            0 => break,
            1 => needed.append(a, value) catch oom(),
            5 => strtab = value,
            10 => strsz = value,
            15 => rpath = value,
            29 => runpath = value,
            else => {},
        }
    }
    if (strtab == null or strsz == null) return null;
    var base: ?u64 = null;
    for (loads.items) |l| if (l.vaddr <= strtab.? and strtab.? - l.vaddr < l.filesz) {
        base = std.math.add(u64, strtab.? - l.vaddr, l.offset) catch return error.Failed;
        break;
    };
    const strings = try readAt(a, fd, base orelse return null, @min(strsz.?, 16 << 20), size);
    var interp: []const u8 = "";
    if (interp_at) |p| {
        const raw = try readAt(a, fd, p[0], @min(p[1], 4096), size);
        interp = raw[0 .. std.mem.findScalar(u8, raw, 0) orelse raw.len];
    }
    const names = a.alloc([]const u8, needed.items.len) catch oom();
    for (needed.items, 0..) |off, n| names[n] = try cstr(strings, off);
    return .{
        .machine = machine,
        .interp = interp,
        .needed = names,
        .rpath = if (rpath) |off| try cstr(strings, off) else null,
        .runpath = if (runpath) |off| try cstr(strings, off) else null,
    };
}

// ---- references -------------------------------------------------------------

fn findFrom(data: []const u8, start: usize, needle: []const u8) ?usize {
    if (needle.len == 0) return if (start <= data.len) start else null;
    if (start > data.len) return null;
    return std.mem.findPos(u8, data, start, needle);
}

/// The first path in `data` under `root` (an EasyBuild temporary directory
/// in it, with `eb_tmp`) that is not under `exempt`.
fn find(a: Allocator, data: []const u8, root: []const u8, exempt: []const []const u8, eb_tmp: bool) ?[]const u8 {
    const needle = a.print("{s}{s}", .{ std.mem.trimEnd(u8, root, "/"), if (eb_tmp) "/eb-" else "" }) catch oom();
    var start: usize = 0;
    scan: while (true) {
        const index = findFrom(data, start, needle) orelse return null;
        start = index + 1;
        if (index > 0 and pathByte(data[index - 1])) continue;
        var end = index + needle.len;
        if (eb_tmp) {
            if (end + 8 > data.len) continue;
            for (data[end .. end + 8]) |c| if (!tmpNameByte(c)) continue :scan;
            end += 8;
        }
        if (end < data.len and data[end] != '/' and pathByte(data[end])) continue;
        while (end < data.len and pathByte(data[end])) end += 1;
        const token = data[index..end];
        if (!under(token, exempt)) return token;
    }
}

// ---- the stage ----------------------------------------------------------------

/// One problem a check found: the check's index in `checks`, and the message.
pub const Problem = struct { check: usize, message: []const u8 };

/// What checking a stage found: its problems, grouped by check, how many ELF
/// objects it read, and whether Lmod was there to load the module files.
pub const Outcome = struct {
    problems: []const Problem,
    objects: usize,
    lmod: bool,
};

const Pair = struct { old: []const u8, new: []const u8 };

fn strs(a: Allocator, v: ?Py) []const []const u8 {
    var out: std.ArrayList([]const u8) = .empty;
    if (v) |x| if (x == .list) for (x.list) |item| if (item == .str) out.append(a, item.str) catch oom();
    return out.items;
}

fn str(v: ?Py) []const u8 {
    const x = v orelse return "";
    return if (x == .str) x.str else "";
}

fn optStr(v: ?Py) ?[]const u8 {
    const x = v orelse return null;
    return if (x == .str and x.str.len > 0) x.str else null;
}

const Stage = struct {
    a: Allocator,
    env: probe.Env,
    journal: Py,
    build_dirs: []const []const u8,
    tmp_dirs: []const []const u8,
    eessi: ?[]const u8,
    eessi_prefix: ?[]const u8,
    software: []const u8,
    generation: []const u8,
    pairs: []Pair,
    exempt: []const []const u8,
    allowed: []const []const u8,
    temporary: []const []const u8,
    prepare: bool,
    loaders: std.StringHashMapUnmanaged([]const []const u8) = .empty,
    problems: std.ArrayList(Problem) = .empty,
    objects: usize = 0,

    fn add(s: *Stage, which: usize, comptime f: []const u8, args: anytype) void {
        s.problems.append(s.a, .{ .check = which, .message = s.a.print(f, args) catch oom() }) catch oom();
    }

    /// Where a path the stage records is now: a run's install paths name its
    /// staged trees.
    fn locate(s: *Stage, path: []const u8) []const u8 {
        for (s.pairs) |p| {
            if (std.mem.eql(u8, path, p.old) or (path.len > p.old.len and std.mem.startsWith(u8, path, p.old) and path[p.old.len] == '/'))
                return s.a.print("{s}{s}", .{ p.new, path[p.old.len..] }) catch oom();
        }
        return path;
    }

    fn temporaryRoot(s: *Stage, path: []const u8) ?[]const u8 {
        if (path.len == 0 or path[0] != '/' or under(path, s.exempt)) return null;
        for (s.temporary) |root| if (under(path, &.{root})) return root;
        return null;
    }

    fn checkAll(s: *Stage, mods: []const Py, trees: []const []const u8, lmod: ?[]const u8) void {
        for (mods, trees) |m, root| {
            const live = admin.abspath(s.a, str(pyfmt.get(m, "live_software")));
            const install = if (live.len > s.software.len + 1) live[s.software.len + 1 ..] else "";
            s.tree(install, root);
            s.moduleFile(m);
            if (lmod) |l| s.load(m, l);
            s.access(install, m, root);
        }
    }

    fn tree(s: *Stage, install: []const u8, root: []const u8) void {
        const top = fsx.statAt(fsx.cwd, root, false) catch |err| {
            s.add(access_check, "{s} cannot be read: {s}", .{ install, strerror(err) });
            return;
        };
        const acls = directoryAcls(s.a, root) catch &.{};
        const wants: u32 = if (acls.len > 0 or s.prepare) 0 else top.perm() & 0o044;
        var walk: Walk = .{ .s = s, .install = install, .wants = wants };
        walk.dir(root, "");
        if (walk.unreadable > 0)
            s.add(access_check, "{s}: {d} entries are less readable than the tree's top directory, the first {s}", .{ install, walk.unreadable, walk.first.? });
    }

    fn file(s: *Stage, path: []const u8, rel: []const u8, shown: []const u8, st: fsx.Stat) void {
        const f = fsx.openRegular(fsx.cwd, path, true) catch |err| {
            s.add(access_check, "{s} cannot be read: {s}", .{ shown, strerror(err) });
            return;
        };
        defer sys.close(f.fd);
        const size = f.stat.size;
        const head = readAt(s.a, f.fd, 0, 4, size) catch |err| {
            s.add(access_check, "{s} cannot be read: {s}", .{ shown, strerror(err) });
            return;
        };
        if (std.mem.eql(u8, head, "\x7fELF")) {
            if (readElf(s.a, f.fd, size)) |e| s.elf(path, shown, e);
        }
        if (std.mem.eql(u8, rel, "easybuild") or std.mem.startsWith(u8, rel, "easybuild/") or st.size > scan_limit) return;
        const data = fsx.readAll(s.a, f.fd, 1 << 40) catch |err| {
            s.add(access_check, "{s} cannot be read: {s}", .{ shown, strerror(err) });
            return;
        };
        for (s.build_dirs) |root| if (find(s.a, data, root, s.exempt, false)) |token|
            s.add(references_check, "{s}: names {s}, in the build directory {s}", .{ shown, token, root });
        for (s.tmp_dirs) |root| if (find(s.a, data, root, s.exempt, true)) |token|
            s.add(references_check, "{s}: names {s}, an EasyBuild temporary directory", .{ shown, token });
    }

    fn link(s: *Stage, path: []const u8, shown: []const u8) void {
        var zb: [sys.path_max:0]u8 = undefined;
        var buf: [sys.path_max]u8 = undefined;
        const z = sys.z(&zb, path) catch return;
        const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch return;
        const target = s.a.dupe(u8, buf[0..n]) catch oom();
        if (s.temporaryRoot(target)) |root| s.add(references_check, "{s}: symbolic link to {s}, under {s}", .{ shown, target, root });
    }

    fn elf(s: *Stage, path: []const u8, shown: []const u8, e: Elf) void {
        const a = s.a;
        s.objects += 1;
        const labelled = [_]struct { label: []const u8, value: ?[]const u8 }{ .{ .label = "RPATH", .value = e.rpath }, .{ .label = "RUNPATH", .value = e.runpath } };
        for (labelled) |item| {
            var it = std.mem.splitScalar(u8, item.value orelse "", ':');
            while (it.next()) |entry_path| {
                const root = s.temporaryRoot(entry_path) orelse continue;
                if (under(entry_path, s.build_dirs)) continue;
                s.add(references_check, "{s}: {s} entry {s} is under {s}", .{ shown, item.label, entry_path, root });
            }
        }
        var loader: ?[]const u8 = null;
        if (e.interp.len > 0) {
            const l = s.locate(e.interp);
            if (!exists(l)) {
                s.add(elf_check, "{s}: loader {s} not found", .{ shown, e.interp });
                return;
            }
            if (s.eessi != null and !under(admin.abspath(a, l), s.allowed))
                s.add(elf_check, "{s}: loader {s} is outside the stage, the site prefix and EESSI", .{ shown, e.interp });
            loader = l;
        } else if (defaultLoader(e.machine)) |name| {
            loader = pjoin(a, s.eessi_prefix orelse "/", name);
        }
        const system = s.systemDirs(loader);
        const origin = pdirname(path);
        var dirs: std.ArrayList([]const u8) = .empty;
        for ([_]?[]const u8{ if (e.runpath == null) e.rpath else null, e.runpath }) |value| {
            var it = std.mem.splitScalar(u8, value orelse "", ':');
            while (it.next()) |dir_entry| {
                if (dir_entry.len == 0) continue;
                const braced = std.mem.replaceOwned(u8, a, dir_entry, "${ORIGIN}", origin) catch oom();
                const expanded = std.mem.replaceOwned(u8, a, braced, "$ORIGIN", origin) catch oom();
                dirs.append(a, s.locate(expanded)) catch oom();
            }
        }
        dirs.appendSlice(a, system) catch oom();
        var allowed: std.ArrayList([]const u8) = .empty;
        allowed.appendSlice(a, s.allowed) catch oom();
        if (s.eessi == null) for (system) |d| allowed.append(a, admin.abspath(a, d)) catch oom();
        const searched = (e.rpath != null and e.rpath.?.len > 0) or (e.runpath != null and e.runpath.?.len > 0);
        for (e.needed) |name| {
            var found: ?[]const u8 = null;
            if (std.mem.findScalar(u8, name, '/') != null) {
                const candidate = s.locate(name);
                if (exists(candidate)) found = candidate;
            } else for (dirs.items) |d| {
                const candidate = pjoin(a, d, name);
                if (exists(candidate)) {
                    found = candidate;
                    break;
                }
            }
            if (found) |f| {
                if (!under(admin.abspath(a, f), allowed.items))
                    s.add(elf_check, "{s}: {s} resolves to {s}, outside the stage, the site prefix and EESSI", .{ shown, name, f });
            } else {
                s.add(elf_check, "{s}: {s} not found{s}", .{ shown, name, if (searched) "" else " (no RPATH or RUNPATH)" });
            }
        }
    }

    fn systemDirs(s: *Stage, loader: ?[]const u8) []const []const u8 {
        const l = loader orelse return &.{};
        if (s.loaders.get(l)) |d| return d;
        const inside = under(admin.abspath(s.a, l), &.{s.generation});
        const dirs = if (inside) beside(s.a, l) else loaderDirs(s.a, l);
        s.loaders.put(s.a, l, dirs) catch oom();
        return dirs;
    }

    fn moduleFile(s: *Stage, m: Py) void {
        const prepared = str(pyfmt.get(m, "prepared"));
        const f = fsx.openRegular(fsx.cwd, prepared, true) catch return;
        defer sys.close(f.fd);
        const data = fsx.readAll(s.a, f.fd, 1 << 30) catch return;
        for (s.temporary) |root| if (find(s.a, data, root, s.exempt, false)) |token| {
            s.add(references_check, "module file {s}: names {s}, under {s}", .{ str(pyfmt.get(m, "rel")), token, root });
            return;
        };
    }

    fn load(s: *Stage, m: Py, lmod: []const u8) void {
        const a = s.a;
        const rel = str(pyfmt.get(m, "rel"));
        var parts: std.ArrayList([]const u8) = .empty;
        var it = std.mem.splitScalar(u8, rel, '/');
        while (it.next()) |p| parts.append(a, p) catch oom();
        const tail = parts.items[if (parts.items.len >= 2) parts.items.len - 2 else 0..];
        var name = std.mem.join(a, "/", tail) catch oom();
        if (std.mem.endsWith(u8, name, ".lua")) name = name[0 .. name.len - 4];
        const lead = parts.items[0 .. parts.items.len - tail.len];
        const stage = admin.abspath(a, str(pyfmt.get(s.journal, "stage_modules")));
        const staged = pjoin(a, stage, rel);
        var path: std.ArrayList([]const u8) = .empty;
        for ([_][]const u8{ stage, admin.abspath(a, str(pyfmt.get(s.journal, "modules"))) }) |top| {
            var dir = top;
            for (lead) |p| dir = pjoin(a, dir, p);
            path.append(a, dir) catch oom();
        }
        var inherited = std.mem.splitScalar(u8, s.env.get("MODULEPATH") orelse "", ':');
        while (inherited.next()) |p| if (p.len > 0) path.append(a, p) catch oom();
        var b = common.env.Builder.fromEnv(a, common.env.Env.fromEnvp(s.env.envp)) catch oom();
        const names = a.dupe([]const u8, b.names.items) catch oom();
        for (names) |n| {
            if (std.mem.eql(u8, n, "LOADEDMODULES") or std.mem.eql(u8, n, "_LMFILES_") or std.mem.startsWith(u8, n, "_ModuleTable") or std.mem.startsWith(u8, n, "__LMOD_REF_COUNT")) b.unset(n);
        }
        b.set("MODULEPATH", std.mem.join(a, ":", path.items) catch oom()) catch oom();
        b.set("LMOD_IGNORE_CACHE", "1") catch oom();
        const envp = b.envp() catch |err| {
            s.add(modules_check, "module file {s}: cannot run {s}: {s}", .{ rel, lmod, strerror(err) });
            return;
        };
        const exe = resolve(a, s.env, lmod) orelse {
            s.add(modules_check, "module file {s}: cannot run {s}: {s}", .{ rel, lmod, "No such file or directory" });
            return;
        };
        const done = proc.run(a, exe, &.{ lmod, "bash", "load", name }, envp, .{
            .stdout_limit = 64 << 20,
            .stderr_limit = 16 << 20,
            .timeout_ms = lmod_timeout_s * 1000,
            .grace_ms = 0,
            .new_session = false,
            .exec_error = true,
        }) catch |err| {
            if (err == error.TimedOut) {
                s.add(modules_check, "module file {s}: Lmod did not load {s} within {d} seconds", .{ rel, name, lmod_timeout_s });
            } else {
                s.add(modules_check, "module file {s}: cannot run {s}: {s}", .{ rel, lmod, strerror(err) });
            }
            return;
        };
        const code: i64 = switch (done.term) {
            .exited => |c| c,
            .signaled => |sig| -@as(i64, sig),
        };
        const out = pytext.decodeReplace(a, done.stdout) catch oom();
        var values: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
        var lines = std.mem.splitScalar(u8, out, '\n');
        while (lines.next()) |raw| if (assignment(std.mem.trim(u8, raw, " \t\r"))) |kv| values.put(a, kv[0], unquote(kv[1])) catch oom();
        if (code != 0 or values.get("_LMFILES_") == null) {
            var said: std.ArrayList([]const u8) = .empty;
            const err_text = pytext.decodeReplace(a, done.stderr) catch oom();
            var parts_it = std.mem.splitScalar(u8, err_text, '\n');
            while (parts_it.next()) |raw| {
                const part = std.mem.trim(u8, raw, " \t\r");
                if (part.len > 0) {
                    said.append(a, part) catch oom();
                } else if (said.items.len > 0) break;
            }
            const reason = if (said.items.len > 0) std.mem.join(a, " ", said.items) catch oom() else a.print("exit status {d}", .{code}) catch oom();
            s.add(modules_check, "module file {s}: Lmod did not load {s}: {s}", .{ rel, name, reason });
            return;
        }
        const lmfiles = values.get("_LMFILES_").?;
        var listed = std.mem.splitScalar(u8, lmfiles, ':');
        const loaded = while (listed.next()) |p| {
            if (std.mem.eql(u8, p, staged)) break true;
        } else false;
        if (!loaded) s.add(modules_check, "module file {s}: Lmod loaded {s}, not {s}", .{ rel, lmfiles, staged });
        const keys = a.dupe([]const u8, values.keys()) catch oom();
        std.mem.sort([]const u8, keys, {}, lessText);
        for (keys) |key| {
            if (!std.mem.startsWith(u8, key, "EBROOT")) continue;
            const value = values.get(key).?;
            if (!isDir(s.locate(value))) s.add(modules_check, "module file {s}: {s} names {s}, which is not a directory", .{ rel, key, value });
        }
    }

    fn access(s: *Stage, install: []const u8, m: Py, root: []const u8) void {
        const a = s.a;
        const euid = linux.geteuid();
        const live = admin.abspath(a, str(pyfmt.get(m, "live_software")));
        var info: [2]fsx.Stat = undefined;
        var acls: [2][]const Xattr = undefined;
        for ([_][]const u8{ root, live }, 0..) |p, i| {
            info[i] = fsx.statAt(fsx.cwd, p, false) catch |err| {
                s.add(access_check, "{s}: cannot read access metadata of {s}: {s}", .{ install, p, strerror(err) });
                return;
            };
            acls[i] = directoryAcls(a, p) catch |err| {
                s.add(access_check, "{s}: cannot read access metadata of {s}: {s}", .{ install, p, strerror(err) });
                return;
            };
        }
        const mine = info[0];
        const theirs = info[1];
        if (euid != 0 and mine.uid != euid)
            s.add(access_check, "{s}: the staged tree belongs to uid {d}, not uid {d}, which publishes it", .{ install, mine.uid, euid });
        if (!s.prepare) {
            if (mine.uid != theirs.uid or mine.gid != theirs.gid)
                s.add(access_check, "{s}: the staged tree is owned by {d}:{d}, the live tree by {d}:{d}", .{ install, mine.uid, mine.gid, theirs.uid, theirs.gid });
            if (mine.perm() & 0o077 != theirs.perm() & 0o077)
                s.add(access_check, "{s}: the staged tree has mode {o:0>4} and the live tree {o:0>4}; publishing would change what group and others may do", .{ install, mine.perm(), theirs.perm() });
            if (!sameXattrs(acls[0], acls[1]))
                s.add(access_check, "{s}: the staged tree's ACLs are not the live tree's", .{install});
        }
        for ([_][]const u8{ live, admin.abspath(a, str(pyfmt.get(m, "live_module"))) }) |target| {
            const directory = nearestDir(target);
            var zb: [sys.path_max:0]u8 = undefined;
            const z = sys.z(&zb, directory) catch continue;
            if (linux.errno(linux.faccessat(linux.AT.FDCWD, z.ptr, linux.W_OK | linux.X_OK, 0)) != .SUCCESS) {
                s.add(access_check, "{s}: uid {d} cannot write {s}", .{ install, euid, directory });
                continue;
            }
            const parent = fsx.statAt(fsx.cwd, directory, true) catch continue;
            const owner = (fsx.statAt(fsx.cwd, target, false) catch continue).uid;
            if (euid != 0 and parent.mode & 0o1000 != 0 and euid != parent.uid and euid != owner)
                s.add(access_check, "{s}: {s} is sticky and {s} belongs to uid {d}", .{ install, directory, target, owner });
        }
    }
};

fn lessText(_: void, x: []const u8, y: []const u8) bool {
    return std.mem.order(u8, x, y) == .lt;
}

/// The closest existing directory above `path`: where publication creates
/// what is missing.
fn nearestDir(path: []const u8) []const u8 {
    var directory = pdirname(path);
    while (!std.mem.eql(u8, directory, "/") and !isDir(directory)) directory = pdirname(directory);
    return directory;
}

/// NAME=VALUE; as Lmod's bash output assigns a variable.
fn assignment(line: []const u8) ?[2][]const u8 {
    if (line.len == 0 or !(std.ascii.isAlphabetic(line[0]) or line[0] == '_')) return null;
    var i: usize = 1;
    while (i < line.len and (std.ascii.isAlphanumeric(line[i]) or line[i] == '_')) i += 1;
    if (i >= line.len or line[i] != '=' or line[line.len - 1] != ';' or line.len < i + 2) return null;
    return .{ line[0..i], line[i + 1 .. line.len - 1] };
}

fn unquote(value: []const u8) []const u8 {
    if (value.len >= 2 and value[0] == value[value.len - 1] and (value[0] == '\'' or value[0] == '"')) return value[1 .. value.len - 1];
    return value;
}

/// The executable subprocess would run for `name`: a path as given, a bare
/// name looked up on PATH.
fn resolve(a: Allocator, env: probe.Env, name: []const u8) ?[]const u8 {
    if (std.mem.findScalar(u8, name, '/') != null) return admin.abspath(a, name);
    return proc.which(a, name, env.get("PATH") orelse "") catch oom();
}

fn beside(a: Allocator, loader: []const u8) []const []const u8 {
    const base = pdirname(loader);
    return a.dupe([]const u8, &.{ base, pjoin(a, pjoin(a, pdirname(base), "usr"), pbasename(base)) }) catch oom();
}

/// The loader's own system search path, as ld.so --help prints it. The
/// loader runs with only LC_ALL set, so a bare name is looked up on
/// Python's default path.
fn loaderDirs(a: Allocator, loader: []const u8) []const []const u8 {
    var dirs: std.ArrayList([]const u8) = .empty;
    collect: {
        const exe = (if (std.mem.findScalar(u8, loader, '/') != null) admin.abspath(a, loader) else proc.which(a, loader, "/bin:/usr/bin") catch oom()) orelse break :collect;
        var b = common.env.Builder.init(a);
        b.set("LC_ALL", "C") catch oom();
        const envp = b.envp() catch break :collect;
        const done = proc.run(a, exe, &.{ loader, "--help" }, envp, .{
            .stdout_limit = 16 << 20,
            .stdout_overflow = .truncate,
            .stderr_limit = 0,
            .timeout_ms = loader_timeout_s * 1000,
            .grace_ms = 0,
            .new_session = false,
            .exec_error = true,
        }) catch break :collect;
        var lines = std.mem.splitScalar(u8, done.stdout, '\n');
        while (lines.next()) |raw| {
            const line = std.mem.trim(u8, raw, " \t\r");
            if (std.mem.endsWith(u8, line, system_suffix))
                dirs.append(a, a.dupe(u8, std.mem.trim(u8, line[0 .. line.len - system_suffix.len], " \t")) catch oom()) catch oom();
        }
    }
    return if (dirs.items.len > 0) dirs.items else beside(a, loader);
}

const Xattr = struct { name: []const u8, value: []const u8 };

/// `atomic.ops` rather than the system calls, so that the test artifact's
/// ACL model answers here as it does for publication.
fn xattrCall(f: anytype, args: anytype) sys.Error!usize {
    while (true) if (try sys.result(@call(.auto, f, args))) |n| return n;
}

/// A directory's system.* attributes, its access and default ACLs among
/// them, by name.
fn directoryAcls(a: Allocator, path: []const u8) sys.Error![]const Xattr {
    const fd = try sys.openat(fsx.cwd, path, .{ .DIRECTORY = true, .NOFOLLOW = true }, 0);
    defer sys.close(fd);
    const list_buf = a.alloc(u8, 64 << 10) catch oom();
    const n = xattrCall(atomic.ops.flistxattr, .{ fd, list_buf }) catch |err| switch (err) {
        error.Unsupported => return &.{},
        else => |e| return e,
    };
    var out: std.ArrayList(Xattr) = .empty;
    var it = std.mem.splitScalar(u8, list_buf[0..n], 0);
    while (it.next()) |name| {
        if (!std.mem.startsWith(u8, name, "system.")) continue;
        const zname = a.dupeSentinel(u8, name, 0) catch oom();
        const value_buf = a.alloc(u8, 64 << 10) catch oom();
        const got = try xattrCall(atomic.ops.fgetxattr, .{ fd, zname, value_buf });
        out.append(a, .{ .name = zname, .value = value_buf[0..got] }) catch oom();
    }
    std.mem.sort(Xattr, out.items, {}, struct {
        fn less(_: void, x: Xattr, y: Xattr) bool {
            return std.mem.order(u8, x.name, y.name) == .lt;
        }
    }.less);
    return out.items;
}

fn sameXattrs(x: []const Xattr, y: []const Xattr) bool {
    if (x.len != y.len) return false;
    for (x, y) |p, q| if (!std.mem.eql(u8, p.name, q.name) or !std.mem.eql(u8, p.value, q.value)) return false;
    return true;
}

/// _entries and the per-entry checks of Stage.tree: depth first, names in
/// byte order.
const Walk = struct {
    s: *Stage,
    install: []const u8,
    wants: u32,
    unreadable: usize = 0,
    first: ?[]const u8 = null,

    fn shownFor(w: *Walk, rel: []const u8) []const u8 {
        return if (rel.len > 0) w.s.a.print("{s}/{s}", .{ w.install, rel }) catch oom() else w.install;
    }

    fn dir(w: *Walk, directory: []const u8, prefix: []const u8) void {
        const a = w.s.a;
        const names = listdir(a, directory) catch |err| {
            w.s.add(access_check, "{s} cannot be read: {s}", .{ w.shownFor(std.mem.trimEnd(u8, prefix, "/")), strerror(err) });
            return;
        };
        for (names) |name| {
            const path = pjoin(a, directory, name);
            const rel = a.print("{s}{s}", .{ prefix, name }) catch oom();
            const shown = w.shownFor(rel);
            const st = fsx.statAt(fsx.cwd, path, false) catch |err| {
                w.s.add(access_check, "{s} cannot be read: {s}", .{ shown, strerror(err) });
                continue;
            };
            if (st.isLink()) {
                w.s.link(path, shown);
                continue;
            }
            if (w.wants != 0 and (st.isDir() or st.isReg())) {
                const need = w.wants | (if (st.isDir()) w.wants >> 2 else 0);
                if (st.perm() & need != need) {
                    w.unreadable += 1;
                    if (w.first == null) w.first = shown;
                }
            }
            if (st.isReg() and !std.mem.eql(u8, rel, stamp)) w.s.file(path, rel, shown, st);
            if (st.isDir()) w.dir(path, a.print("{s}/", .{rel}) catch oom());
        }
    }
};

fn listdir(a: Allocator, directory: []const u8) sys.Error![]const []const u8 {
    const fd = try fsx.openDir(fsx.cwd, directory, true);
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch oom();
    var names: std.ArrayList([]const u8) = .empty;
    while (try it.next()) |e| names.append(a, a.dupe(u8, e.name) catch oom()) catch oom();
    std.mem.sort([]const u8, names.items, {}, lessText);
    return names.items;
}

/// Check a built run's staged trees; the problems come grouped by check, in
/// the order they were found.
pub fn check(a: Allocator, env: probe.Env, journal: Py, trees: []const []const u8) Outcome {
    const lmod: ?[]const u8 = if (env.get("LMOD_CMD")) |l| (if (l.len > 0) l else null) else null;
    const recorded = pyfmt.get(journal, "verify_context");
    const ctx = if (pyfmt.truthy(recorded)) recorded.? else context(a, strs(a, pyfmt.get(journal, "eb_args")), env);
    const mods = blk: {
        const v = pyfmt.get(journal, "modules_plan") orelse break :blk &[_]Py{};
        break :blk if (v == .list) v.list else &[_]Py{};
    };
    const n = @min(mods.len, trees.len);
    var pairs: std.ArrayList(Pair) = .empty;
    for (mods[0..n], trees[0..n]) |m, t| {
        pairs.append(a, .{ .old = admin.abspath(a, str(pyfmt.get(m, "staged"))), .new = t }) catch oom();
        pairs.append(a, .{ .old = admin.abspath(a, str(pyfmt.get(m, "live_software"))), .new = t }) catch oom();
    }
    std.mem.sort(Pair, pairs.items, {}, struct {
        fn longer(_: void, x: Pair, y: Pair) bool {
            return x.old.len > y.old.len;
        }
    }.longer);
    const generation = admin.abspath(a, str(pyfmt.get(journal, "generation_root")));
    const software = admin.abspath(a, str(pyfmt.get(journal, "software")));
    var exempt: std.ArrayList([]const u8) = .empty;
    for ([_][]const u8{ "generation_root", "state_root", "software", "modules" }) |key| exempt.append(a, admin.abspath(a, str(pyfmt.get(journal, key)))) catch oom();
    const eessi = optStr(pyfmt.get(ctx, "eessi"));
    var allowed: std.ArrayList([]const u8) = .empty;
    allowed.appendSlice(a, &.{ generation, software }) catch oom();
    if (eessi) |e| allowed.append(a, e) catch oom();
    const build_dirs = strs(a, pyfmt.get(ctx, "build_dirs"));
    const tmp_dirs = strs(a, pyfmt.get(ctx, "tmp_dirs"));
    var temporary: std.ArrayList([]const u8) = .empty;
    temporary.appendSlice(a, build_dirs) catch oom();
    temporary.appendSlice(a, tmp_dirs) catch oom();
    var s: Stage = .{
        .a = a,
        .env = env,
        .journal = journal,
        .build_dirs = build_dirs,
        .tmp_dirs = tmp_dirs,
        .eessi = eessi,
        .eessi_prefix = optStr(pyfmt.get(ctx, "eessi_prefix")),
        .software = software,
        .generation = generation,
        .pairs = pairs.items,
        .exempt = exempt.items,
        .allowed = allowed.items,
        .temporary = temporary.items,
        .prepare = optStr(pyfmt.get(journal, "prepare_executable")) != null,
    };
    s.checkAll(mods[0..n], trees[0..n], lmod);
    var grouped: std.ArrayList(Problem) = .empty;
    for (checks, 0..) |_, c| for (s.problems.items) |p| if (p.check == c) grouped.append(a, p) catch oom();
    return .{ .problems = grouped.items, .objects = s.objects, .lmod = lmod != null };
}

/// The verify.json text and the record the journal keeps.
pub fn report(a: Allocator, run_id: []const u8, outcome: Outcome, at: f64, path: []const u8) struct { text: []const u8, record: Py } {
    var counts: [checks.len]Py.Entry = undefined;
    for (checks, 0..) |name, c| {
        var n: i64 = 0;
        for (outcome.problems) |p| if (p.check == c) {
            n += 1;
        };
        counts[c] = entry(name, if (c == modules_check and !outcome.lmod) .null else .{ .int = n });
    }
    const checks_py: Py = .{ .dict = a.dupe(Py.Entry, &counts) catch oom() };
    const skipped: Py = if (outcome.lmod) .{ .dict = &.{} } else .{ .dict = a.dupe(Py.Entry, &.{entry("modules", Py.s("LMOD_CMD is not set"))}) catch oom() };
    const result = if (outcome.problems.len > 0) "fail" else "pass";
    var listed: std.ArrayList(Py) = .empty;
    for (outcome.problems) |p| listed.append(a, .{ .dict = a.dupe(Py.Entry, &.{ entry("check", Py.s(checks[p.check])), entry("message", Py.s(p.message)) }) catch oom() }) catch oom();
    const body: Py = .{ .dict = a.dupe(Py.Entry, &.{
        entry("run_id", Py.s(run_id)),
        entry("result", Py.s(result)),
        entry("at", .{ .float = at }),
        entry("checks", checks_py),
        entry("skipped", skipped),
        entry("elf_objects", .{ .int = @intCast(outcome.objects) }),
        entry("problems", .{ .list = listed.items }),
    }) catch oom() };
    const text = a.print("{s}\n", .{json.dumps(a, body, 2, true) catch oom()}) catch oom();
    const record: Py = .{ .dict = a.dupe(Py.Entry, &.{
        entry("result", Py.s(result)),
        entry("at", .{ .float = at }),
        entry("checks", checks_py),
        entry("skipped", skipped),
        entry("elf_objects", .{ .int = @intCast(outcome.objects) }),
        entry("report", Py.s(path)),
        entry("forced", .{ .bool = false }),
    }) catch oom() };
    return .{ .text = text, .record = record };
}

fn log(comptime f: []const u8, args: anytype) void {
    common.cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

/// The lines verify prints once its report is written.
pub fn logReport(a: Allocator, run_id: []const u8, outcome: Outcome, path: []const u8) void {
    for (outcome.problems[0..@min(outcome.problems.len, shown_limit)]) |p| log("verify: {s}: {s}", .{ checks[p.check], p.message });
    if (outcome.problems.len > shown_limit) log("verify: {d} more problem(s) in {s}", .{ outcome.problems.len - shown_limit, path });
    var counts: std.ArrayList(u8) = .empty;
    for (checks, 0..) |name, c| {
        if (c > 0) counts.appendSlice(a, ", ") catch oom();
        if (c == modules_check and !outcome.lmod) {
            counts.print(a, "{s} skipped (LMOD_CMD is not set)", .{name}) catch oom();
            continue;
        }
        var n: usize = 0;
        for (outcome.problems) |p| if (p.check == c) {
            n += 1;
        };
        counts.print(a, "{s} {d}", .{ name, n }) catch oom();
    }
    log("verify {s}: {s} ({s}); {d} ELF object(s); report {s}", .{ run_id, if (outcome.problems.len > 0) "fail" else "pass", counts.items, outcome.objects, path });
}

test "paths join and split as posixpath does" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    try std.testing.expectEqualStrings("/a/b", pjoin(a, "/a", "b"));
    try std.testing.expectEqualStrings("/a/b", pjoin(a, "/a/", "b"));
    try std.testing.expectEqualStrings("/b", pjoin(a, "/a", "/b"));
    try std.testing.expectEqualStrings("b", pjoin(a, "", "b"));
    try std.testing.expectEqualStrings("/a", pdirname("/a/b"));
    try std.testing.expectEqualStrings("/", pdirname("/a"));
    try std.testing.expectEqualStrings("", pdirname("a"));
    try std.testing.expectEqualStrings("//", pdirname("//a"));
    try std.testing.expectEqualStrings("lib64", pbasename("/x/lib64"));
}

test "a reference is a whole path under the root, outside the exempt trees" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const exempt = [_][]const u8{"/b/state"};
    try std.testing.expectEqualStrings("/b/build/x/y.c", find(a, "x=/b/build/x/y.c:z", "/b/build", &exempt, false).?);
    try std.testing.expect(find(a, "/a/b/build/x", "/b/build", &exempt, false) == null);
    try std.testing.expect(find(a, "/b/buildx/y", "/b/build", &exempt, false) == null);
    try std.testing.expectEqualStrings("/b/build", find(a, "\"/b/build\"", "/b/build", &exempt, false).?);
    try std.testing.expect(find(a, "/b/state/x", "/b", &exempt, false) == null);
    try std.testing.expectEqualStrings("/tmp/eb-ab_12xyz/log", find(a, " /tmp/eb-ab_12xyz/log ", "/tmp", &exempt, true).?);
    try std.testing.expect(find(a, "/tmp/eb-short", "/tmp", &exempt, true) == null);
    try std.testing.expect(find(a, "/tmp/tmux-1000", "/tmp", &exempt, true) == null);
}

test "an Lmod assignment is NAME=VALUE; on its own line" {
    try std.testing.expectEqualStrings("EBROOTX", assignment("EBROOTX=/a/b;").?[0]);
    try std.testing.expectEqualStrings("/a/b", assignment("EBROOTX=/a/b;").?[1]);
    try std.testing.expectEqualStrings("", assignment("X=;").?[1]);
    try std.testing.expect(assignment("export EBROOTX;") == null);
    try std.testing.expect(assignment("1X=a;") == null);
    try std.testing.expect(assignment("X=a") == null);
    try std.testing.expectEqualStrings("a b", unquote("'a b'"));
}
