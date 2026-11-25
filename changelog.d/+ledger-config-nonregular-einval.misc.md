`loadConfig` maps a directory or other non-regular file from
`fsx.openRegular` to `EINVAL` and leaves a symlink loop unchanged.
With zig-common v0.10.0 those opens already fail with `EINVAL`, so the
ledger config refusal text does not change.
