#!/usr/bin/env bash
# Start the Paper Trading Platform service and wait until it answers.
set -euo pipefail

SERVICE_NAME="paper-trading"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="$script_dir/../backend/.env"

die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

systemctl cat "$SERVICE_NAME" >/dev/null 2>&1 \
  || die "service '$SERVICE_NAME' is not installed; run deploy/install.sh first"

sudo_cmd=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "sudo is required to control the service"
  sudo_cmd="sudo"
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "$SERVICE_NAME is already running"
  exit 0
fi

$sudo_cmd systemctl start "$SERVICE_NAME"

port="$(grep -E '^[[:space:]]*PORT[[:space:]]*=' "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d " \t\r\"'" || true)"
port="${port:-8000}"

echo "waiting for $SERVICE_NAME on port $port ..."
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
    echo "$SERVICE_NAME is up on port $port"
    exit 0
  fi
  sleep 1
done

$sudo_cmd journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
die "$SERVICE_NAME did not become healthy; see the log lines above"
