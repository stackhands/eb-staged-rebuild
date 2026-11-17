"""ASV benchmarks for eb-staged-rebuild site hot paths."""

import contextlib
import io
import itertools
import json
import os
import shutil
import tempfile

from eb_staged_rebuild.atomicio import atomic_write
from eb_staged_rebuild.discover import order_module_files, walk_module_tree
from eb_staged_rebuild.engine import (
    Spec,
    modules_built_by_extended_dry_run,
    rollback,
    run,
    software_rels_from_dry_run,
)
from eb_staged_rebuild.faults import FaultInjector
from eb_staged_rebuild.livecheck import snapshot
from eb_staged_rebuild.modulefile import rewrite_root
from eb_staged_rebuild.paths import abspath
from eb_staged_rebuild.prefix import rewrite_tree

_PROBES = {
    "bwrap": {"installed": False, "usable": False, "reason": "disabled", "version": None, "path": None},
    "user_namespaces": {"max_user_namespaces": None, "readable": False},
    "userns_sandbox": {"installed": False, "usable": False, "path": None, "reason": "disabled"},
    "rename_exchange": {"ok": False, "errno": 22, "error": "benchmark", "fstype": "tmpfs"},
    "easybuild": {
        "found": True,
        "framework_version": [5, 4, 2],
        "stage_path_keys": True,
        "keys_source": "benchmark",
        "version_text": "5.4.2",
        "reason": None,
    },
    "filesystem": {"fstype": "tmpfs"},
    "flock": {"cross_node": "unmeasured", "method": "flock"},
}


def _lua_module(root, name="demo", version="1.0", loads=()):
    lines = [f'local root = "{root}"', f'conflict("{name}")']
    for dep in loads:
        lines.append(f'load("{dep}")')
    lines.append(f'setenv("EBROOT{name.upper()}", root)')
    lines.append(f'setenv("EBVERSION{name.upper()}", "{version}")')
    lines.append("")
    return "\n".join(lines)


