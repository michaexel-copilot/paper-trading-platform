# Deploy on a headless Linux machine

One command takes a fresh Linux machine (with systemd) to a running
Paper Trading Platform service:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash
```

That clones the repository to `~/paper-trading-platform`, installs any missing
prerequisites, builds the frontend, installs a systemd service called
`paper-trading` and starts it. The UI is then reachable on port 8000, e.g.
`http://<server-ip>:8000`.

Prefer to inspect first (reasonable for anything piped to a shell):

```sh
curl -fsSL -o install.sh https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh
less install.sh
bash install.sh
```

Already have a checkout on the machine? Run `deploy/install.sh` from inside it;
it operates in place instead of cloning.

## What you need

- A headless Linux machine running **systemd** (Debian/Ubuntu servers work out
  of the box), with outbound internet access for market data.
- `curl`, `bash`, `git` and **sudo** rights. The installer needs sudo only to
  write the unit file and talk to systemctl; the service itself runs as your
  user, never as root.
- `uv` and Node.js 20+ are installed automatically when missing (uv via the
  official astral.sh installer, Node.js via NodeSource on apt-based systems).
  On other distributions, install Node.js 20+ manually and re-run.

## What the installer does

1. Checks out or updates the repository (`~/paper-trading-platform` when piped
   via curl, in place otherwise).
2. Installs missing prerequisites.
3. Builds the backend environment (`uv sync`) and the frontend (`npm ci &&
   npm run build`). If a build fails, the script stops and a previously
   installed service keeps running untouched.
4. Renders `deploy/paper-trading.service` and installs it to
   `/etc/systemd/system/paper-trading.service`.
5. Enables and starts the service (or restarts it, when updating).

The script is idempotent: re-running it updates the checkout, rebuilds and
restarts the service. It never touches `backend/.env` or `backend/data/`, so
your configuration, database, accounts, portfolios and trades survive updates.

## Configuration

Defaults work with zero configuration: the service listens on `0.0.0.0:8000`
and stores its database in `backend/data/`.

To change the listen address or any application setting, create
`backend/.env` (it is loaded by both systemd and the app) and restart:

```sh
echo "PORT=9000" >> ~/paper-trading-platform/backend/.env
systemctl restart paper-trading   # or: sudo systemctl restart paper-trading
```

- `HOST` / `PORT` — listen address for the service (defaults `0.0.0.0` / `8000`).
- All application settings from the root README (`DATABASE_URL`, `CHAIN_*`,
  `COOKIE_SECURE`, ...) work here too.

You can also pin what gets deployed: `DEPLOY_REF=<branch-or-tag>` and
`DEPLOY_DIR=<clone target>` are honored by the installer, e.g.
`curl -fsSL ... | DEPLOY_REF=v1.0.0 bash`.

## Running it

Everyday control via the scripts (they wrap systemctl so you don't have to
remember the unit name):

```sh
~/paper-trading-platform/deploy/start.sh   # starts and waits until the UI answers
~/paper-trading-platform/deploy/stop.sh    # graceful stop
```

Equivalent systemctl commands:

```sh
systemctl start paper-trading
systemctl stop paper-trading
systemctl restart paper-trading
systemctl status paper-trading
```

The service starts automatically on boot and is restarted by systemd if it
crashes. Exactly one backend process runs at a time (enforced by systemd and
the application's lock file).

## Logs

```sh
journalctl -u paper-trading -f
```

## Updating

Re-run the same one-liner (or `deploy/install.sh` from the checkout). It pulls
the latest revision, rebuilds and restarts, preserving data and configuration.

## Removing the deployment

```sh
~/paper-trading-platform/deploy/stop.sh
sudo systemctl disable paper-trading
sudo rm /etc/systemd/system/paper-trading.service
sudo systemctl daemon-reload
rm -rf ~/paper-trading-platform   # deletes the database in backend/data/
```

## Security notes

- The default `0.0.0.0` binding makes the UI reachable from any machine that
  can reach the server — that is usually what you want on a headless box, but
  anyone who can reach the port can register an account. Restrict access with
  a firewall (e.g. `ufw allow from <your-ip> to any port 8000`) if the machine
  is exposed.
- There is no TLS. Before serving this over untrusted networks, put it behind
  a reverse proxy with HTTPS and set `COOKIE_SECURE=true` in `backend/.env`.
- Yahoo Finance data is unofficial and licensed for personal use; see the root
  README before offering the platform to others.
