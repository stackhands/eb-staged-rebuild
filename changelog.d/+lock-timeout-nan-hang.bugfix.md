Treat a `NaN` deadline in `FileLock.acquire` as expired (`not (remaining > 0)`) instead of hanging indefinitely on a contended lock, matching the Zig binary's exit `2` refusal.
