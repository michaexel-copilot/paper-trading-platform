# shellcheck shell=bash
# Shared by start.sh and stop.sh. Sourced, not run.
# shellcheck disable=SC2034  # the variables are used by the scripts that source this

SERVICE=paper-trading
UNIT_FILE=/etc/systemd/system/$SERVICE.service
ENV_FILE=/etc/paper-trading/$SERVICE.env

DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BACKEND=$(dirname "$DEPLOY_DIR")/backend
PID_FILE=$BACKEND/data/$SERVICE.pid
LOG_FILE=$BACKEND/data/$SERVICE.log

die() {
    echo "$(basename "$0"): $*" >&2
    exit 1
}

as_root() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

# True when a systemd unit is installed that runs this checkout.
service_installed() {
    [[ -f $UNIT_FILE ]] && grep -qF "$DEPLOY_DIR/start.sh" "$UNIT_FILE"
}

# Print the PID of the background process start.sh launched, or fail when there is none.
standalone_pid() {
    local pid
    [[ -f $PID_FILE ]] || return 1
    pid=$(<"$PID_FILE")
    [[ $pid =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # A recycled PID that now belongs to something else does not count.
    tr '\0' ' ' 2>/dev/null <"/proc/$pid/cmdline" | grep -q 'app\.main:app' || return 1
    echo "$pid"
}
