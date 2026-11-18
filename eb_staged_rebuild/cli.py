"""Command line for eb-staged-rebuild."""

import argparse
import json
import math
import os
import sys

from eb_staged_rebuild import __version__
from eb_staged_rebuild.constants import DEFAULT_GRACE_HOURS
from eb_staged_rebuild.engine import Spec, describe_module, resume, rollback, run, verify
from eb_staged_rebuild.errors import NeedsIntervention, Refused, StagedRebuildError
from eb_staged_rebuild.gc import collect
from eb_staged_rebuild.generations import format_generations, generations
from eb_staged_rebuild.journal import list_journals
from eb_staged_rebuild.retire import retire
from eb_staged_rebuild.probe import probe_all


def _software(value):
    return value or os.environ.get("EASYBUILD_INSTALLPATH_SOFTWARE")


def _modules(value):
    return value or os.environ.get("EASYBUILD_INSTALLPATH_MODULES")


def _state_root(value, software):
    if value:
        return value
    env = os.environ.get("EB_STAGED_REBUILD_STATE_ROOT")
    if env:
        return env
    if not software:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(software)), ".eb-staged-rebuild")


def _is_hours(value):
    return math.isfinite(value) and value >= 0


def _hours(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {text!r}") from None
    if not _is_hours(value):
        raise argparse.ArgumentTypeError(f"not a number of hours, 0 or more: {text!r}")
    return value


def _lock_timeout(text):
    """A finite lock timeout. nan and infinity are rejected."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("--lock-timeout is not a finite number")
    return value


def _bytes(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer value: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"not a number of bytes, 0 or more: {text!r}")
    return value


def _grace(value):
    if value is not None:
        return value
    text = os.environ.get("EB_STAGED_REBUILD_GRACE_HOURS", str(DEFAULT_GRACE_HOURS))
    try:
        hours = float(text)
    except ValueError:
        raise Refused(f"EB_STAGED_REBUILD_GRACE_HOURS is not a number: {text!r}") from None
    if not _is_hours(hours):
        raise Refused(f"EB_STAGED_REBUILD_GRACE_HOURS is not a number of hours, 0 or more: {text!r}")
    return hours


def _min_free_bytes(value):
    if value is not None:
        return value
    text = os.environ.get("EB_STAGED_REBUILD_MIN_FREE_BYTES")
    if text is None:
        return None
    try:
        minimum = int(text)
    except ValueError:
        raise Refused(f"EB_STAGED_REBUILD_MIN_FREE_BYTES is not an integer: {text!r}") from None
    if minimum < 0 or minimum > (1 << 64) - 1:
        raise Refused(
            "EB_STAGED_REBUILD_MIN_FREE_BYTES is not a number of bytes, "
            f"0 through {(1 << 64) - 1}: {text!r}"
        )
    return minimum


def _add_state_root(parser):
    default = os.environ.get("EB_STAGED_REBUILD_STATE_ROOT") or None
    parser.add_argument("--state-root", default=default, required=default is None)


def _add_common(parser):
    parser.add_argument("--software", help="live EASYBUILD_INSTALLPATH_SOFTWARE")
    parser.add_argument("--modules", help="live EASYBUILD_INSTALLPATH_MODULES")
    parser.add_argument("--state-root", help="journal and stage root (default: <parent of software>/.eb-staged-rebuild)")
    parser.add_argument("--eb", default=os.environ.get("EB_STAGED_REBUILD_EB", "eb"), help="EasyBuild executable")


def _parser():
    parser = argparse.ArgumentParser(
        prog="eb-staged-rebuild",
        description=(
            "Build EasyBuild software in a staged tree, then exchange the complete "
            "tree with the stable live directory."
        ),
    )
    parser.add_argument("--version", action="version", version=f"eb-staged-rebuild {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="report bwrap, user namespaces, RENAME_EXCHANGE, and EasyBuild")
    probe.add_argument("--path", help="directory on the filesystem to probe (default: .)")
    probe.add_argument(
        "--no-create",
        action="store_true",
        help="refuse when --path does not exist instead of creating it for the probe",
    )
    probe.add_argument("--eb", default=os.environ.get("EB_STAGED_REBUILD_EB", "eb"))
    probe.add_argument(
        "--require-eb",
        action="store_true",
        help="refuse when the EasyBuild command is not available",
    )
    probe.add_argument("--json", action="store_true")

    run_p = sub.add_parser("run", help="build and atomically swap a staged tree")
    _add_common(run_p)
    run_p.add_argument(
        "--backend",
        choices=("auto", "bwrap", "userns", "installpath"),
        default="auto",
        help=(
            "build isolation: auto tries bwrap, then a read-only user namespace, "
            "then the unsandboxed install-path fallback"
        ),
    )
    run_p.add_argument("--grace-hours", type=_hours, default=None)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--module", action="append", default=[], dest="module_rels", metavar="NAME/VERSION")
    run_p.add_argument("--run-id", default=None)
    run_p.add_argument("--lock-timeout", type=_lock_timeout, default=None)
    run_p.add_argument("--skip-live-check", action="store_true")
    run_p.add_argument(
        "--prepare",
        help="executable called with a directory-swap context JSON before the live directory is exchanged",
    )
    run_p.add_argument(
        "--ledger-config",
        help="JSON eb-stack ledger configuration; started records precede the directory swap",
    )
    run_p.add_argument(
        "--build-dir",
        default=None,
        metavar="DIR",
        help="scratch build directory passed to eb as --buildpath",
    )
    run_p.add_argument(
        "--min-free-bytes",
        type=_bytes,
        default=None,
        metavar="BYTES",
        help="refuse before the build unless the generation filesystem has at least BYTES available",
    )
    run_p.add_argument(
        "--require-exchange",
        action="store_true",
        help="refuse unless publication can use RENAME_EXCHANGE",
    )
    run_p.add_argument(
        "--verify", action="store_true", help="check the stage before the swap, and do not publish one that fails"
    )
    run_p.add_argument(
        "--force", action="store_true", help="with --verify, publish a stage that fails; the journal records it"
    )
    run_p.add_argument("eb_args", nargs=argparse.REMAINDER, help="easyconfig and eb options, after --")

    resume_p = sub.add_parser("resume", help="continue a run after a crash")
    _add_state_root(resume_p)
    resume_p.add_argument("--lock-timeout", type=_lock_timeout, default=None)
    resume_p.add_argument("--verify", action="store_true", help="check the stage before the swap, as run --verify does")
    resume_p.add_argument(
        "--force", action="store_true", help="with --verify, publish a stage that fails; the journal records it"
    )
    resume_p.add_argument("run_id")

    verify_p = sub.add_parser("verify", help="check a built run's stage before it is published")
    _add_state_root(verify_p)
    verify_p.add_argument("--lock-timeout", type=_lock_timeout, default=None)
    verify_p.add_argument("run_id", metavar="STAGE", help="the run id that names the stage")

    back_p = sub.add_parser("rollback", help="restore the previous module files, or put a retired module file back")
    _add_state_root(back_p)
    back_p.add_argument("--lock-timeout", type=_lock_timeout, default=None)
    back_p.add_argument("run_id")

    gc_p = sub.add_parser("gc", help="remove hidden retired siblings after the grace period")
    _add_state_root(gc_p)
    gc_p.add_argument("--grace-hours", type=_hours, default=None)
    gc_p.add_argument("--dry-run", action="store_true")
    gc_p.add_argument("--lock-timeout", type=_lock_timeout, default=None)
    gc_p.add_argument("--usage-log", default=None, help="usage log checked with eb-keep-logs used --require-none")
    gc_p.add_argument("--eb-keep-logs", default="eb-keep-logs", help="eb-keep-logs executable")
    gc_p.add_argument(
        "--metrics-file",
        metavar="PATH",
        help="atomically write Prometheus metrics after garbage collection",
    )
    gc_p.add_argument(
        "--include-failed",
        action="store_true",
        help="also remove the hidden trees and generation of a failed run that published nothing, after the grace period",
    )
    gc_p.add_argument("--json", action="store_true", help="write collection actions as JSON")

    retire_p = sub.add_parser(
        "retire",
        help="move a module file out of the module tree after its package is published elsewhere",
    )
    _add_state_root(retire_p)
    retire_p.add_argument("--module-file", required=True, help="module file to retire")
    retire_p.add_argument("--reason", default=None)
    retire_p.add_argument("--dry-run", action="store_true")
    retire_p.add_argument("--usage-log", default=None, help="usage log checked with eb-keep-logs used --require-none")
    retire_p.add_argument("--eb-keep-logs", default="eb-keep-logs", help="eb-keep-logs executable")
    retire_p.add_argument("--lock-timeout", type=_lock_timeout, default=0.0, help="seconds to wait for the lock (default: refuse at once)")

    status_p = sub.add_parser("status", help="show run journals")
    _add_state_root(status_p)
    status_p.add_argument("--json", action="store_true", help="print one JSON document")
    status_p.add_argument("run_id", nargs="?")

    list_p = sub.add_parser("list", help="show each install path's live tree and the generations kept beside it")
    _add_state_root(list_p)

    resolve_p = sub.add_parser("resolve", help="print the install path a module file names")
    resolve_p.add_argument("module")
    return parser


def _eb_args(raw):
    args = list(raw)
    if args and args[0] == "--":
        args = args[1:]
    if not any(not arg.startswith("-") for arg in args):
        raise Refused("pass the easyconfig after --, for example: eb-staged-rebuild run --software S --modules M -- file.eb")
    return args


def main(argv=None):
    """Run the ``eb-staged-rebuild`` command line and return its exit status.

    A refusal, an intervention or an ``OSError`` is printed as ``eb-staged-rebuild: MESSAGE``
    and gives exit status 2; otherwise the status is the command's.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="surrogateescape")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="surrogateescape")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        code = _dispatch(args)
    except (Refused, NeedsIntervention, StagedRebuildError) as exc:
        print(f"eb-staged-rebuild: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"eb-staged-rebuild: {exc}", file=sys.stderr)
        return 2
    return code


def _dispatch(args):
    if args.command == "probe":
        directory = args.path or os.getcwd()
        if args.no_create and not os.path.exists(directory):
            print(f"probe path does not exist: {directory}", file=sys.stderr)
            return 1
        report = probe_all(directory, args.eb)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_probe(report)
        if args.require_eb and not report["easybuild"].get("found"):
            print(f"probe requires EasyBuild, but it was not found: {args.eb}", file=sys.stderr)
            return 1
        return 0
    if args.command == "resolve":
        described = describe_module(args.module)
        print(json.dumps(described, indent=2, sort_keys=True))
        if described.get("kind") != "file" or not described.get("complete"):
            return 1
        return 0
    if args.command == "run":
        software = _software(args.software)
        modules = _modules(args.modules)
        if not software or not modules:
            raise Refused("--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required")
        spec = Spec(
            software=software,
            modules=modules,
            state_root=_state_root(args.state_root, software),
            eb_args=_eb_args(args.eb_args),
            backend=args.backend,
            grace_hours=_grace(args.grace_hours),
            dry_run=args.dry_run,
            module_rels=list(args.module_rels),
            eb_executable=args.eb,
            run_id=args.run_id,
            skip_live_check=args.skip_live_check,
            lock_timeout=args.lock_timeout,
            prepare_executable=args.prepare,
            ledger_config=args.ledger_config,
            build_dir=args.build_dir,
            min_free_bytes=_min_free_bytes(args.min_free_bytes),
            require_exchange=args.require_exchange,
            verify=args.verify,
            force=args.force,
        )
        if args.prepare is not None and not args.prepare:
            raise Refused("--prepare must name a program; it is empty")
        if args.build_dir is not None and not args.build_dir:
            raise Refused("--build-dir must name a directory; it is empty")
        if args.force and not args.verify:
            raise Refused("--force needs --verify; without it, nothing checks the stage before the swap")
        result = run(spec)
        return result.returncode
    if args.command == "resume":
        result = resume(
            args.state_root, args.run_id, lock_timeout=args.lock_timeout, verify=args.verify, force=args.force
        )
        return result.returncode
    if args.command == "verify":
        return verify(args.state_root, args.run_id, lock_timeout=args.lock_timeout).returncode
    if args.command == "rollback":
        result = rollback(args.state_root, args.run_id, lock_timeout=args.lock_timeout)
        return result.returncode
    if args.command == "gc":
        actions = collect(
            args.state_root,
            _grace(args.grace_hours),
            __import__("time").time(),
            dry_run=args.dry_run,
            include_failed=args.include_failed,
            lock_timeout=args.lock_timeout,
            run_grace=args.grace_hours is None,
            usage_log=args.usage_log,
            eb_keep_logs=args.eb_keep_logs,
            metrics_file=args.metrics_file,
        )
        if args.json:
            document = {
                "actions": [
                    {
                        "action": action["action"],
                        "path": action["path"],
                        "reason": action["reason"],
                    }
                    for action in actions
                ]
            }
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            for action in actions:
                print(f"{action['action']}: {action['path']} ({action['reason']})")
        return 0
    if args.command == "retire":
        result = retire(
            args.state_root,
            args.module_file,
            reason=args.reason,
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
            usage_log=args.usage_log,
            eb_keep_logs=args.eb_keep_logs,
        )
        return result.returncode
    if args.command == "status":
        journals = list_journals(args.state_root)
        if args.run_id:
            journals = [journal for journal in journals if journal.get("run_id") == args.run_id]
            if not journals:
                raise Refused(f"no journal for {args.run_id}")
        if args.json:
            print(json.dumps({"runs": [_status_view(journal) for journal in journals]}, indent=2, sort_keys=True))
            return 0
        if not journals:
            print("no runs")
            return 0
        for journal in journals:
            if journal.get("kind") == "retire":
                print(f"{journal.get('run_id')}  {journal.get('state')}  retire  {journal.get('module_path')}")
                print(f"  software directory {journal.get('software_dir')}")
                continue
            modules = journal.get("modules_plan") or []
            published = sum(1 for mod in modules if mod.get("status") == "published")
            print(
                f"{journal.get('run_id')}  {journal.get('state')}  "
                f"backend={journal.get('backend')}  modules={published}/{len(modules)}"
            )
            if journal.get("message"):
                print(f"  {journal['message']}")
            for mod in modules:
                print(
                    f"  {mod.get('status'):<10} {mod.get('rel')}  "
                    f"live={mod.get('live_software')} retired={mod.get('retired')}"
                )
        return 0
    if args.command == "list":
        for line in format_generations(generations(list_journals(args.state_root))):
            print(line)
        return 0
    raise Refused(f"unknown command {args.command}")


def _status_view(journal):
    """The fields ``status`` prints, as one object of the JSON document.

    A retire record keeps the run, its state, the module file and the software
    directory it left in place. Any other journal keeps the backend, the
    message, and one object per planned module. A missing kind is ``rebuild``.
    """
    if journal.get("kind") == "retire":
        return {
            "kind": "retire",
            "module_path": journal.get("module_path"),
            "run_id": journal.get("run_id"),
            "software_dir": journal.get("software_dir"),
            "state": journal.get("state"),
        }
    modules = journal.get("modules_plan") or []
    published = sum(1 for mod in modules if mod.get("status") == "published")
    kind = journal.get("kind") or "rebuild"
    return {
        "backend": journal.get("backend"),
        "kind": kind,
        "message": journal.get("message"),
        "modules": [
            {
                "live_software": mod.get("live_software"),
                "rel": mod.get("rel"),
                "retired": mod.get("retired"),
                "status": mod.get("status"),
            }
            for mod in modules
        ],
        "modules_published": published,
        "modules_total": len(modules),
        "run_id": journal.get("run_id"),
        "state": journal.get("state"),
    }


def _print_probe(report):
    bwrap = report["bwrap"]
    namespaces = report["user_namespaces"]
    userns = report["userns_sandbox"]
    exchange = report["rename_exchange"]
    easybuild = report["easybuild"]
    filesystem = report.get("filesystem") or {}
    print(f"bwrap: {'usable' if bwrap.get('usable') else 'not usable'}")
    if bwrap.get("version"):
        print(f"  version: {bwrap['version']}")
    if bwrap.get("reason"):
        print(f"  reason: {bwrap['reason']}")
    print(f"user.max_user_namespaces: {namespaces.get('max_user_namespaces')}")
    if namespaces.get("unprivileged_userns_clone") is not None:
        print(f"kernel.unprivileged_userns_clone: {namespaces['unprivileged_userns_clone']}")
    print(f"user-namespace sandbox: {'usable' if userns.get('usable') else 'not usable'}")
    if userns.get("reason"):
        print(f"  reason: {userns['reason']}")
    fstype = exchange.get("fstype") or filesystem.get("fstype")
    print(
        "RENAME_EXCHANGE: "
        + ("yes" if exchange.get("ok") else "no")
        + (f" on {fstype}" if fstype else "")
    )
    if exchange.get("error"):
        print(f"  {exchange['error']}")
    if easybuild.get("found"):
        version = easybuild.get("framework_version")
        rendered = ".".join(str(part) for part in version) if version else "unknown"
        print(f"EasyBuild framework: {rendered}")
        print(f"  bwrap stage-path keys: {easybuild.get('stage_path_keys')} ({easybuild.get('keys_source')})")
    else:
        print(f"EasyBuild: {easybuild.get('reason')}")
    print(f"filesystem: {filesystem.get('fstype')} {filesystem.get('mount') or ''}".rstrip())
    print("lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured")


if __name__ == "__main__":
    raise SystemExit(main())
