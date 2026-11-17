# shellcheck shell=bash
# Sourced by the docs/examples scripts. Each command a guide shows runs
# through step, which prints it as "+ COMMAND" and stops the script when it
# exits with another status than the guide says.

# Print the words of a command, quoted where the shell needs it.
show() {
    local line='' word
    for word in "$@"; do
        case $word in
            '' | *[!A-Za-z0-9_./:=@%+,-]*) word="'${word//\'/\'\\\'\'}'" ;;
        esac
        line+=${line:+ }$word
    done
    printf '+ %s\n' "$line"
}

# step STATUS COMMAND...
step() {
    local want=$1 got=0
    shift
    show "$@"
    "$@" || got=$?
    if [ "$got" -ne "$want" ]; then
        printf '%s: %s exited %s, not %s\n' "${0##*/}" "$1" "$got" "$want" >&2
        exit 1
    fi
}
