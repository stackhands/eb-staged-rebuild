Reject `NaN` and `Infinity` ledger config timeouts in `eb_staged_rebuild.ledger_adapter.load_config` and `zig/publish.zig` instead of letting `_bounded` spin without a deadline.
