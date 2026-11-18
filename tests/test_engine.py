"""End-to-end transaction tests, including every recorded fault point."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from eb_staged_rebuild import cli
from eb_staged_rebuild.engine import Result, Spec, resume, rollback, run
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.journal import journal_path, load_journal
from eb_staged_rebuild.modulefile import parse_root
from eb_staged_rebuild.paths import lstat_kind
from eb_staged_rebuild.swap import FALLBACK_HOLD_SUFFIX
from tests.support import PROBES, classic_install, installpath_builder


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        self.now += 10
        return self.now


def _fixture(root, *, two=False):
    software = os.path.join(root, "software")
    modules = os.path.join(root, "modules")
    state = os.path.join(root, "state")
    os.makedirs(software)
    os.makedirs(modules)
    packages = []
    installed = {}
    rows = (
        ("lib", "lib-v0\n", "lib-v1\n"),
        ("app", "app-v0\n", "app-v1\n"),
    ) if two else (("demo", "v0\n", "v1\n"),)
    for name, old, new in rows:
        rel = f"{name}/1"
        module_rel = f"all/{name}/1.lua"
        install, module, module_text = classic_install(
            software,
            modules,
            rel,
            old,
            module_rel,
            name=name,
            loads=("lib/1",) if name == "app" else (),
        )
        installed[rel] = {
            "install": install,
            "module": module,
            "module_text": module_text,
            "inode": os.lstat(install).st_ino,
            "old": old,
            "new": new,
        }
        packages.append(
            {
                "rel": rel,
                "payload": new,
                "module_rel": module_rel,
                "name": name,
                "loads": ("lib/1",) if name == "app" else (),
            }
        )
    return software, modules, state, installed, installpath_builder(packages)


def _spec(
    software,
    modules,
    state,
    builder,
    *,
    run_id="run1",
    faults=None,
    probes=None,
    clock=None,
):
    rels = ["lib/1", "app/1"] if os.path.isdir(os.path.join(software, "lib")) else ["demo/1"]
    return Spec(
        software=software,
        modules=modules,
        state_root=state,
        eb_args=["demo-1.eb"],
        backend="installpath",
        module_rels=rels,
        builder=builder,
        faults=faults or FaultInjector(),
        run_id=run_id,
        probes=probes or PROBES,
        clock=clock,
    )


def _payload(path):
    with open(os.path.join(path, "PAYLOAD"), encoding="utf-8") as handle:
        return handle.read()


def _main(argv, env=None):
    """Run the command line with the variables of the grace period and the state root unset unless ``env`` sets them."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        os.environ.pop("EB_STAGED_REBUILD_GRACE_HOURS", None)
        os.environ.pop("EB_STAGED_REBUILD_STATE_ROOT", None)
        os.environ.update(env or {})
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class FreeSpaceTests(unittest.TestCase):
    def test_default_command_output_and_status_stay_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch(
                    "eb_staged_rebuild.engine.os.statvfs",
                    side_effect=AssertionError("default run checked free space"),
                ),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                result = run(_spec(software, modules, state, builder))
            expected = (
                f"eb-staged-rebuild: run run1 backend installpath generation "
                f"{state}/generations/run1\n"
                "eb-staged-rebuild: swapped all/demo/1.lua (rename-fallback)\n"
                "eb-staged-rebuild: published module all/demo/1.lua\n"
                "eb-staged-rebuild: run run1 published 1 module(s)\n"
            ).encode()
        self.assertEqual(
            (result.returncode, out.getvalue().encode(), err.getvalue().encode()),
            (0, expected, b""),
        )

    def test_command_leaves_the_guard_disabled_when_the_option_is_absent(self):
        result = Result(0)
        with mock.patch("eb_staged_rebuild.cli.run", return_value=result) as called:
            code, out, err = _main(
                ["run", "--software", "software", "--modules", "modules", "--", "demo-1.eb"]
            )
        self.assertEqual((code, out.encode(), err.encode()), (0, b"", b""))
        self.assertIsNone(called.call_args.args[0].min_free_bytes)

    def test_minimum_free_space_refuses_before_the_journal_and_build(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, _builder = _fixture(root)
            builder = mock.Mock(return_value=0)
            spec = _spec(software, modules, state, builder)
            spec.min_free_bytes = 4096
            space = mock.Mock(f_bavail=3, f_frsize=1024)
            with mock.patch("eb_staged_rebuild.engine.os.statvfs", return_value=space):
                with self.assertRaisesRegex(
                    Refused,
                    r"generation filesystem has 3072 bytes available; "
                    r"--min-free-bytes requires 4096",
                ):
                    run(spec)
            builder.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(state, "runs")))


