# Deploying on a Linux server

This runs the platform on a headless Linux machine as a systemd service: started at
boot, restarted if it crashes, logging to the journal. One process serves both the API
and the web UI.

## Requirements

- Debian 12 or newer, or Ubuntu 22.04 or newer, with systemd. x86-64 or arm64.
- Root access through `sudo`.
- Outbound internet access: to GitHub, astral.sh, NodeSource, PyPI and the npm registry
  while installing, and to the market-data sources (OKX, Kraken, Coinbase, Yahoo
  Finance) while running.
- About 1 GB of free memory for the frontend build and 2 GB of disk.

For other distributions see [Manual installation](#manual-installation).

## Install

SSH into the machine and run:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash
```

When it finishes, the platform is running and the installer prints its address. Open
`http://<server>:8000` from your own machine, create an account and trade. The first
start checks every asset against the price sources, which takes about half a minute;
the UI is usable meanwhile.

> **The default installation serves plain HTTP on all network interfaces.** Logins and
> session cookies cross the network unencrypted. That is fine on a trusted network or
> for a first look. For anything else, see [Serve it over HTTPS](#serve-it-over-https).

If the machine runs a firewall, allow the port, for example `sudo ufw allow 8000/tcp`.
The installer does not change firewall rules.

### Read before you run

Piping a script into `sudo bash` runs it unread. To look first:

```sh
git clone https://github.com/michaexel-copilot/paper-trading-platform.git
less paper-trading-platform/deploy/install.sh
sudo paper-trading-platform/deploy/install.sh
```

The result is the same. The installer always deploys its own checkout under
`/opt/paper-trading/repo`, not the directory it was started from, so the clone in your
home directory can be deleted afterwards.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--ref <ref>` | `main` | Branch, tag or commit to deploy. |
| `--host <addr>` | `0.0.0.0` | Address to listen on. `127.0.0.1` keeps it local to the machine. |
| `--port <n>` | `8000` | Port to listen on. |
| `--help` | | Show the options. |

Through the pipe, options go after `bash -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash -s -- --host 127.0.0.1 --port 9000
```

`--host` and `--port` only apply to a first installation. Once the settings file
exists, change `APP_HOST` and `APP_PORT` there.

### What the installer does

1. Checks that it runs as root on a machine with systemd and `apt-get`. If not, it stops
   before changing anything.
2. Installs `git`, `curl` and `ca-certificates` with apt.
3. Installs Node.js 22 from NodeSource, unless Node.js 22.12 or newer is already there.
4. Installs [uv](https://docs.astral.sh/uv/) into `/usr/local/bin`, unless `uv` is
   already there.
5. Creates the system user `papertrading`. It has no home directory and no login shell.
6. Clones the repository, or updates the existing clone, and checks out the ref.
7. Builds the Python environment (`uv sync --frozen --no-dev`) and the web UI
   (`npm ci`, `npm run build`), both from the committed lockfiles.
8. Writes the settings file if there is none.
9. Installs, enables and starts the systemd service and waits until it answers.

Any failing step stops the installer with a message naming the step. It is safe to run
again after fixing the cause.

### What ends up where

| Path | Content | Kept on upgrade |
| --- | --- | --- |
| `/opt/paper-trading/repo` | The code, the Python environment and the built UI. Owned by root. | Replaced |
| `/opt/paper-trading/python` | The Python interpreter uv downloaded, if the system had no Python 3.11. | Yes |
| `/var/lib/paper-trading` | The database `app.db` and the lock file. Owned by `papertrading`. | Yes |
| `/var/cache/paper-trading` | A small Yahoo Finance cache. Safe to delete. | Yes |
| `/etc/paper-trading/paper-trading.env` | The settings. Readable by root and `papertrading` only. | Yes |
| `/etc/systemd/system/paper-trading.service` | The systemd unit. | Replaced |

The service can write to `/var/lib/paper-trading` and `/var/cache/paper-trading` and
nowhere else.

Do not edit files under `/opt/paper-trading/repo`. An upgrade discards local changes
there. Everything meant to be changed is in the settings file.

## Configure

Edit the settings file and restart:

```sh
sudoedit /etc/paper-trading/paper-trading.env
sudo systemctl restart paper-trading
```

One `NAME=value` per line, no quotes, no spaces around `=`.

| Variable | Installed value | Meaning |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | Address to listen on. |
| `APP_PORT` | `8000` | Port to listen on. |
| `DATABASE_URL` | `sqlite+aiosqlite:////var/lib/paper-trading/app.db` | Where the database is. |
| `LOCK_FILE` | `/var/lib/paper-trading/backend.lock` | Lock that keeps a second backend from starting. |
| `COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS. |

The remaining settings (`CHAIN_*`, `AUTO_MIGRATE`, `RUN_MATCHER`,
`CHECK_ASSETS_ON_STARTUP`) are in the file as comments showing their defaults. The
[README](../README.md#settings) explains them.

If you point `DATABASE_URL` or `LOCK_FILE` at another directory, the service cannot
write there until you allow it. Run `sudo systemctl edit paper-trading` and add:

```ini
[Service]
ReadWritePaths=/your/directory
```

## Operate

```sh
systemctl status paper-trading           # is it running, since when, last log lines
sudo systemctl restart paper-trading     # after a settings change
sudo systemctl stop paper-trading
sudo systemctl start paper-trading
sudo systemctl disable paper-trading     # do not start at boot
sudo systemctl enable paper-trading      # start at boot again

journalctl -u paper-trading -f           # follow the log
journalctl -u paper-trading -n 100       # the last 100 lines
journalctl -u paper-trading --since today

curl http://127.0.0.1:8000/api/health    # {"status":"ok"} when it is up
```

The service starts at boot and is restarted five seconds after a crash. Stopping it
gives the backend 30 seconds to finish its shutdown before systemd kills it.

Only one backend may run against a database. Do not start a second copy by hand while
the service runs; it would refuse to start because the lock file is held.

### Start and stop scripts

`deploy/start.sh` and `deploy/stop.sh` do the same as `systemctl start` and
`systemctl stop`, and `start.sh` additionally waits until the platform answers:

```sh
/opt/paper-trading/repo/deploy/start.sh
/opt/paper-trading/repo/deploy/stop.sh
```

They ask for `sudo` when needed. In a checkout that has no systemd service installed
for it, the same scripts run the platform as a plain background process instead; see
[Without systemd](#without-systemd).

## Upgrade

Run the installer again:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash
```

It fetches the new code, rebuilds and restarts the service. The database and the
settings file are kept. The old version keeps running during the build; the platform is
unavailable only for the few seconds of the restart.

Database migrations are applied when the new version starts. They are not undone by
going back to older code, so take a [backup](#back-up) before upgrading.

To go back to an earlier version, pass its tag or commit:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash -s -- --ref <tag-or-commit>
```

If the newer version had migrated the database, restore the backup first.

## Back up

Everything worth keeping is in `/var/lib/paper-trading` and the settings file.

```sh
sudo systemctl stop paper-trading
sudo tar -czf paper-trading-backup-$(date +%F).tar.gz -C / var/lib/paper-trading etc/paper-trading
sudo systemctl start paper-trading
```

Stop the service first: the database has companion files (`app.db-wal`, `app.db-shm`)
that must be copied in a consistent state.

To restore, stop the service, unpack with `sudo tar -xzf <file> -C /` and start it.

## Serve it over HTTPS

Put a reverse proxy that terminates TLS on the same machine, and let the platform
listen only locally.

1. In `/etc/paper-trading/paper-trading.env` set:

   ```
   APP_HOST=127.0.0.1
   COOKIE_SECURE=true
   ```

   then `sudo systemctl restart paper-trading`.

2. Point the proxy at `127.0.0.1:8000`. With [Caddy](https://caddyserver.com/), which
   obtains the certificate itself, `/etc/caddy/Caddyfile` is:

   ```
   trading.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```

   With nginx, inside the `server` block that has your certificate:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

With `COOKIE_SECURE=true` the browser only sends the session cookie over HTTPS, so
logging in over plain HTTP stops working. That is intended.

Without a domain or a proxy, an SSH tunnel gives you an encrypted path to a platform
that listens only locally:

```sh
ssh -L 8000:127.0.0.1:8000 <user>@<server>
```

Then open http://localhost:8000 on your own machine.

## Remove

```sh
sudo systemctl disable --now paper-trading
sudo rm /etc/systemd/system/paper-trading.service
sudo systemctl daemon-reload
sudo rm -rf /opt/paper-trading /etc/paper-trading /var/cache/paper-trading
sudo rm -rf /var/lib/paper-trading     # deletes every account, portfolio and trade
sudo userdel papertrading
```

Node.js and uv stay installed. Remove them with `sudo apt-get remove nodejs` and
`sudo rm /usr/local/bin/uv /usr/local/bin/uvx` if nothing else uses them.

## Manual installation

On a distribution the installer does not support, install these yourself:

- git
- Node.js 22.12 or newer, with npm
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

Then build a checkout:

```sh
git clone https://github.com/michaexel-copilot/paper-trading-platform.git
cd paper-trading-platform
(cd backend && uv sync --frozen --no-dev)
(cd frontend && npm ci && npm run build)
```

### Without systemd

```sh
APP_HOST=0.0.0.0 APP_PORT=8000 deploy/start.sh
deploy/stop.sh
```

`start.sh` runs the backend in the background, detached from the terminal, waits until
it answers and prints where the log is (`backend/data/paper-trading.log`). Without
`APP_HOST` it listens on `127.0.0.1` only. `stop.sh` asks the process to shut down and
waits for it. Other settings go in `backend/.env`; the data is in `backend/data/`.

Nothing restarts the platform after a crash or a reboot in this mode.

### With systemd, by hand

This is what the installer does, for a checkout at `/opt/paper-trading/repo`:

```sh
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin papertrading
sudo install -d /etc/paper-trading
sudo install -m 0640 -o root -g papertrading deploy/paper-trading.env.example /etc/paper-trading/paper-trading.env
sed "s|@REPO@|/opt/paper-trading/repo|g" deploy/paper-trading.service | sudo tee /etc/systemd/system/paper-trading.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now paper-trading
```

The checkout, and the Python interpreter its `backend/.venv` points to, must be readable
by the `papertrading` user and must not be under `/home` or `/root`: the unit hides
those directories from the service. If uv downloads Python for you, set
`UV_PYTHON_INSTALL_DIR=/opt/paper-trading/python` when running `uv sync`.

## Troubleshooting

**The installer says the service did not answer.** It prints the last log lines. The
same log is in `journalctl -u paper-trading -n 100`.

**`Another backend process holds … backend.lock`.** A second backend is running against
the same data, usually one started by hand. Stop it; the service retries every five
seconds.

**`address already in use`.** Another program has the port. Change `APP_PORT` in the
settings file and restart.

**`Permission denied` or `Read-only file system` in the log.** The service may only
write to `/var/lib/paper-trading` and `/var/cache/paper-trading`. If a setting points
elsewhere, add `ReadWritePaths=` as described under [Configure](#configure).

**The frontend build is killed.** The machine ran out of memory. Add swap or use a
machine with at least 1 GB free, then run the installer again.

**The page does not load from another machine.** Check `APP_HOST` is not `127.0.0.1`,
that the firewall allows the port, and that your provider's network rules do too.
`curl http://127.0.0.1:8000/api/health` on the server tells you whether the platform
itself is up.

**Prices are missing.** The machine needs outbound HTTPS to the market-data sources.
`journalctl -u paper-trading` shows which source failed.
