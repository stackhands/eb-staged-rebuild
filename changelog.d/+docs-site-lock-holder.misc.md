The lock case of `docs/examples/site.sh` keeps the state root lock until the refused `gc` has run. It no longer waits a fixed half-second and then three seconds.
