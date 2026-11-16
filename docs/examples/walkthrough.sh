#!/bin/sh
# Walk through probe, dry-run, a failed build, resume, rollback, and gc.
# The EasyBuild stand-in is docs/examples/eb-standin. Output is the transcript.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
STANDIN="$ROOT/docs/examples/eb-standin"
chmod +x "$STANDIN"

# The tool prints the absolute --eb path it was given. A transcript replaces
# this checkout with <checkout>, so the recording is not tied to one machine.
tool() {
    eb-staged-rebuild "$@" >"$WORK/.out" 2>"$WORK/.err"
    _status=$?
    sed "s|$ROOT|<checkout>|g" "$WORK/.out"
    sed "s|$ROOT|<checkout>|g" "$WORK/.err" >&2
    return $_status
}

if [ -n "${WALKTHROUGH_ROOT:-}" ]; then
    WORK="$WALKTHROUGH_ROOT"
    rm -rf "$WORK"
    mkdir -p "$WORK"
else
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
fi

SOFTWARE="$WORK/software"
MODULES="$WORK/modules"
STATE="$WORK/.eb-staged-rebuild"
export EASYBUILD_INSTALLPATH_SOFTWARE="$SOFTWARE"
export EASYBUILD_INSTALLPATH_MODULES="$MODULES"

mkdir -p "$SOFTWARE/demo/1/easybuild" "$MODULES/all/demo"
printf 'payload-v1\n' > "$SOFTWARE/demo/1/PAYLOAD"
printf 'name = '\''demo'\''\nversion = '\''1'\''\n' > "$SOFTWARE/demo/1/easybuild/demo-1.eb"
cat > "$MODULES/all/demo/1.lua" <<EOF
local root = "$SOFTWARE/demo/1"
conflict("demo")
setenv("EBROOTDEMO", root)
setenv("EBVERSIONDEMO", "1")
EOF

cd "$WORK"
echo "=== version ==="
tool --version
echo "=== help ==="
tool --help
echo "=== probe ==="
tool probe --path "$SOFTWARE" --eb "$STANDIN"
echo "=== probe json keys ==="
tool probe --path "$SOFTWARE" --eb "$STANDIN" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d))'
echo "=== dry-run ==="
tool run \
  --software "$SOFTWARE" \
  --modules "$MODULES" \
  --state-root "$STATE" \
  --eb "$STANDIN" \
  --backend installpath \
  --module demo/1 \
  --dry-run \
  -- demo-1.eb --robot
echo "=== failed build ==="
set +e
EB_STANDIN_FAIL=1 tool run \
  --software "$SOFTWARE" \
  --modules "$MODULES" \
  --state-root "$STATE" \
  --eb "$STANDIN" \
  --backend installpath \
  --run-id demo-run \
  --module demo/1 \
  -- demo-1.eb --robot
echo "exit:$?"
set -e
echo "=== status after failure ==="
tool status --state-root "$STATE"
echo "=== live payload after failure ==="
cat "$SOFTWARE/demo/1/PAYLOAD"
echo "=== resume ==="
tool resume --state-root "$STATE" demo-run
echo "=== status after resume ==="
tool status --state-root "$STATE" demo-run
echo "=== live payload after publish ==="
cat "$SOFTWARE/demo/1/PAYLOAD"
echo "=== resolve ==="
tool resolve "$MODULES/all/demo/1.lua"
echo "=== retire dry-run ==="
tool retire \
  --state-root "$STATE" \
  --module-file "$MODULES/all/demo/1.lua" \
  --reason "published on a CernVM-FS repository" \
  --dry-run
echo "=== retire ==="
tool retire \
  --state-root "$STATE" \
  --module-file "$MODULES/all/demo/1.lua" \
  --reason "published on a CernVM-FS repository"
echo "=== status after retire ==="
tool status --state-root "$STATE"
echo "=== rollback retire ==="
RETIRE_ID=$(tool status --state-root "$STATE" | awk '/ retire / {print $1; exit}')
tool rollback --state-root "$STATE" "$RETIRE_ID"
echo "=== module restored ==="
test -f "$MODULES/all/demo/1.lua" && echo "module file present"
echo "=== retired sibling ==="
find "$SOFTWARE/demo" -maxdepth 1 -mindepth 1 -printf '%f\n' | sort
echo "=== rollback ==="
tool rollback --state-root "$STATE" demo-run
echo "=== live payload after rollback ==="
cat "$SOFTWARE/demo/1/PAYLOAD"
echo "=== status after rollback ==="
tool status --state-root "$STATE" demo-run
echo "=== gc dry-run ==="
tool gc --state-root "$STATE" --grace-hours 144 --dry-run
echo "=== gc include-failed dry-run ==="
tool gc --state-root "$STATE" --grace-hours 144 --dry-run --include-failed
echo "=== second rollback ==="
tool rollback --state-root "$STATE" demo-run
echo "=== build-events help ==="
python3 -m eb_staged_rebuild.build_events --help
echo "=== ledger-maintenance help ==="
python3 -m eb_staged_rebuild.ledger_maintenance --help
echo "=== missing easyconfig ==="
set +e
tool run \
  --software "$SOFTWARE" \
  --modules "$MODULES" \
  --state-root "$STATE" \
  --eb "$STANDIN" \
  --backend installpath \
  --dry-run
echo "exit:$?"
set -e
echo "WORK=$WORK"