class TransactionTests(unittest.TestCase):
    def assert_old_live(self, installed):
        for row in installed.values():
            self.assertEqual(lstat_kind(row["install"]), "dir")
            self.assertEqual(os.lstat(row["install"]).st_ino, row["inode"])
            self.assertEqual(_payload(row["install"]), row["old"])
            with open(row["module"], encoding="utf-8") as handle:
                self.assertEqual(handle.read(), row["module_text"])
            parent = os.path.dirname(row["install"])
            for name in os.listdir(parent):
                if name.startswith(f".{os.path.basename(row['install'])}."):
                    self.assertFalse(
                        os.path.lexists(os.path.join(parent, name) + FALLBACK_HOLD_SUFFIX)
                    )

    def assert_new_live(self, installed):
        for row in installed.values():
            self.assertEqual(lstat_kind(row["install"]), "dir")
            self.assertNotEqual(os.lstat(row["install"]).st_ino, row["inode"])
            self.assertEqual(_payload(row["install"]), row["new"])
            with open(row["module"], encoding="utf-8") as handle:
                module_text = handle.read()
            self.assertEqual(parse_root(module_text)[1], row["install"])

    def test_success_uses_fallback_and_keeps_old_tree_for_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            result = run(_spec(software, modules, state, builder))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_new_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            mod = journal["modules_plan"][0]
            self.assertEqual(mod["swap_method"], "rename-fallback")
            self.assertEqual(_payload(mod["retired"]), "v0\n")

    def test_success_uses_rename_exchange_when_probe_succeeds(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            probes = json.loads(json.dumps(PROBES))
            probes["rename_exchange"]["ok"] = True
            result = run(_spec(software, modules, state, builder, probes=probes))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_new_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            self.assertEqual(
                journal["modules_plan"][0]["swap_method"],
                "renameat2-exchange",
            )

    def test_every_recorded_failure_restores_old_tree_then_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            recording = _spec(
                software,
                modules,
                state,
                builder,
                run_id="record",
            )
            self.assertEqual(run(recording).returncode, 0)
            steps = list(recording.faults.reached)
            self.assertIn("swap:all/demo/1.lua.fallback.staged-to-live", steps)
            self.assertIn("journal.swap:all/demo/1.lua.rename", steps)
            self.assertIn("journal.complete.rename", steps)

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root)
                    spec = _spec(
                        software,
                        modules,
                        state,
                        builder,
                        faults=FaultInjector(step),
                    )
                    with self.assertRaises(InjectedCrash):
                        run(spec)
                    self.assert_old_live(installed)
                    path = journal_path(state, "run1")
                    if os.path.isfile(path):
                        result = resume(
                            state,
                            "run1",
                            builder=builder,
                            faults=FaultInjector(),
                        )
                        self.assertEqual(result.returncode, 0, result.message)
                        self.assert_new_live(installed)

    def test_failure_on_second_module_restores_both_modules(self):
        failures = (
            "swap:all/app/1.lua.fallback.staged-to-live",
            "journal.swap:all/app/1.lua.rename",
            "journal.module:all/app/1.lua.rename",
        )
        for step in failures:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root, two=True)
                    with self.assertRaises(InjectedCrash):
                        run(
                            _spec(
                                software,
                                modules,
                                state,
                                builder,
                                faults=FaultInjector(step),
                            )
                        )
                    self.assert_old_live(installed)

    def test_an_installed_tool_ignores_the_fail_hook(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"EB_STAGED_REBUILD_FAIL_AT": "build.start"}
        ):
            software, modules, state, installed, builder = _fixture(root)
            result = run(_spec(software, modules, state, builder))
            self.assertEqual(result.returncode, 0, result.message)
            self.assert_new_live(installed)

    def test_the_fail_hook_fails_each_step_it_names(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            FaultInjector, "failing", frozenset({"build.start"})
        ):
            software, modules, state, installed, builder = _fixture(root)
            with self.assertRaises(OSError) as caught:
                run(_spec(software, modules, state, builder))
            self.assertEqual(str(caught.exception), "[Errno 5] injected failure at build.start")
            self.assert_old_live(installed)

    def test_failed_build_never_touches_live_tree(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, _builder = _fixture(root)

            def fail(_context):
                return 7

            result = run(_spec(software, modules, state, fail))
            self.assertEqual(result.returncode, 7)
            self.assert_old_live(installed)

    def test_dry_run_creates_no_state_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            spec = _spec(software, modules, state, builder)
            spec.dry_run = True
            result = run(spec)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(os.path.isdir(os.path.join(state, "runs")))
            self.assert_old_live(installed)
            self.assertIn("installpath-software", result.command)

    def test_missing_rebuild_target_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state")
            os.makedirs(software)
            os.makedirs(modules)
            called = []

            def builder(_context):
                called.append(True)
                return 0

            with self.assertRaises(Refused):
                run(_spec(software, modules, state, builder))
            self.assertEqual(called, [])

    def test_symlink_rebuild_target_is_refused_before_build(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            row = installed["demo/1"]
            moved = row["install"] + ".real"
            os.rename(row["install"], moved)
            os.symlink(moved, row["install"])
            with self.assertRaises(Refused):
                run(_spec(software, modules, state, builder))
            self.assertEqual(_payload(moved), row["old"])

    def test_module_path_cannot_escape_software_root(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            spec = _spec(software, modules, state, builder)
            spec.module_rels = ["../outside/1"]
            with self.assertRaises(Refused) as caught:
                run(spec)
            self.assertIn("escapes", str(caught.exception))

    def test_a_module_that_is_no_directory_is_refused_for_what_it_is(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            open(os.path.join(software, "demo", "file"), "w").close()
            os.mkfifo(os.path.join(software, "demo", "fifo"))
            for rel, what in (("demo/2", "missing"), ("demo/file", "a file"), ("demo/fifo", "a special file")):
                with self.subTest(rel=rel):
                    spec = _spec(software, modules, state, builder)
                    spec.module_rels = [rel]
                    with self.assertRaises(Refused) as caught:
                        run(spec)
                    self.assertEqual(
                        str(caught.exception),
                        f"{os.path.join(software, rel)} is {what}, not an existing rebuild directory",
                    )
            self.assert_old_live(installed)

    def test_symlinked_state_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, _state, installed, builder = _fixture(root)
            real_state = os.path.join(root, "real-state")
            os.makedirs(real_state)
            state = os.path.join(root, "state-link")
            os.symlink(real_state, state)
            with self.assertRaises(Refused) as caught:
                run(_spec(software, modules, state, builder))
            self.assertIn("state root", str(caught.exception))
            self.assert_old_live(installed)


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_exact_old_directory(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            result = rollback(state, "run1")
            self.assertEqual(result.returncode, 0, result.message)
            TransactionTests().assert_old_live(installed)
            journal = load_journal(journal_path(state, "run1"))
            self.assertEqual(_payload(journal["modules_plan"][0]["retired"]), "v1\n")
            self.assertEqual(rollback(state, "run1").returncode, 0)

    def test_a_run_a_later_run_replaced_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            row = installed["demo/1"]
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            later = installpath_builder(
                [{"rel": "demo/1", "payload": "v2\n", "module_rel": "all/demo/1.lua", "name": "demo"}]
            )
            self.assertEqual(run(_spec(software, modules, state, later, run_id="run2")).returncode, 0)
            with open(row["module"], encoding="utf-8") as handle:
                module_text = handle.read()
            with self.assertRaisesRegex(Refused, "holds the tree of run run2; roll back run2 first"):
                rollback(state, "run1")
            self.assertEqual(_payload(row["install"]), "v2\n")
            with open(row["module"], encoding="utf-8") as handle:
                self.assertEqual(handle.read(), module_text)
            self.assertEqual(load_journal(journal_path(state, "run1"))["state"], "complete")
            self.assertEqual(rollback(state, "run2").returncode, 0)
            self.assertEqual(_payload(row["install"]), "v1\n")
            self.assertEqual(rollback(state, "run1").returncode, 0)
            TransactionTests().assert_old_live(installed)
            for run_id in ("run1", "run2"):
                self.assertEqual(load_journal(journal_path(state, run_id))["state"], "rolled_back")

    def test_a_live_tree_the_run_did_not_publish_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            row = installed["demo/1"]
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            os.unlink(os.path.join(row["install"], ".eb-staged-rebuild-complete"))
            with self.assertRaisesRegex(Refused, "is not the tree it published"):
                rollback(state, "run1")
            self.assertEqual(_payload(row["install"]), "v1\n")
            self.assertEqual(load_journal(journal_path(state, "run1"))["state"], "complete")

    def test_every_rollback_failure_restores_published_tree(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            recorder = FaultInjector()
            self.assertEqual(rollback(state, "run1", faults=recorder).returncode, 0)
            steps = list(recorder.reached)
            self.assertIn(
                "rollback.swap:all/demo/1.lua.fallback.staged-to-live",
                steps,
            )
            self.assertIn("journal.rolled_back.rename", steps)

        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    software, modules, state, installed, builder = _fixture(root)
                    self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
                    with self.assertRaises(InjectedCrash):
                        rollback(state, "run1", faults=FaultInjector(step))
                    TransactionTests().assert_new_live(installed)
                    self.assertEqual(rollback(state, "run1").returncode, 0)
                    TransactionTests().assert_old_live(installed)


class GarbageCollectionTests(unittest.TestCase):
    def test_gc_removes_only_the_hidden_retired_tree_after_grace(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            clock = Clock()
            self.assertEqual(
                run(_spec(software, modules, state, builder, clock=clock)).returncode,
                0,
            )
            journal = load_journal(journal_path(state, "run1"))
            retired = journal["modules_plan"][0]["retired"]
            grace_hours = 144
            early = collect(state, grace_hours, clock.now, dry_run=True)
            self.assertEqual(early[0]["action"], "keep")
            later = collect(
                state,
                grace_hours,
                clock.now + grace_hours * 3600 + 1,
                dry_run=False,
            )
            self.assertEqual(later[0]["action"], "remove")
            self.assertFalse(os.path.exists(retired))
            TransactionTests().assert_new_live(installed)

    def test_gc_removes_a_generation_once_its_hidden_trees_are_gone(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            clock = Clock()
            self.assertEqual(run(_spec(software, modules, state, builder, clock=clock)).returncode, 0)
            generation = os.path.join(state, "generations", "run1")
            self.assertTrue(os.path.isdir(generation))
            early = collect(state, 144, clock.now, dry_run=True)
            self.assertNotIn(generation, [action["path"] for action in early])
            later = clock.now + 144 * 3600 + 1
            removal = {"path": generation, "action": "remove", "reason": "generation of a complete run"}
            self.assertIn(removal, collect(state, 144, later, dry_run=True))
            self.assertTrue(os.path.isdir(generation))
            self.assertIn(removal, collect(state, 144, later))
            self.assertFalse(os.path.exists(generation))
            journal = load_journal(journal_path(state, "run1"))
            self.assertEqual(journal["generation_removed_at"], later)
            self.assertEqual(journal["message"], "retired tree and generation removed")
            self.assertTrue(os.path.isdir(os.path.join(state, "generations")))
            self.assertEqual(collect(state, 144, later), [])
            TransactionTests().assert_new_live(installed)

    def test_gc_removes_a_failed_generation_only_with_include_failed(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)

            def fail(context):
                builder(context)
                return 7

            clock = Clock()
            self.assertEqual(run(_spec(software, modules, state, fail, clock=clock)).returncode, 7)
            generation = os.path.join(state, "generations", "run1")
            self.assertTrue(os.path.isdir(generation))
            self.assertEqual(collect(state, 0, clock.now + 1), [])
            self.assertTrue(os.path.isdir(generation))
            self.assertEqual(
                collect(state, 0, clock.now + 1, include_failed=True),
                [{"path": generation, "action": "remove", "reason": "generation of a failed run"}],
            )
            self.assertFalse(os.path.exists(generation))
            self.assertEqual(load_journal(journal_path(state, "run1"))["message"], "generation removed")
            TransactionTests().assert_old_live(installed)
            self.assertEqual(resume(state, "run1", builder=builder, clock=clock).returncode, 0)
            self.assertTrue(os.path.isdir(generation))
            self.assertIsNone(load_journal(journal_path(state, "run1"))["generation_removed_at"])
            TransactionTests().assert_new_live(installed)

    def test_gc_refuses_a_generation_named_by_a_run_id_that_is_not_a_plain_name(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            clock = Clock()
            self.assertEqual(run(_spec(software, modules, state, builder, clock=clock)).returncode, 0)
            path = journal_path(state, "run1")
            journal = load_journal(path)
            journal["run_id"] = "../runs"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(journal, handle)
            with self.assertRaisesRegex(Refused, "run id is not a plain name"):
                collect(state, 0, clock.now + 1)
            self.assertTrue(os.path.isdir(os.path.join(state, "runs", "run1")))

    @staticmethod
    def _gc(state, *args, env=None):
        code, out, _ = _main(["gc", "--state-root", state, "--dry-run", *args], env)
        return code, out

    def _published(self, root, grace_hours):
        software, modules, state, installed, builder = _fixture(root)
        spec = _spec(software, modules, state, builder)
        spec.grace_hours = grace_hours
        self.assertEqual(run(spec).returncode, 0)
        return state, load_journal(journal_path(state, "run1"))["modules_plan"][0]["retired"]

    @staticmethod
    def _removal(state, retired):
        generation = os.path.join(state, "generations", "run1")
        return (
            0,
            f"remove: {retired} (previous live tree)\n"
            f"remove: {generation} (generation of a complete run)\n",
        )

    def test_gc_keeps_a_tree_for_the_grace_period_its_run_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            state, retired = self._published(root, 0)
            remove = self._removal(state, retired)
            keep = (0, f"keep: {retired} (within grace (previous live tree))\n")
            self.assertEqual(self._gc(state), remove)
            self.assertEqual(self._gc(state, "--grace-hours", "1"), keep)
            self.assertEqual(self._gc(state, env={"EB_STAGED_REBUILD_GRACE_HOURS": "1"}), remove)

    def test_a_longer_grace_period_of_the_run_outlasts_the_default(self):
        with tempfile.TemporaryDirectory() as root:
            state, retired = self._published(root, 2)
            keep = (0, f"keep: {retired} (within grace (previous live tree))\n")
            self.assertEqual(self._gc(state, env={"EB_STAGED_REBUILD_GRACE_HOURS": "0"}), keep)
            self.assertEqual(self._gc(state, "--grace-hours", "0"), self._removal(state, retired))

    def test_a_recorded_grace_period_that_is_not_hours_is_not_used(self):
        with tempfile.TemporaryDirectory() as root:
            state, retired = self._published(root, 0)
            path = journal_path(state, "run1")
            for value in (-5, float("nan"), "0", True, None):
                with self.subTest(value=value):
                    with open(path, encoding="utf-8") as handle:
                        journal = json.load(handle)
                    journal["grace_hours"] = value
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(journal, handle)
                    self.assertEqual(
                        self._gc(state),
                        (0, f"keep: {retired} (within grace (previous live tree))\n"),
                    )
                    self.assertEqual(
                        self._gc(state, env={"EB_STAGED_REBUILD_GRACE_HOURS": "0"}),
                        self._removal(state, retired),
                    )

    def test_gc_refuses_a_grace_period_below_0_or_not_finite(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = self._published(root, 0)
            for args, text in (
                (["--grace-hours", "-5"], "-5"),
                (["--grace-hours=-5"], "-5"),
                (["--grace-hours", "nan"], "nan"),
                (["--grace-hours", "inf"], "inf"),
                (["--grace-hours=-inf"], "-inf"),
            ):
                with self.subTest(args=args):
                    code, out, err = _main(["gc", "--state-root", state, "--dry-run", *args])
                    self.assertEqual(
                        (code, out, err.splitlines()[-1:]),
                        (2, "", [f"eb-staged-rebuild gc: error: argument --grace-hours: not a number of hours, 0 or more: '{text}'"]),
                    )

    def test_gc_refuses_an_environment_grace_period_below_0_or_not_finite(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = self._published(root, 0)
            for text in ("-5", "nan", "inf"):
                with self.subTest(text=text):
                    self.assertEqual(
                        _main(["gc", "--state-root", state, "--dry-run"], {"EB_STAGED_REBUILD_GRACE_HOURS": text}),
                        (2, "", f"eb-staged-rebuild: EB_STAGED_REBUILD_GRACE_HOURS is not a number of hours, 0 or more: '{text}'\n"),
                    )

    def test_run_refuses_a_grace_period_below_0_or_not_finite(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _, _ = _fixture(root)
            argv = ["run", "--software", software, "--modules", modules, "--state-root", state, "--dry-run"]
            code, out, err = _main([*argv, "--grace-hours", "-5", "--", "demo-1.eb"])
            self.assertEqual(
                (code, out, err.splitlines()[-1:]),
                (2, "", ["eb-staged-rebuild run: error: argument --grace-hours: not a number of hours, 0 or more: '-5'"]),
            )
            self.assertEqual(
                _main([*argv, "--", "demo-1.eb"], {"EB_STAGED_REBUILD_GRACE_HOURS": "nan"}),
                (2, "", "eb-staged-rebuild: EB_STAGED_REBUILD_GRACE_HOURS is not a number of hours, 0 or more: 'nan'\n"),
            )
            self.assertFalse(os.path.exists(state))

    def test_gc_refuses_an_environment_grace_period_that_is_not_a_number(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = self._published(root, 0)
            for text, shown in (("abc", "'abc'"), ("", "''"), ("5h", "'5h'"), ("it's", '"it\'s"')):
                with self.subTest(text=text):
                    self.assertEqual(
                        _main(["gc", "--state-root", state, "--dry-run"], {"EB_STAGED_REBUILD_GRACE_HOURS": text}),
                        (2, "", f"eb-staged-rebuild: EB_STAGED_REBUILD_GRACE_HOURS is not a number: {shown}\n"),
                    )

    def test_run_refuses_an_environment_grace_period_that_is_not_a_number(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _, _ = _fixture(root)
            argv = ["run", "--software", software, "--modules", modules, "--state-root", state, "--dry-run"]
            self.assertEqual(
                _main([*argv, "--", "demo-1.eb"], {"EB_STAGED_REBUILD_GRACE_HOURS": "abc"}),
                (2, "", "eb-staged-rebuild: EB_STAGED_REBUILD_GRACE_HOURS is not a number: 'abc'\n"),
            )
            self.assertFalse(os.path.exists(state))


class StateRootVariableTests(unittest.TestCase):
    def test_every_command_takes_the_state_root_from_the_environment(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, installed, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            env = {"EB_STAGED_REBUILD_STATE_ROOT": state}
            for argv in (
                ["status"],
                ["status", "run1"],
                ["retire", "--module-file", installed["demo/1"]["module"], "--dry-run"],
                ["gc", "--dry-run"],
                ["resume", "run1"],
            ):
                with self.subTest(argv=argv):
                    given = _main([argv[0], "--state-root", state, *argv[1:]])
                    self.assertEqual(given[0], 0, given)
                    self.assertEqual(_main(argv, env), given)
            self.assertEqual(_main(["rollback", "run1"], env)[0], 0)
            TransactionTests().assert_old_live(installed)

    def test_the_option_wins_over_the_environment(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _, builder = _fixture(root)
            self.assertEqual(run(_spec(software, modules, state, builder)).returncode, 0)
            elsewhere = os.path.join(root, "elsewhere")
            os.makedirs(elsewhere)
            env = {"EB_STAGED_REBUILD_STATE_ROOT": elsewhere}
            self.assertEqual(_main(["status"], env), (0, "no runs\n", ""))
            code, out, _ = _main(["status", "--state-root", state], env)
            self.assertEqual((code, out.splitlines()[0]), (0, "run1  complete  backend=installpath  modules=1/1"))

    def test_without_the_variable_the_option_is_required(self):
        for env in ({}, {"EB_STAGED_REBUILD_STATE_ROOT": ""}):
            with self.subTest(env=env):
                code, out, err = _main(["status"], env)
                self.assertEqual(
                    (code, out, err.splitlines()[-1:]),
                    (2, "", ["eb-staged-rebuild status: error: the following arguments are required: --state-root"]),
                )

    def test_with_the_variable_the_usage_shows_the_option_as_optional(self):
        code, out, err = _main(["rollback"], {"EB_STAGED_REBUILD_STATE_ROOT": "/nonexistent"})
        lines = err.splitlines()
        self.assertEqual(
            (code, out, lines[:1], lines[-1:]),
            (
                2,
                "",
                ["usage: eb-staged-rebuild rollback [-h] [--state-root STATE_ROOT]"],
                ["eb-staged-rebuild rollback: error: the following arguments are required: run_id"],
            ),
        )
if __name__ == "__main__":
    unittest.main()
