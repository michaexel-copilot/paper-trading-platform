#!/usr/bin/env bash
# Deploy the Paper Trading Platform on a headless Linux machine.
#
# One-liner from a fresh machine:
#   curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash
#
# The same script installs and updates: safe to re-run at any time.
# Optional environment: DEPLOY_DIR (clone target), DEPLOY_REF (git ref to deploy),
# HOST / PORT (listen address, default 0.0.0.0 / 8000).
set -euo pipefail

REPO_URL="https://github.com/michaexel-copilot/paper-trading-platform.git"
CLONE_DIR="${DEPLOY_DIR:-$HOME/paper-trading-platform}"
SERVICE_NAME="paper-trading"
UNIT_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_HOST="0.0.0.0"
DEFAULT_PORT="8000"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1 — install it and re-run"
}

# --- Bootstrap: when not running inside a checkout, get one and re-exec -------

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd || true)"
if [ -n "$script_dir" ] && [ -f "$script_dir/../backend/app/main.py" ] && [ -f "$script_dir/../frontend/package.json" ]; then
  CHECKOUT_DIR="$(cd "$script_dir/.." && pwd)"
else
  need_cmd git
  if [ -d "$CLONE_DIR/.git" ]; then
    log "updating existing checkout in $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    log "cloning $REPO_URL into $CLONE_DIR"
    git clone "$REPO_URL" "$CLONE_DIR"
  fi
  # No INSTALLER_UPDATED here: the re-exec'd script runs the update phase,
  # which is a no-op pull for a fresh clone and honors DEPLOY_REF.
  exec "$CLONE_DIR/deploy/install.sh" "$@"
fi

# --- Preflight checks ----------------------------------------------------------

command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
  || die "systemd is required; this installer only supports Linux machines running systemd"
need_cmd curl
need_cmd git
if [ "$(id -u)" -ne 0 ]; then
  need_cmd sudo
  sudo -v # cache credentials up front; the build takes a while
fi

# --- Update the checkout, then re-exec the updated script ----------------------

if [ "${INSTALLER_UPDATED:-0}" != "1" ]; then
  if [ -n "${DEPLOY_REF:-}" ]; then
    log "checking out $DEPLOY_REF"
    git -C "$CHECKOUT_DIR" fetch origin
    git -C "$CHECKOUT_DIR" checkout "$DEPLOY_REF"
  else
    log "updating checkout in $CHECKOUT_DIR"
    git -C "$CHECKOUT_DIR" pull --ff-only
  fi
  INSTALLER_UPDATED=1 exec "$CHECKOUT_DIR/deploy/install.sh" "$@"
fi

# --- Prerequisites: uv and Node.js 20+ -----------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
command -v uv >/dev/null 2>&1 || die "uv installation failed"
log "uv $(uv --version | awk '{print $2}')"

node_major="$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)"
if [ -z "${node_major:-}" ] || [ "$node_major" -lt 20 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    log "installing Node.js 20 from NodeSource"
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
  else
    die "Node.js 20+ is required; install it manually and re-run (automatic install only supported on apt-based systems)"
  fi
fi
node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
[ "$node_major" -ge 20 ] || die "Node.js 20+ is required, found $(node --version)"
log "node $(node --version)"

# --- Build ----------------------------------------------------------------------

log "installing backend dependencies"
(cd "$CHECKOUT_DIR/backend" && uv sync)

log "building frontend"
(cd "$CHECKOUT_DIR/frontend" && npm ci && npm run build)

# --- Install and (re)start the service ------------------------------------------

HOST="${HOST:-$DEFAULT_HOST}"
PORT="${PORT:-$DEFAULT_PORT}"
UV_BIN="$(command -v uv)"

log "installing systemd unit $UNIT_TARGET (user=$USER, host=$HOST, port=$PORT)"
rendered="$(mktemp)"
sed -e "s|@USER@|$USER|g" \
    -e "s|@CHECKOUT@|$CHECKOUT_DIR|g" \
    -e "s|@UV@|$UV_BIN|g" \
    -e "s|@HOST@|$HOST|g" \
    -e "s|@PORT@|$PORT|g" \
    "$CHECKOUT_DIR/deploy/paper-trading.service" > "$rendered"

was_active=0
systemctl is-active --quiet "$SERVICE_NAME" && was_active=1 || true

sudo install -m 0644 "$rendered" "$UNIT_TARGET"
rm -f "$rendered"
sudo systemctl daemon-reload
if [ "$was_active" -eq 1 ]; then
  log "restarting $SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"
else
  log "enabling and starting $SERVICE_NAME"
  sudo systemctl enable --now "$SERVICE_NAME"
fi

log "waiting for the service to answer on port $PORT"
healthy=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if [ "$healthy" -ne 1 ]; then
  sudo journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
  die "$SERVICE_NAME did not become healthy; see the log lines above"
fi

log "done — $SERVICE_NAME is running on $HOST:$PORT"
echo "    UI:      http://$HOST:$PORT"
echo "    status:  systemctl status $SERVICE_NAME"
echo "    logs:    journalctl -u $SERVICE_NAME -f"
echo "    start:   $CHECKOUT_DIR/deploy/start.sh"
echo "    stop:    $CHECKOUT_DIR/deploy/stop.sh"