class ModuleTreeSuite:
    """Scanning and dependency-ordering a module tree."""

    def setup(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.modules = os.path.join(self.tmp.name, "modules")
        os.makedirs(os.path.join(self.modules, "all"), exist_ok=True)
        os.makedirs(os.path.join(self.modules, "tools"), exist_ok=True)
        previous = None
        for idx in range(80):
            pkg = f"Pkg{idx:03d}"
            ver = "1.0-GCCcore-15.2.0"
            loads = (previous,) if previous is not None else ()
            target = os.path.join(self.modules, "all", pkg, f"{ver}.lua")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(_lua_module(f"/opt/site/software/{pkg}/{ver}", name=pkg, version=ver, loads=loads))
            link = os.path.join(self.modules, "tools", pkg, f"{ver}.lua")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(target, link)
            previous = f"{pkg}/{ver}"
        self.files, _ = walk_module_tree(self.modules)

    def teardown(self):
        self.tmp.cleanup()

    def time_walk_module_tree(self):
        walk_module_tree(self.modules)

    def time_order_module_files(self):
        order_module_files(self.files, self.modules)


class LogParsingSuite:
    """Parsing EasyBuild 5.4.2 wrapped and ANSI-colored dry-run logs."""

    def setup(self):
        dry_lines = [
            "== Temporary log file in case of crash /tmp/eb-x1zw5cqf/easybuild-4u5bzs64.log",
            "== found valid index for \x1b[35m/opt/easybuild/easybuild/\x1b[0m\x1b[95measyconfigs\x1b[0m, so using it\x1b[33m...\x1b[0m",
            "Dry run: printing build status of easyconfigs and dependencies",
        ]
        ext_lines = ["== resolving dependencies \x1b[33m...\x1b[0m", ""]
        for idx in range(32):
            mark = "x" if idx % 3 == 0 else "R" if idx % 3 == 1 else " "
            pkg = f"Pkg{idx:02d}"
            ver = "1.0-GCCcore-15.2.0"
            dry_lines.extend(
                [
                    f" * \x1b[1m[\x1b[0m{mark}\x1b[1m]\x1b[0m \x1b[35m/opt/easybuild/easybuild/easyconfigs/p/{pkg}/\x1b[0m\x1b[95m{pkg}-{ver}.eb\x1b[0m \x1b[1m(\x1b[0mmodule: ",
                    f"{pkg}/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[1m)\x1b[0m",
                ]
            )
            ext_lines.extend(
                [
                    "== processing EasyBuild easyconfig ",
                    f"\x1b[35m/opt/easybuild/easybuild/easyconfigs/p/{pkg}/\x1b[0m\x1b[95m{pkg}-{ver}.eb\x1b[0m",
                    "",
                    "*** DRY RUN using \x1b[32m'ConfigureMake'\x1b[0m easyblock ***",
                    f"== building and installing {pkg}/\x1b[1;36m1.0\x1b[0m-GCCcore-\x1b[1;36m15.2\x1b[0m.\x1b[1;36m0\x1b[0m\x1b[33m...\x1b[0m",
                    f"  >> installation prefix: \x1b[35m/tmp/eb-stage/__ROOT__/opt/site/software/{pkg}/\x1b[0m\x1b[95m{ver}\x1b[0m",
                    "fetching files and verifying checksums\x1b[33m...\x1b[0m \x1b[1m[\x1b[0mDRY RUN\x1b[1m]\x1b[0m",
                ]
            )
        dry_lines.append("== Temporary directory \x1b[35m/tmp/\x1b[0m\x1b[95meb-x1zw5cqf\x1b[0m has been removed.")
        ext_lines.append("== Build succeeded for \x1b[1;36m32\x1b[0m out of \x1b[1;36m32\x1b[0m")
        self.dry_run_text = "\n".join(dry_lines) + "\n"
        self.extended_text = "\n".join(ext_lines) + "\n"

    def time_parse_dry_run_logs(self):
        software_rels_from_dry_run(self.dry_run_text)

    def time_parse_extended_dry_run_logs(self):
        modules_built_by_extended_dry_run(self.extended_text)


class PrefixRewriteSuite:
    """Rewriting staged install prefixes and module files before swap."""

    def setup(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stage_dir = os.path.join(self.tmp.name, "staged")
        self.prefix_a = "/opt/site/.eb-staged-rebuild/generations/run-aaaa"
        self.prefix_b = "/opt/site/.eb-staged-rebuild/generations/run-bbbb"
        self.old = self.prefix_a
        self.new = self.prefix_b
        os.makedirs(os.path.join(self.stage_dir, "lib", "pkgconfig"), exist_ok=True)
        os.makedirs(os.path.join(self.stage_dir, "bin"), exist_ok=True)
        for idx in range(15):
            pc = os.path.join(self.stage_dir, "lib", "pkgconfig", f"mod{idx:02d}.pc")
            with open(pc, "w", encoding="utf-8") as handle:
                handle.write(f"prefix={self.prefix_a}/software/Demo/1.0\nlibdir=${{prefix}}/lib\n")
            script = os.path.join(self.stage_dir, "bin", f"tool{idx:02d}")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(f"#!{self.prefix_a}/software/Demo/1.0/bin/python\nprint('ok')\n")
            os.chmod(script, 0o755)
        for idx in range(5):
            os.symlink(
                f"{self.prefix_a}/software/Demo/1.0/bin/tool{idx:02d}",
                os.path.join(self.stage_dir, "bin", f"alias{idx:02d}"),
            )
        with open(os.path.join(self.stage_dir, ".eb-staged-rebuild-complete"), "w", encoding="utf-8") as handle:
            handle.write('{"run_id": "bench"}\n')
        self.module_text = _lua_module(f"{self.prefix_a}/software/Demo/1.0", name="Demo", version="1.0")

    def teardown(self):
        self.tmp.cleanup()

    def time_rewrite_prefix_and_module(self):
        rewrite_tree(self.stage_dir, self.old.encode("utf-8"), self.new.encode("utf-8"))
        self.module_text = rewrite_root(self.module_text, f"{self.new}/software/Demo/1.0")
        self.old, self.new = self.new, self.old


class StagedPlanSuite:
    """Snapshotting, planning, atomically swapping and rolling back a rebuild."""

    def setup(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.software = os.path.join(self.root, "software")
        self.modules = os.path.join(self.root, "modules")
        self.state_root = os.path.join(self.root, "state")
        os.makedirs(self.software, exist_ok=True)
        os.makedirs(self.modules, exist_ok=True)
        silent = FaultInjector()
        self.packages = []
        for idx, name in enumerate(("Alpha", "Beta", "Gamma")):
            rel = f"{name}/1.0"
            mod_rel = f"all/{name}/1.0.lua"
            install = os.path.join(self.software, rel)
            os.makedirs(os.path.join(install, "easybuild"), exist_ok=True)
            atomic_write(os.path.join(install, "PAYLOAD"), f"v0-{idx}\n", silent, "bench.payload")
            atomic_write(
                os.path.join(install, "easybuild", f"{name}-1.0.eb"),
                f"name = '{name}'\n",
                silent,
                "bench.eb",
            )
            mod_path = os.path.join(self.modules, mod_rel)
            atomic_write(mod_path, _lua_module(abspath(install), name=name, version="1.0"), silent, "bench.mod")
            self.packages.append(
                {
                    "rel": rel,
                    "payload": f"v1-{idx}\n",
                    "module_rel": mod_rel,
                    "name": name,
                    "version": "1.0",
                }
            )
        self.counter = itertools.count(1)

        def build(ctx):
            local_silent = FaultInjector()
            for pkg in self.packages:
                gen = os.path.join(ctx.generation_root, "software", pkg["rel"])
                os.makedirs(os.path.join(gen, "easybuild"), exist_ok=True)
                atomic_write(os.path.join(gen, "PAYLOAD"), pkg["payload"], local_silent, "bench.payload")
                atomic_write(
                    os.path.join(gen, "easybuild", f"{pkg['name']}-1.0.eb"),
                    f"name = '{pkg['name']}'\n",
                    local_silent,
                    "bench.eb",
                )
                mod = os.path.join(ctx.generation_root, "modules", pkg["module_rel"])
                atomic_write(
                    mod,
                    _lua_module(abspath(gen), name=pkg["name"], version=pkg["version"]),
                    local_silent,
                    "bench.mod",
                )
            return 0

        self.builder = build

    def teardown(self):
        self.tmp.cleanup()

    def time_staged_run_and_rollback(self):
        run_id = f"bench-{next(self.counter)}"
        snapshot(os.path.join(self.software, "Alpha", "1.0"))
        spec = Spec(
            software=self.software,
            modules=self.modules,
            state_root=self.state_root,
            eb_args=["Alpha-1.0.eb"],
            backend="installpath",
            module_rels=[pkg["rel"] for pkg in self.packages],
            builder=self.builder,
            probes=json.loads(json.dumps(_PROBES)),
            run_id=run_id,
        )
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            res = run(spec)
            if res.returncode != 0:
                raise RuntimeError(res.message)
            back = rollback(self.state_root, run_id)
            if back.returncode != 0:
                raise RuntimeError(back.message)
        shutil.rmtree(os.path.join(self.state_root, "generations", run_id), ignore_errors=True)
        shutil.rmtree(os.path.join(self.state_root, "runs", run_id), ignore_errors=True)
        for pkg in self.packages:
            retired = os.path.join(self.software, pkg["rel"].split("/")[0], f".1.0.{run_id}")
            shutil.rmtree(retired, ignore_errors=True)
