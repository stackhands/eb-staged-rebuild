#!/bin/bash
# The example site of the how-to guides. EasyBuild installs
# Example/1.0-GCCcore-15.2.0 under $SITE (default /opt/site), and each case
# runs the commands of one guide with eb-staged-rebuild. Before each command
# it prints a line "+ COMMAND"; a command that exits other than the guide
# says stops the script.
#
#   docs/examples/site.sh setup     the site and the first install
#   docs/examples/site.sh CASE      one case of the list in $cases below
#   docs/examples/site.sh all       setup, every case in that order, clean
#   docs/examples/site.sh clean     remove what setup made
#
# The cases change the site in turn, so run them in the order of $cases.
# They need eb-staged-rebuild on PATH, EasyBuild 5.4.2 as eb, Lmod as
# LMOD_CMD, bwrap, openssl, an eb-stack 0.7.0 binary as EB_STACK_BIN or on
# PATH, and a $SITE that exists and that you can write. The guides show
# the output of a run as alice on login1.
set -euo pipefail

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$here/steps.sh"
SITE=${SITE:-/opt/site}
EASYCONFIGS=$HOME/easyconfigs
STATE=$SITE/.eb-staged-rebuild
MODULE=$SITE/modules/all/Example/1.0-GCCcore-15.2.0.lua
EB_STACK=$SITE/bin/eb-stack
KEY=$SITE/keys/ledger.pem
PUB=$SITE/keys/ledger.pub
LOG=$SITE/ledger/stack.jsonl
HEAD=$SITE/ledger-heads/head.json

cases="probe dry-run rebuild prepare files fail resume rollback gc retire lock
robot userns build-dir no-easyconfig not-installed ledger-setup ledger-run
ledger-rollback verify"

export EASYBUILD_SOURCEPATH=$SITE/sources
export EASYBUILD_INSTALLPATH_SOFTWARE=$SITE/software
export EASYBUILD_INSTALLPATH_MODULES=$SITE/modules
export EASYBUILD_MODULE_SYNTAX=Lua
export MODULEPATH=$SITE/modules/all:$SITE/standin/modules/all
# EasyBuild's output in the order a terminal shows it, not as a pipe buffers it.
export PYTHONUNBUFFERED=1

run_example() {
    local id=$1
    shift
    step "${STATUS:-0}" eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --run-id "$id" "$@" -- Example-1.0-GCCcore-15.2.0.eb
}

setup() {
    install -d "$SITE/software" "$SITE/modules" "$SITE/sources/e/Example" "$SITE/bin" \
        "$SITE/standin/modules/all/GCCcore" "$EASYCONFIGS" "$HOME/sources"
    install -m 644 "$here/site/GCCcore-15.2.0.lua" "$SITE/standin/modules/all/GCCcore/15.2.0.lua"
    install -m 644 "$here/site/example-1.0.sh" "$SITE/sources/e/Example/example-1.0.sh"
    install -m 644 "$here/site/example-1.0.sh" "$HOME/sources/example-1.0.sh"
    install -m 644 "$here/site/Example-1.0-GCCcore-15.2.0.eb" "$EASYCONFIGS/"
    install -m 755 "$here/site/prepare-publication" "$SITE/bin/prepare-publication"
    cd "$EASYCONFIGS"
    step 0 eb Example-1.0-GCCcore-15.2.0.eb
}

case_probe() {
    step 0 eb-staged-rebuild probe --path "$SITE/software"
}

case_dry-run() {
    step 0 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --dry-run -- Example-1.0-GCCcore-15.2.0.eb
}

case_rebuild() {
    run_example example-1
    step 0 eb-staged-rebuild status --state-root "$STATE" example-1
    step 0 ls -A "$SITE/software/Example"
    step 0 eb-staged-rebuild resolve "$MODULE"
    step 0 "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh"
}

case_prepare() {
    step 0 ls -l "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh" "$MODULE"
    run_example example-2 --prepare "$SITE/bin/prepare-publication"
    step 0 ls -l "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh" "$MODULE"
    step 0 cat "$STATE/runs/example-2/prepare-context.json"
}

case_files() {
    step 0 ls -A "$STATE" "$STATE/runs/example-2" "$STATE/generations/example-2"
    step 0 cat "$SITE/software/Example/1.0-GCCcore-15.2.0/.eb-staged-rebuild-complete"
}

case_fail() {
    # The source file in the source path is no longer the one the
    # easyconfig's checksum was taken from.
    printf 'echo changed\n' >>"$SITE/sources/e/Example/example-1.0.sh"
    STATUS=42 run_example example-3
    step 0 eb-staged-rebuild status --state-root "$STATE" example-3
    step 0 "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh"
}

case_resume() {
    step 0 install -m 644 "$here/site/example-1.0.sh" "$SITE/sources/e/Example/example-1.0.sh"
    step 0 eb-staged-rebuild resume --state-root "$STATE" example-3
    step 0 eb-staged-rebuild status --state-root "$STATE" example-3
}

case_rollback() {
    step 2 eb-staged-rebuild rollback --state-root "$STATE" example-2
    step 0 eb-staged-rebuild rollback --state-root "$STATE" example-3
    step 0 eb-staged-rebuild status --state-root "$STATE" example-3
    step 0 ls -A "$SITE/software/Example"
    step 0 eb-staged-rebuild rollback --state-root "$STATE" example-3
}

case_gc() {
    step 0 eb-staged-rebuild status --state-root "$STATE"
    step 0 eb-staged-rebuild gc --state-root "$STATE" --dry-run
    step 0 eb-staged-rebuild gc --state-root "$STATE" --grace-hours 0 --dry-run
    step 0 eb-staged-rebuild gc --state-root "$STATE" --grace-hours 0
    step 0 ls -A "$SITE/software/Example"
}

