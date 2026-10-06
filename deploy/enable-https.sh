#!/usr/bin/env bash
# Serve the platform over HTTPS on this machine with Caddy, which obtains and renews
# the certificate itself.
#
#   sudo deploy/enable-https.sh trading.example.com
#
# The domain's DNS A (and AAAA) record must already point at this server, and ports
# 80 and 443 must be reachable. The platform is switched to listen on 127.0.0.1 only
# and to set secure cookies. The site goes into /etc/caddy/conf.d/<domain>.caddy, so
# other services can get their own subdomain next to it. Running it again is safe.
# Note: the main Caddyfile is replaced by a single import line (a backup is kept as
# Caddyfile.orig).
# See docs/hostinger.md.
set -euo pipefail

SERVICE=paper-trading
ENV_FILE=/etc/paper-trading/$SERVICE.env
CADDYFILE=/etc/caddy/Caddyfile

die() {
    echo "enable-https.sh: $*" >&2
    exit 1
}

usage() {
    echo "Usage: sudo enable-https.sh <domain>"
}

[[ ${1:-} == -h || ${1:-} == --help ]] && { usage; exit 0; }
[[ $# -eq 1 ]] || { usage >&2; exit 1; }
DOMAIN=$1
[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$ ]] || die "'$DOMAIN' is not a valid domain name"
[[ $EUID -eq 0 ]] || die "run as root (use sudo)"
[[ -f $ENV_FILE ]] || die "$ENV_FILE not found; install the platform first (deploy/install.sh)"
command -v apt-get >/dev/null || die "needs a Debian or Ubuntu machine"

PORT=$(sed -n 's/^APP_PORT=//p' "$ENV_FILE" | tail -n1)
PORT=${PORT:-8000}

if ! command -v caddy >/dev/null; then
    echo "==> Installing Caddy"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
        gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        -o /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi

echo "==> Configuring Caddy for $DOMAIN"
# One file per site in conf.d, so further services on this server (other subdomains)
# can be added without touching this one.
CONF_DIR=/etc/caddy/conf.d
IMPORT_LINE="import $CONF_DIR/*.caddy"
install -d "$CONF_DIR"
[[ -f $CADDYFILE && ! -f $CADDYFILE.orig ]] && cp "$CADDYFILE" "$CADDYFILE.orig"
if ! grep -qxF "$IMPORT_LINE" "$CADDYFILE" 2>/dev/null; then
    echo "$IMPORT_LINE" >"$CADDYFILE"
fi
cat >"$CONF_DIR/$DOMAIN.caddy" <<CADDY
$DOMAIN {
	reverse_proxy 127.0.0.1:$PORT
}
CADDY
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null || die "Caddy rejected the configuration"

echo "==> Switching the platform to localhost-only with secure cookies"
set_var() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >>"$ENV_FILE"
    fi
}
set_var APP_HOST 127.0.0.1
set_var COOKIE_SECURE true

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    echo "==> Opening ports 80 and 443 in ufw, closing $PORT"
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw delete allow "$PORT/tcp" >/dev/null 2>&1 || true
fi

systemctl restart "$SERVICE"
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

echo
echo "Done. Open https://$DOMAIN (the certificate can take a minute on first use)."
echo "If it does not load: journalctl -u caddy -n 50"
