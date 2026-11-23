The Zig `run` command sets `EASYBUILD_UMASK` in the build command through one helper. It leaves its own umask alone, as the Python command does.
