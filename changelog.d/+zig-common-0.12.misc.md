The Zig build now uses zig-common v0.12.0. A directory open goes from
`[Errno 22] Invalid argument` to `[Errno 21] Is a directory`, and any
other non-regular file goes from `[Errno 22] Invalid argument` to
`not a regular file`.
