Switch `zig/probe.zig` in `eb-staged-rebuild` to call `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` retry loop.
