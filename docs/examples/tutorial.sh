#!/bin/bash
# The commands of the tutorial, from "Make an installation to rebuild" to the
# end, in $TUTORIAL (default ~/rebuild-tutorial), which it creates and
# removes. Before each command it prints a line "+ COMMAND"; a command that
# exits other than the tutorial says, or a PAYLOAD that is not the one it
# shows, stops the script. eb-staged-rebuild must be on PATH.
set -euo pipefail

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$here/steps.sh"
TUTORIAL=${TUTORIAL:-$HOME/rebuild-tutorial}

payload() {
    step 0 cat "$1"
    if [ "$(cat "$1")" != "$2" ]; then
        echo "tutorial.sh: $1 holds $(cat "$1"), not $2" >&2
        exit 1
    fi
}

if [ -e "$TUTORIAL" ]; then
    echo "tutorial.sh: $TUTORIAL exists; remove it first" >&2
    exit 2
fi
mkdir -p "$TUTORIAL/bin"
install -m 755 "$here/eb-standin" "$TUTORIAL/bin/eb"
cd "$TUTORIAL"
export PATH=$PWD/bin:$PATH
step 0 eb --version

mkdir -p software/demo/1/easybuild modules/all/demo
printf 'payload-v1\n' > software/demo/1/PAYLOAD
printf "name = 'demo'\nversion = '1'\n" > software/demo/1/easybuild/demo-1.eb
cat > modules/all/demo/1.lua <<EOF
local root = "$PWD/software/demo/1"
conflict("demo")
setenv("EBROOTDEMO", root)
setenv("EBVERSIONDEMO", "1")
EOF
step 0 cat modules/all/demo/1.lua
export EASYBUILD_INSTALLPATH_SOFTWARE=$PWD/software
export EASYBUILD_INSTALLPATH_MODULES=$PWD/modules

step 0 eb-staged-rebuild probe --path software
step 0 eb-staged-rebuild run --backend installpath --dry-run -- demo-1.eb
step 0 eb-staged-rebuild run --backend installpath --run-id first -- demo-1.eb
payload software/demo/1/PAYLOAD payload-v2
step 0 ls -A software/demo
payload software/demo/.1.first/PAYLOAD payload-v1
step 0 cat modules/all/demo/1.lua
step 0 eb-staged-rebuild status --state-root .eb-staged-rebuild
step 0 eb-staged-rebuild rollback --state-root .eb-staged-rebuild first
payload software/demo/1/PAYLOAD payload-v1
step 0 eb-staged-rebuild status --state-root .eb-staged-rebuild

cd
rm -rf "$TUTORIAL"