case_retire() {
    step 0 eb-staged-rebuild retire --state-root "$STATE" --module-file "$MODULE" \
        --reason 'provided by EESSI 2026.06' --dry-run
    step 0 eb-staged-rebuild retire --state-root "$STATE" --module-file "$MODULE" \
        --reason 'provided by EESSI 2026.06'
    local id
    id=$(eb-staged-rebuild status --state-root "$STATE" | awk '$3 == "retire" { id = $1 } END { print id }')
    step 0 eb-staged-rebuild status --state-root "$STATE" "$id"
    step 0 ls -lA "$SITE/modules/all/Example"
    step 0 eb-staged-rebuild gc --state-root "$STATE" --grace-hours 0 --dry-run
    step 0 eb-staged-rebuild rollback --state-root "$STATE" "$id"
    step 0 ls -lA "$SITE/modules/all/Example"
}

case_lock() {
    # Another process holds the state root's lock, says so, and lets go
    # when this case writes a line to it.
    : > "$STATE/.lock"
    local to from
    coproc HOLDER { flock "$STATE/.lock" sh -c 'echo held; read -r _'; }
    from=${HOLDER[0]} to=${HOLDER[1]}
    read -r _ <&"$from"
    step 2 eb-staged-rebuild gc --state-root "$STATE" --dry-run --lock-timeout 0.2
    echo release >&"$to"
    wait "$HOLDER_PID"
}

case_robot() {
    step 0 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --run-id example-robot -- Example-1.0-GCCcore-15.2.0.eb --robot
    step 0 ls -A "$SITE/software" "$SITE/software/.locks"
}

case_userns() {
    run_example example-userns --backend userns
    step 0 ls -l "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh"
}

case_build-dir() {
    step 0 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --build-dir "$SITE/build" --dry-run -- Example-1.0-GCCcore-15.2.0.eb
    run_example example-build-dir --build-dir "$SITE/build"
    step 2 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --build-dir "$SITE/software/Example" --dry-run -- Example-1.0-GCCcore-15.2.0.eb
}

case_no-easyconfig() {
    step 2 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" --dry-run
}

case_not-installed() {
    step 2 eb-staged-rebuild run --software "$SITE/software" --modules "$SITE/modules" \
        --module Example/2.0-GCCcore-15.2.0 -- Example-1.0-GCCcore-15.2.0.eb
}

case_ledger-setup() {
    install -d "$SITE/etc" "$SITE/keys" "$SITE/ledger" "$SITE/ledger-heads"
    install -m 755 "${EB_STACK_BIN:-$(command -v eb-stack)}" "$EB_STACK"
    step 0 "$EB_STACK" --version
    step 0 openssl genpkey -algorithm ed25519 -out "$KEY"
    step 0 chmod 400 "$KEY"
    step 0 openssl pkey -in "$KEY" -pubout -out "$PUB"
    cat >"$SITE/etc/ledger.json" <<EOF
{
  "eb_stack": "$EB_STACK",
  "log": "$LOG",
  "key": "$KEY",
  "public_key": "$PUB",
  "head": "$HEAD",
  "actor": "alice",
  "stack": "2026.06",
  "target": "x86_64/amd/zen4"
}
EOF
    step 0 cat "$SITE/etc/ledger.json"
}

case_ledger-run() {
    run_example example-4 --ledger-config "$SITE/etc/ledger.json"
    step 0 "$EB_STACK" ledger show --log "$LOG" --package Example
    step 0 "$EB_STACK" ledger verify --log "$LOG" --public-key "$PUB" --record 1 --head "$HEAD"
}

case_ledger-rollback() {
    step 0 eb-staged-rebuild rollback --state-root "$STATE" example-4
    step 0 "$EB_STACK" ledger show --log "$LOG" --package Example
}

case_verify() {
    # The builder's EasyBuild configuration leaves the new tree readable by
    # its owner only.
    export EASYBUILD_UMASK=077
    STATUS=2 run_example example-6 --verify
    unset EASYBUILD_UMASK
    step 0 eb-staged-rebuild status --state-root "$STATE" example-6
    step 1 eb-staged-rebuild verify --state-root "$STATE" example-6
    step 0 cat "$STATE/runs/example-6/verify.json"
    step 0 chmod -R go+rX "$STATE/generations/example-6/software/Example/1.0-GCCcore-15.2.0"
    step 0 eb-staged-rebuild verify --state-root "$STATE" example-6
    step 0 eb-staged-rebuild resume --state-root "$STATE" example-6
    step 0 ls -l "$SITE/software/Example/1.0-GCCcore-15.2.0/example-1.0.sh"
}
clean() {
    rm -rf "${SITE:?}/software" "${SITE:?}/modules" "${SITE:?}/sources" "${SITE:?}/bin" "${SITE:?}/standin" \
        "$STATE" "${SITE:?}/build" "${SITE:?}/etc" "${SITE:?}/ledger" "${SITE:?}/ledger-heads" \
        "$EASYCONFIGS" "$HOME/sources"
    if [ -d "$SITE/keys" ]; then
        chmod -R u+w "$SITE/keys"
        rm -rf "$SITE/keys"
    fi
}

command=${1:?usage: site.sh setup|CASE|all|clean}
cd "$EASYCONFIGS" 2>/dev/null || true
case $command in
    setup) setup ;;
    clean) clean ;;
    all)
        setup
        for name in $cases; do
            echo "case: $name"
            "case_$name"
        done
        clean
        ;;
    *)
        if [[ " $(echo $cases) " != *" $command "* ]]; then
            echo "site.sh: unknown case $command" >&2
            exit 2
        fi
        "case_$command"
        ;;
esac
