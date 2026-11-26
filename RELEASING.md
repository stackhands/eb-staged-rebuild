# Releasing

A release is a tag `vX.Y.Z` on `main`. The `release` workflow builds the
static Zig binaries for x86_64 and aarch64 with `-Dcommit` set to the
tag's commit and tests each on its own architecture: the Zig unit tests,
the golden command tests, the crash and fail matrices and the signal tests.

It refuses a release binary that contains a test hook
(`EB_STAGED_REBUILD_ACL_MODEL`, `EB_STAGED_REBUILD_FAIL_AT`, `EB_STAGED_REBUILD_NOW`), a build that
accepts `-Dtest-ops` with `-Drelease`, a binary whose `--version` names
another version or commit, and a tag that disagrees with the version of
the Python package (`eb_staged_rebuild.__version__`). It builds one wheel per
architecture with the binary inside, and opens a draft release with the
binaries, the wheels, an archive of `share/` (completions, man page),
`SHA256SUMS` and build provenance attestations.

`cog bump` does the mechanical part. `cog.toml` runs `tools/release-prepare`
before the bump commit; the script's header lists what it sets and what it
leaves to a person.

## Steps

1. Branch from `main`: `git switch -c chore/release-X.Y.Z origin/main`.
2. Check, and fix in a commit of its own: `share/` names every command and
   option (`tests/test_share.py`; regenerate the completions and the man page
   when the command line changed), and the `--version` example of
   `docs/orgmode/reference/cli.org` names this release.
3. `cog bump --minor` (or `--patch`, `--major`, `--version X.Y.Z`). It runs
   the hook, commits `chore(version): X.Y.Z` and creates the tag on that
   commit. `cog bump --minor --dry-run` prints the version only.
4. Push the branch, open the pull request, and merge it with a merge commit
   once its checks pass on both architectures, including the real-EasyBuild
   integration job and the ledger tests against a
   real eb-stack, so the tagged commit is on `main`.
5. Sign the tag and push it:
   `git tag -s -f vX.Y.Z -m "eb-staged-rebuild X.Y.Z" 'vX.Y.Z^{}' && git push origin vX.Y.Z`.
6. Check the draft release: two binaries, `SHA256SUMS`, attestations
   (`gh attestation verify FILE --repo stackhands/eb-staged-rebuild`), and a
   local build of the tag with the same sha256 (`zig build -Drelease=true
   -Dcommit=<7-char sha> -Dtarget=ARCH-linux-musl`; v1.0.0 predates
   `-Dcommit` and refuses it, so build that tag without it).
7. Publish the draft. Assets cannot change after that, so nothing is added
   to a published release.
8. `docs/orgmode/howto/install.org` walks through one release with its
   checksum and attestation; move it to this release once the assets exist.
