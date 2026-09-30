#!/usr/bin/env bash
# Stop the Paper Trading Platform service gracefully.
set -euo pipefail

SERVICE_NAME="paper-trading"

die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

systemctl cat "$SERVICE_NAME" >/dev/null 2>&1 \
  || die "service '$SERVICE_NAME' is not installed; nothing to stop"

sudo_cmd=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "sudo is required to control the service"
  sudo_cmd="sudo"
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
  $sudo_cmd systemctl stop "$SERVICE_NAME"
  echo "$SERVICE_NAME stopped"
else
  echo "$SERVICE_NAME is not running"
fi

systemctl is-active "$SERVICE_NAME" 2>&1 || true
