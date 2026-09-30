#!/usr/bin/env bash
# Install or upgrade the paper trading platform on a headless Debian or Ubuntu machine.
#
#   curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash
#
# Where curl is missing, "wget -qO- <url> | sudo bash" does the same. With options:
#
#   curl -fsSL … | sudo bash -s -- --ref v1.0 --port 9000
#   sudo deploy/install.sh --ref v1.0
#
# Options:
#   --ref <ref>    Branch, tag or commit to deploy (default: main)
#   --host <addr>  Address to listen on (default: 0.0.0.0, all interfaces)
#   --port <n>     Port to listen on (default: 8000)
#   -h, --help     Show this help
#
# --host and --port only apply to a first installation. Afterwards the listen
# address is set in /etc/paper-trading/paper-trading.env.
#
# Running the installer again upgrades in place and keeps the data and that file.
# See docs/deployment.md.
set -euo pipefail

REPO_URL=https://github.com/michaexel-copilot/paper-trading-platform.git
SERVICE=paper-trading
SERVICE_USER=papertrading
INSTALL_DIR=/opt/paper-trading
REPO_DIR=$INSTALL_DIR/repo
ENV_FILE=/etc/paper-trading/$SERVICE.env
UNIT_FILE=/etc/systemd/system/$SERVICE.service
HEALTH_TIMEOUT=60

REF=main
HOST=
PORT=
STEP="reading the options"

usage() {
    cat <<'EOF'
Install or upgrade the paper trading platform on Debian or Ubuntu.

Usage: install.sh [--ref <ref>] [--host <addr>] [--port <n>]

  --ref <ref>    Branch, tag or commit to deploy (default: main)
  --host <addr>  Address to listen on (default: 0.0.0.0, all interfaces)
  --port <n>     Port to listen on (default: 8000)
  -h, --help     Show this help

--host and --port only apply to a first installation. Afterwards the listen
address is set in /etc/paper-trading/paper-trading.env.
EOF
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

step() {
    STEP=$1
    printf '\n==> %s\n' "$1"
}

on_exit() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "install.sh: failed while ${STEP,} (exit status $status)." >&2
    fi
}

parse_options() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --ref | --host | --port)
                [[ $# -ge 2 ]] || die "$1 needs a value"
                case $1 in
                    --ref) REF=$2 ;;
                    --host) HOST=$2 ;;
                    --port) PORT=$2 ;;
                esac
                shift 2
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
    done
    [[ -n $REF ]] || die "--ref needs a value"
    if [[ -n $HOST && ! $HOST =~ ^[0-9A-Za-z.:-]+$ ]]; then
        die "--host must be an IP address or host name"
    fi
    if [[ -n $PORT ]] && { [[ ! $PORT =~ ^[0-9]{1,5}$ ]] || ((PORT < 1 || PORT > 65535)); }; then
        die "--port must be a number from 1 to 65535"
    fi
}

# Everything here only looks; nothing on the machine has changed when it fails.
preflight() {
    [[ $EUID -eq 0 ]] ||
        die "this installer needs root. Run it with sudo, for example: curl -fsSL … | sudo bash"
    if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
        die "systemd is not running on this machine. See docs/deployment.md for a manual setup."
    fi
    command -v apt-get >/dev/null ||
        die "apt-get not found. The installer supports Debian and Ubuntu; see docs/deployment.md for a manual setup."
    case $(uname -m) in
        x86_64 | aarch64 | arm64) ;;
        *) die "unsupported architecture $(uname -m); x86-64 and arm64 are supported." ;;
    esac
}

