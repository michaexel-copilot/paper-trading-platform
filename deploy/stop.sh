#!/usr/bin/env bash
# Stop the paper trading platform.
#
#   stop.sh   Stop the systemd service when one is installed for this checkout,
#             otherwise stop the background process start.sh launched.
#
# The backend gets 30 seconds to shut down cleanly before it is killed.
set -euo pipefail

# shellcheck source=deploy/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

STOP_TIMEOUT=30

usage() {
    sed -n '2,7s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

stop_service() {
    local state
    state=$(systemctl is-active "$SERVICE" || true)
    if [[ $state == inactive || $state == failed ]]; then
        echo "$SERVICE is not running."
        return
    fi
    # The unit's TimeoutStopSec bounds this; systemd kills the process after it.
    as_root systemctl stop "$SERVICE"
    echo "$SERVICE stopped."
}

stop_standalone() {
    local pid
    if ! pid=$(standalone_pid); then
        rm -f "$PID_FILE"
        echo "Not running."
        return
    fi
    kill -TERM "$pid"
    for ((i = 0; i < STOP_TIMEOUT; i++)); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "PID $pid did not exit within ${STOP_TIMEOUT}s; killing it." >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Stopped (PID $pid)."
}

case ${1:-} in
    -h | --help) usage ;;
    "")
        if service_installed; then stop_service; else stop_standalone; fi
        ;;
    *) die "unknown option: $1 (try --help)" ;;
esac
