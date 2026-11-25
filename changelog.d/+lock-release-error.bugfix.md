The Zig build reports a failed release of the state-root lock, as the
Python tool does. If `fcntl` or `flock` fails to release the lock at the
end of `run`, `resume`, `rollback`, `retire`, `verify` or `gc`, the
command exits 2 with `eb-staged-rebuild: [Errno N] strerror`. Before, it
dropped the error and exited with its own status. In that case `gc` prints
no actions, because Python prints them only after it releases the lock.
After a refusal under the lock the binary keeps the refusal and drops the
failed release; COMPAT.md lists the difference. The build pins zig-common
`a45142ad` on `develop`, where `state.RootLock.release` returns the error.
