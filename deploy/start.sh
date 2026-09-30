#!/usr/bin/env bash
# Start the paper trading platform.
#
#   start.sh               Start the systemd service when one is installed for this
#                          checkout, otherwise start a background process.
#   start.sh --foreground  Run the backend in this process. The systemd unit uses this.
#
# APP_HOST and APP_PORT choose the listen address (default 127.0.0.1:8000). The
# systemd service takes them from /etc/paper-trading/paper-trading.env.
set -euo pipefail

# shellcheck source=deploy/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

HEALTH_TIMEOUT=60
UVICORN=$BACKEND/.venv/bin/uvicorn

usage() {
    sed -n '2,9s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

require_build() {
    [[ -x $UVICORN ]] ||
        die "backend/.venv is missing. Build it first: (cd backend && uv sync --frozen --no-dev)"
}

health_url() { # host port
    local host=$1
    case $host in
        0.0.0.0) host=127.0.0.1 ;;
        ::) host='[::1]' ;;
        *:*) host="[$host]" ;;
    esac
    echo "http://$host:$2/api/health"
}

healthy() { # url
    if command -v curl >/dev/null; then
        curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null
    else
        "$BACKEND/.venv/bin/python" -c \
            'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' \
            "$1" 2>/dev/null
    fi
}

# Read a value from the service's environment file, which only root may read.
env_value() { # name default
    local value
    value=$(as_root sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n 1) || true
    value=${value//\"/}
    echo "${value:-$2}"
}

run_foreground() {
    require_build
    cd "$BACKEND"
    # One process only: it holds the lock file and runs the order matcher.
    exec "$UVICORN" app.main:app \
        --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8000}" \
        --timeout-graceful-shutdown 20
}

start_service() {
    local url
    if systemctl is-active --quiet "$SERVICE"; then
        echo "$SERVICE is already running."
        return
    fi
    as_root systemctl start "$SERVICE"
    url=$(health_url "$(env_value APP_HOST 127.0.0.1)" "$(env_value APP_PORT 8000)")
    for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
        if healthy "$url"; then
            echo "$SERVICE is running."
            return
        fi
        sleep 1
    done
    die "no answer from $url after ${HEALTH_TIMEOUT}s. See: journalctl -u $SERVICE -n 50"
}

start_standalone() {
    local pid url
    require_build
    if pid=$(standalone_pid); then
        echo "Already running (PID $pid)."
        return
    fi
    url=$(health_url "${APP_HOST:-127.0.0.1}" "${APP_PORT:-8000}")
    if healthy "$url"; then
        die "something else already answers at $url. Set APP_PORT to a free port."
    fi

    mkdir -p "$(dirname "$PID_FILE")"
    nohup "$DEPLOY_DIR/start.sh" --foreground >>"$LOG_FILE" 2>&1 </dev/null &
    pid=$!
    echo "$pid" >"$PID_FILE"

    for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            die "the backend exited while starting. See $LOG_FILE"
        fi
        if healthy "$url"; then
            echo "Running (PID $pid) at ${url%/api/health}"
            echo "Log: $LOG_FILE"
            return
        fi
        sleep 1
    done
    die "no answer from $url after ${HEALTH_TIMEOUT}s. PID $pid is still running;" \
        "see $LOG_FILE and stop it with deploy/stop.sh"
}

case ${1:-} in
    --foreground) run_foreground ;;
    -h | --help) usage ;;
    "")
        if service_installed; then start_service; else start_standalone; fi
        ;;
    *) die "unknown option: $1 (try --help)" ;;
esac
