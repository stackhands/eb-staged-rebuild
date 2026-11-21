<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/logo/logo-dark.svg">
    <img src="branding/logo/logo-light.svg" alt="eb-staged-rebuild" width="460">
  </picture>
</p>

# eb-staged-rebuild

eb-staged-rebuild rebuilds an existing EasyBuild installation in a versioned generation directory, then exchanges that complete tree with the stable `software/<name>/<version>` directory and publishes the module file with a same-directory replace. The previous tree stays on disk as `software/<name>/.<version>.<run-id>`.

EasyBuild `--rebuild` and `--force` empty an existing install before the new install has passed its sanity checks. This program sends EasyBuild at `<state-root>/generations/<run-id>` and exchanges the live directory only after that generation is complete.

On EESSI's CernVM-FS repositories you do not need it: the publish transaction is the atomic swap. EESSI's rebuild hook ([`eb_hooks.py`](https://github.com/EESSI/software-layer-scripts/blob/main/eb_hooks.py), `pre_fetch_hook_remove_install_dir_for_rebuilds`) warns that a rebuild over a live install can pick up its old files ([easybuild-framework#5362](https://github.com/easybuilders/easybuild-framework/issues/5362)), and removes the old install early only when `EESSI_FORCE_EARLY_REMOVAL_OF_INSTALL_DIR` is set, which EESSI's install script does for `/cvmfs` targets.
It is for a stack on NFS, Lustre or GPFS that jobs read while it is rebuilt, where there is no transaction.

EasyBuild's own `--bwrap` builds the new tree at a temporary path with the real path bound over it, and writes `bwrap_info.json` there; moving the tree into place is left to the caller. With `--backend bwrap`, the first one `auto` tries, this program runs that build and does the move: it exchanges the finished tree with the live one with `renameat2(RENAME_EXCHANGE)`, or with three ordinary renames that restore both names on failure where the filesystem refuses rename flags, publishes the module file with a same-directory replace, and keeps the previous tree for jobs that still have it open. `installpath` and `userns` are for hosts without bubblewrap and rewrite the generation path in each file before the exchange.

Documentation: <https://stackhands.github.io/eb-staged-rebuild/>, built from the Org pages in `docs/orgmode/`: a tutorial, how-to guides, explanation, and a reference, including the API generated from the Python and Zig code.

```console
pixi run -e docs docbld
```

The HTML is written to `docs/build/html`. `docs/orgmode/contributing.org` says how to write a page.

Release bundles now include bash and zsh completion scripts with a man page produced from `_parser`, using pinned `shtab` and `argparse-manpage` in CI. Fish output is excluded because the generator toolchain does not provide fish completion support.

## Install

Linux only. There are two builds of the same command-line tool; their outputs agree byte for byte except where [COMPAT.md](COMPAT.md) lists a deliberate difference.

The static binary, one file per architecture, from the [latest release](https://github.com/stackhands/eb-staged-rebuild/releases/latest) (v1.0.0):

```bash
v=2.0.0 arch=x86_64   # or aarch64
curl -fsSLO "https://github.com/stackhands/eb-staged-rebuild/releases/download/v${v}/eb-staged-rebuild-${v}-${arch}-linux-musl"
curl -fsSLO "https://github.com/stackhands/eb-staged-rebuild/releases/download/v${v}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
install -m 0755 "eb-staged-rebuild-${v}-${arch}-linux-musl" ~/.local/bin/eb-staged-rebuild
eb-staged-rebuild --version
```

`gh attestation verify FILE --repo stackhands/eb-staged-rebuild` checks the build provenance. To build it yourself, Zig 0.17.1: `zig build -Drelease=true`.

The Python package, Python 3.11 or newer, no third-party dependencies:

```bash
python3 -m pip install .
eb-staged-rebuild --version
```

## Usage

Rebuild `Example/1.0-GCCcore-15.2.0`, which EasyBuild installed under `/opt/site`, from the directory that holds its easyconfig, and look at the run:

```bash
eb-staged-rebuild probe --path /opt/site/software
eb-staged-rebuild run --software /opt/site/software --modules /opt/site/modules \
  --run-id example-1 -- Example-1.0-GCCcore-15.2.0.eb
eb-staged-rebuild status --state-root /opt/site/.eb-staged-rebuild example-1
```

`probe` reports the sandboxes the machine has and whether the filesystem exchanges two directories in one call. `run` builds into `/opt/site/.eb-staged-rebuild/generations/example-1`, in the default state root for that software tree, and swaps the new tree in for `software/Example/1.0-GCCcore-15.2.0`, then its module file. Arguments after `--` go to `eb`. `rollback` with the run id swaps the previous tree back. The [how-to guide](https://stackhands.github.io/eb-staged-rebuild/howto/rebuild.html) covers the dry run and the output of each step, and the guides after it cover failed runs, rollback and `gc`.
`run --verify` checks the built stage before publishing it.
Sites that keep a filesystem reserve can add `--min-free-bytes BYTES` to `run`; without it, the command does not check free space.

## Changelog

A change adds a news fragment, `changelog.d/+<name>.<type>.md`, with type `feature`, `bugfix`, `doc`, `removal` or `misc`. `towncrier build --draft --version X.Y.Z` prints the release notes the fragments make. A release writes them into [CHANGELOG.md](CHANGELOG.md), as [RELEASING.md](RELEASING.md) describes.

## License

MIT, see [LICENSE](LICENSE). Author: Rohit Goswami &lt;rgoswami@ieee.org&gt;.

<!-- stackhands:begin -->
## Part of stackhands

eb-staged-rebuild is one of the [stackhands](https://github.com/stackhands) tools for building, installing and maintaining an HPC software stack with EasyBuild on top of EESSI.

| Tool | Job | Docs |
|---|---|---|
| [eb-stack](https://github.com/stackhands/eb-stack) | Turn conda-forge, Spack, PyPI, CRAN and Cargo recipes into easyconfigs; solve, shard and plan an EasyBuild build list | [docs](https://eb-stack.rgoswami.me/) |
| **eb-staged-rebuild** | Rebuild an installation beside the live one and swap it in, for a stack outside CernVM-FS that jobs read during the rebuild | [docs](https://stackhands.github.io/eb-staged-rebuild/) |
| [eessi-arch-guard](https://github.com/stackhands/eessi-arch-guard) | Reload EESSI in a Slurm task that inherited a stack loaded for another CPU or GPU, as srun from a login shell does | [docs](https://stackhands.github.io/eessi-arch-guard/) |
| [eb-local](https://github.com/stackhands/eb-local) | Build on EESSI-extend inside a Slurm job's node-local disk, compile your own code with RPATH, list and uninstall per CPU target | [docs](https://stackhands.github.io/eb-local/) |
| [eb-keep-logs](https://github.com/stackhands/eb-keep-logs) | Keep a failed EasyBuild command's own output, which --tmp-logdir does not; rotate a pipeline's log tree | [docs](https://stackhands.github.io/eb-keep-logs/) |
| [eb-pipeline](https://github.com/stackhands/eb-pipeline) | Drive a site's EasyBuild builds from one site file, through eb-stack, eb-staged-rebuild and eb-keep-logs | [docs](https://stackhands.github.io/eb-pipeline/) |
| [rfc9162](https://github.com/stackhands/rfc9162) | Append-only Merkle logs hashed as RFC 9162, with tiled storage and signed checkpoints | [docs](https://stackhands.github.io/rfc9162/) |
| [zig-common](https://github.com/stackhands/zig-common) | The shared Zig library under the Zig tools: system calls, checked files, child processes | [docs](https://stackhands.github.io/zig-common/) |
<!-- stackhands:end -->
