The deep walks hold one descriptor at a time: the prefix walkers close
their directory before descending, the livecheck walk keeps a worklist of
paths, and the admin cleanup refuses a tree nested past 512 levels with
`error.TooDeep`, so a run tree past the file-descriptor limit still walks
whole.