# Vite needs Node 20.19+ or 22.12+; one threshold keeps this simple.
node_is_recent() {
    local version major minor
    command -v node >/dev/null && command -v npm >/dev/null || return 1
    version=$(node --version)
    version=${version#v}
    major=${version%%.*}
    minor=${version#*.}
    minor=${minor%%.*}
    ((major > 22 || (major == 22 && minor >= 12)))
}

install_prerequisites() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends ca-certificates curl git

    if node_is_recent; then
        echo "Node.js $(node --version) is already installed."
    else
        echo "Installing Node.js 22 from NodeSource."
        curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
        apt-get install -y -qq nodejs
        node_is_recent || die "Node.js 22.12 or newer is required, found $(node --version 2>/dev/null || echo none)"
    fi

    if command -v uv >/dev/null; then
        echo "$(uv --version) is already installed."
    else
        echo "Installing uv into /usr/local/bin."
        curl -LsSf https://astral.sh/uv/install.sh |
            env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
        command -v uv >/dev/null || die "uv was installed but is not on PATH"
    fi
}

create_service_user() {
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        echo "User $SERVICE_USER exists."
    else
        useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin "$SERVICE_USER"
        echo "Created user $SERVICE_USER."
    fi
}

# The checkout belongs to the installer: local edits in it are discarded.
fetch_code() {
    mkdir -p "$INSTALL_DIR"
    if [[ -d $REPO_DIR/.git ]]; then
        git -C "$REPO_DIR" fetch --quiet --tags --prune origin
    elif [[ -e $REPO_DIR ]]; then
        die "$REPO_DIR exists but is not a git checkout. Move it away and run again."
    else
        git clone --quiet "$REPO_URL" "$REPO_DIR"
    fi

    if git -C "$REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
        git -C "$REPO_DIR" checkout --quiet --force -B "$REF" "origin/$REF"
    else
        git -C "$REPO_DIR" checkout --quiet --force --detach "$REF"
    fi
    echo "Deployed $REF: $(git -C "$REPO_DIR" log -1 --format='%h %s')"

    [[ -f $REPO_DIR/deploy/$SERVICE.service ]] ||
        die "$REF has no deploy/ directory; choose a ref that includes the deployment files."
}

build() {
    # The managed Python must be outside /root so the service user can run it.
    (cd "$REPO_DIR/backend" &&
        UV_PYTHON_INSTALL_DIR=$INSTALL_DIR/python uv sync --frozen --no-dev)
    (cd "$REPO_DIR/frontend" &&
        npm ci --no-audit --no-fund &&
        npm run build)
    [[ -f $REPO_DIR/frontend/dist/index.html ]] || die "the frontend build produced no index.html"
}

write_env_file() {
    install -d -m 0755 "$(dirname "$ENV_FILE")"
    if [[ -e $ENV_FILE ]]; then
        echo "Keeping the existing $ENV_FILE."
        if [[ -n $HOST || -n $PORT ]]; then
            echo "--host and --port were ignored: edit APP_HOST and APP_PORT in that file instead."
        fi
        return
    fi
    # Create it with its final permissions before any content goes in.
    install -m 0640 -o root -g "$SERVICE_USER" /dev/null "$ENV_FILE"
    sed -e "s|^APP_HOST=.*|APP_HOST=${HOST:-0.0.0.0}|" \
        -e "s|^APP_PORT=.*|APP_PORT=${PORT:-8000}|" \
        "$REPO_DIR/deploy/$SERVICE.env.example" >"$ENV_FILE"
    echo "Wrote $ENV_FILE."
}

install_unit() {
    sed "s|@REPO@|$REPO_DIR|g" "$REPO_DIR/deploy/$SERVICE.service" >"$UNIT_FILE"
    chmod 0644 "$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE"
}

env_value() { # name default
    local value
    value=$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)
    value=${value//\"/}
    echo "${value:-$2}"
}

start_and_wait() {
    local host port probe url
    host=$(env_value APP_HOST 127.0.0.1)
    port=$(env_value APP_PORT 8000)
    case $host in
        0.0.0.0) probe=127.0.0.1 ;;
        ::) probe='[::1]' ;;
        *:*) probe="[$host]" ;;
        *) probe=$host ;;
    esac
    url=http://$probe:$port/api/health

    systemctl restart "$SERVICE"
    echo "Waiting for $url"
    for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
        if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
            summary "$host" "$port"
            return
        fi
        sleep 1
    done
    echo "The service did not answer within ${HEALTH_TIMEOUT}s. Its latest log lines:" >&2
    journalctl -u "$SERVICE" -n 50 --no-pager >&2 || true
    exit 1
}

summary() { # host port
    local shown=$1
    case $1 in
        0.0.0.0 | ::) shown=$(hostname -I 2>/dev/null | awk '{print $1}') ;;
    esac
    cat <<EOF

The paper trading platform is running.

  Open:     http://${shown:-$(hostname)}:$2
  Status:   systemctl status $SERVICE
  Logs:     journalctl -u $SERVICE -f
  Restart:  sudo systemctl restart $SERVICE
  Stop:     sudo systemctl stop $SERVICE
  Settings: $ENV_FILE
  Data:     /var/lib/$SERVICE

EOF
    case $1 in
        127.0.0.1 | ::1 | localhost)
            echo "It listens on this machine only. Reach it through a reverse proxy or an SSH tunnel."
            ;;
        *)
            echo "It serves plain HTTP: logins cross the network unencrypted. For anything but a"
            echo "trusted network, put it behind HTTPS as described in docs/deployment.md."
            ;;
    esac
}

main() {
    trap on_exit EXIT
    parse_options "$@"

    STEP="checking the machine"
    preflight

    # When piped into bash, stdin is this script. Nothing below may read from it.
    exec </dev/null
    umask 022
    # "sudo bash" can keep the caller's HOME; tool caches belong in root's.
    HOME=$(getent passwd 0 | cut -d: -f6)
    export HOME
    # uv goes into /usr/local/bin, which a root shell without sudo may not search.
    case ":$PATH:" in
        *:/usr/local/bin:*) ;;
        *) PATH=/usr/local/bin:$PATH ;;
    esac

    step "Installing prerequisites (git, Node.js, uv)"
    install_prerequisites
    step "Creating the service user"
    create_service_user
    step "Fetching the code ($REF)"
    fetch_code
    step "Building the backend and the web UI"
    build
    step "Writing the configuration"
    write_env_file
    step "Installing the systemd service"
    install_unit
    step "Starting the service"
    start_and_wait
}

# Calling main on the last line means a truncated download runs nothing.
main "$@"
