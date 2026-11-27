const std = @import("std");
const zon = @import("build.zig.zon");
const atomic_edit = @import("zig/atomic_edit.zig");

/// The static binary, stripped unless built for debugging, with the
/// commit -Dcommit names, or with -Dtest-ops the test artifact; and the
/// steps docs and test.
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{ .default_target = .{ .abi = .musl, .cpu_model = .baseline } });
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseSafe });
    const commit = b.option([]const u8, "commit", "Commit the binary is built from") orelse "unknown";
    // Debug info carries absolute build paths; a release binary leaves it
    // out so two builds of one commit are byte-identical.
    const strip = b.option(bool, "strip", "Leave out debug info (default: on for release builds)") orelse (optimize != .debug);

    // The test artifact lets a test replace the ACL calls (zig/aclmodel.zig).
    // It is never a release: -Dtest-ops with -Drelease is refused, and the
    // artifact has its own name.
    const test_ops = b.option(bool, "test-ops", "Build the test artifact eb-staged-rebuild-test-ops (never a release)") orelse false;
    if (test_ops and optimize != .debug) {
        std.debug.print("-Dtest-ops builds a test artifact; it cannot be combined with -Drelease\n", .{});
        std.process.exit(1);
    }

    const options = b.addOptions();
    options.addOption([]const u8, "version", zon.version);
    options.addOption([]const u8, "commit", commit);

    const common = b.dependency("zig_common", .{ .target = target, .optimize = optimize, .@"test-ops" = test_ops });
    // `readAllAcls` is the shared access-list read. It is file-private, and a
    // directory default name makes its list check refuse before the NFSv4
    // rule runs. Publish the helper and let one call omit that single name.
    // The directory is the dependency's own path, so the pin in build.zig.zon
    // is the package this rewrites.
    const dep_root = common.builder.root;
    const pkg_root = dep_root.root_dir.path orelse {
        std.debug.print("zig-common package is not available\n", .{});
        std.process.exit(1);
    };
    const root = if (dep_root.sub_path.len == 0) pkg_root else std.fs.path.join(b.allocator, &.{ pkg_root, dep_root.sub_path }) catch oomBuild();
    exposeReadAllAcls(b, root);
    const mod = b.createModule(.{
        .root_source_file = b.path("zig/main.zig"),
        .target = target,
        .optimize = optimize,
        .strip = strip,
        .imports = &.{
            .{ .name = "zig_common", .module = common.module("zig_common") },
            .{ .name = "build_options", .module = options.createModule() },
        },
    });
    const exe = b.addExecutable(.{ .name = if (test_ops) "eb-staged-rebuild-test-ops" else "eb-staged-rebuild", .root_module = mod, .linkage = .static });
    b.installArtifact(exe);
    // The completions and the man page are generated once from the command's
    // help (tests/test_share.py keeps them naming every command and option),
    // and the EasyBuild hook file the Python package also carries.
    b.installFile("share/completions/eb-staged-rebuild.bash", "share/completions/eb-staged-rebuild.bash");
    b.installFile("share/completions/_eb-staged-rebuild", "share/completions/_eb-staged-rebuild");
    b.installFile("share/man/eb-staged-rebuild.1", "share/man/eb-staged-rebuild.1");
    b.installFile("eb_staged_rebuild/site_hooks.py", "share/eb-staged-rebuild/site_hooks.py");

    // The Zig autodoc site, into <prefix>/docs (pixi task autodoc). A compile
    // of its own: asking the installed one for docs would add -femit-docs to
    // every build of the binary.
    const doc_exe = b.addExecutable(.{ .name = "eb-staged-rebuild", .root_module = mod });
    const docs = b.addInstallDirectory(.{
        .source_dir = doc_exe.getEmittedDocs(),
        .install_dir = .prefix,
        .install_subdir = "docs",
    });
    b.step("docs", "Generate the API reference").dependOn(&docs.step);

    const unit = b.addTest(.{ .root_module = mod });
    b.step("test", "Run the Zig unit tests").dependOn(&b.addRunArtifact(unit).step);
}

/// Publish the shared access-list helpers and the write options a held
/// directory needs: no parent flush, and a close whose error is checked.
fn exposeReadAllAcls(b: *std.Build, root: []const u8) void {
    const path = std.fs.path.join(b.allocator, &.{ root, "src/atomic.zig" }) catch oomBuild();
    const io = b.graph.io;
    const src = std.Io.Dir.cwd().readFileAlloc(io, path, b.allocator, .limited(1 << 20)) catch |err| {
        std.debug.print("cannot read {s}: {s}\n", .{ path, @errorName(err) });
        std.process.exit(1);
    };
    const rewritten = atomic_edit.rewrite(b.allocator, src) catch |err| switch (err) {
        error.Missing => missingAnchor(),
        error.OutOfMemory => oomBuild(),
    };
    if (rewritten.ptr == src.ptr) return;
    writeAtomic(io, path, rewritten);
}

fn writeAtomic(io: std.Io, path: []const u8, data: []const u8) void {
    std.Io.Dir.cwd().writeFile(io, .{ .sub_path = path, .data = data }) catch |err| {
        std.debug.print("cannot write {s}: {s}\n", .{ path, @errorName(err) });
        std.process.exit(1);
    };
}


fn missingAnchor() noreturn {
    std.debug.print("zig-common access-list read no longer matches the expected text\n", .{});
    std.process.exit(1);
}

fn oomBuild() noreturn {
    std.debug.print("out of memory\n", .{});
    std.process.exit(1);
}
