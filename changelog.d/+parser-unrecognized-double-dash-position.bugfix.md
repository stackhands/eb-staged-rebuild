The Zig parser keeps `--` in unrecognized-argument errors as Python 3.14 does: every `--` for subcommands without positional arguments, all but the first for the others.
