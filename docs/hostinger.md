# Deploying on a Hostinger VPS

This puts the platform on a Hostinger VPS (KVM) with the one-line installer, then
serves it over HTTPS on your own domain. [deployment.md](deployment.md) explains the
installer, configuration, backups and upgrades; this page covers what is specific to
Hostinger.

## Which plan

Any KVM plan works. The platform needs 1 GB of memory and 2 GB of disk; the frontend
build is the peak, so on a 1 GB plan add swap first (step 2). KVM 1 (1 vCPU, 4 GB) is
plenty.

## 1. Create the VPS

In hPanel choose **VPS → Add / Manage**, then as operating system **Ubuntu 24.04**
(plain, without a control panel template). Set a root password or upload an SSH key,
and note the server's IPv4 address.

## 2. First login and basic hardening

```sh
ssh root@<server-ip>
apt-get update && apt-get -y upgrade
```

Optional swap, recommended on small plans:

```sh
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Firewall. Allow SSH first, or you lock yourself out:

```sh
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

Hostinger also has a firewall in hPanel (**VPS → Security → Firewall**). If you use
it, allow the same ports there.

## 3. Install the platform

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash -s -- --host 127.0.0.1
```

`--host 127.0.0.1` keeps the platform off the public internet until HTTPS is in place.
Check it from the server:

```sh
curl http://127.0.0.1:8000/api/health     # {"status":"ok"}
```

## 4. Point a domain at the server

In your domain's DNS (hPanel **Domains → DNS / Nameservers** if the domain is with
Hostinger) create an **A record** for e.g. `trading.example.com` with the server's
IPv4 address. Wait until it resolves:

```sh
dig +short trading.example.com
```

## 5. Enable HTTPS

```sh
/opt/paper-trading/repo/deploy/enable-https.sh trading.example.com
```

This installs Caddy (which gets and renews the Let's Encrypt certificate), proxies the
domain to the platform, sets `COOKIE_SECURE=true` and restarts the service. Open
`https://trading.example.com`, create an account and trade.

Without a domain, skip steps 4 and 5 and use an SSH tunnel instead:

```sh
ssh -L 8000:127.0.0.1:8000 root@<server-ip>      # then open http://localhost:8000
```

## Operate

Upgrades, backups, logs and removal work as in [deployment.md](deployment.md). The
essentials:

```sh
systemctl status paper-trading
journalctl -u paper-trading -f
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash   # upgrade
```

Hostinger offers weekly snapshots and backups in hPanel; those are a second line of
defence. Take your own copy of `/var/lib/paper-trading` as described under
[Back up](deployment.md#back-up) before upgrades.

## Troubleshooting

**The certificate is not issued.** DNS must point at the server and ports 80 and 443
must be open in both `ufw` and the hPanel firewall. `journalctl -u caddy -n 50` shows
the reason.

**The frontend build is killed.** Out of memory; add swap (step 2) and run the
installer again.

**Login works over HTTPS but not over `http://<ip>:8000`.** Intended: with
`COOKIE_SECURE=true` the browser sends the session cookie only over HTTPS.

**Prices are missing.** The VPS needs outbound HTTPS to OKX, Kraken, Coinbase and Yahoo
Finance. Some data-centre IP ranges are rate-limited by Yahoo; the other sources in a
chain answer meanwhile. `journalctl -u paper-trading` shows which source failed.
