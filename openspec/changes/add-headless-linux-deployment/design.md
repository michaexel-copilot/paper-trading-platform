## Context

See proposal.md for motivation. What the code on `main` gives us to build on:

- **One process serves everything.** `backend/app/main.py` serves the API and, when
  `frontend/dist/index.html` exists, the built UI. `frontend_dist` resolves relative to
  the backend directory, so a built checkout needs no web server in front.
- **Exactly one process.** `ProcessLock` takes a non-blocking `flock` on the lock file
  at startup and a second process fails with `AlreadyRunning`. No `--workers`, no
  `--reload`. `flock` is released by the kernel when the process dies, so a crash leaves
  no stale lock and a supervised restart is safe.
- **Clean shutdown exists.** The FastAPI lifespan stops the matcher, cancels the daily
  asset check, closes the market sources, disposes the engine and releases the lock.
  Uvicorn runs that on SIGTERM.
- **Settings come from the environment.** `Settings` (pydantic-settings) reads
  environment variables and a `.env` in the working directory. `DATABASE_URL` and
  `LOCK_FILE` default to `./data/…`, relative to the working directory. `make_engine`
  creates the SQLite parent directory. Migrations run at startup (`AUTO_MIGRATE`).
- **Host and port are not settings.** They are uvicorn arguments.
- **Toolchain.** Python `>=3.11` pinned to 3.11 in `backend/.python-version`, managed by
  uv with a committed `uv.lock`. The frontend has a committed `package-lock.json`; Vite 8
  requires Node `^20.19.0 || >=22.12.0`. Debian 12 ships Node 18, Ubuntu 22.04 ships
  Node 12, so distribution packages are not enough.
- **Repository.** Public on GitHub (`michaexel-copilot/paper-trading-platform`, default
  branch `main`), so an unauthenticated `curl` of a raw file and an anonymous clone both
  work. It is developed on Windows with `core.autocrlf=true` and no `.gitattributes`.
- **Health check.** `GET /api/health` returns `{"status": "ok"}` and needs no login.
- **First start is slow.** Seeding and the asset availability check take about half a
  minute, but they run after the app starts accepting requests, so the health endpoint
  answers early.

## Goals / Non-Goals

**Goals:**

- One command from a bare Debian/Ubuntu machine to a running, supervised platform.
- The same command upgrades.
- Data and configuration live outside the code tree and survive upgrades and re-clones.
- The launch command is defined in one place.
- The service runs unprivileged and cannot modify its own code.

**Non-Goals:**

- Containers, Kubernetes, or any orchestration.
- TLS termination, a bundled reverse proxy, or firewall changes. The docs describe them.
- Postgres provisioning. `DATABASE_URL` still allows it; the installer sets up SQLite.
- RPM-based distributions, Alpine, or non-systemd init in the installer.
- An uninstall script. Removal is five commands, documented.
- Changing application code, including making host and port application settings.
- Zero-downtime upgrades. One process holds the lock; an upgrade is a restart.

## Decisions

### 1. Native systemd service, not Docker

The target is "a linux machine (headless)" with "systemctl control files". A venv plus a
unit file has the fewest moving parts, uses the journal for logs, and matches the
single-process constraint directly.

*Alternative:* a Docker image with compose. Rejected: it adds a Docker dependency to the
one-liner, and systemd would still be needed to supervise it.

### 2. Layout on the target machine

| Path | Content | Owner | Survives upgrade |
| --- | --- | --- | --- |
| `/opt/paper-trading/repo` | git clone, `backend/.venv`, `frontend/dist` | root | replaced |
| `/opt/paper-trading/python` | uv-managed Python 3.11 | root | kept |
| `/var/lib/paper-trading` | `app.db` (+ WAL files), `backend.lock` | `papertrading` | kept |
| `/var/cache/paper-trading` | yfinance's timezone/cookie cache | `papertrading` | disposable |
| `/etc/paper-trading/paper-trading.env` | settings, mode `0640 root:papertrading` | root | kept |
| `/etc/systemd/system/paper-trading.service` | the unit | root | replaced |

Code is root-owned and read-only to the service. State is the only thing the service can
write. The environment file sets
`DATABASE_URL=sqlite+aiosqlite:////var/lib/paper-trading/app.db` and
`LOCK_FILE=/var/lib/paper-trading/backend.lock`, both existing settings, so no
application change is needed to move the data out of `backend/data`.

uv would put its managed Python under `/root/.local/share/uv`, which the service user
cannot read; `UV_PYTHON_INSTALL_DIR=/opt/paper-trading/python` during the build puts it
where the venv's interpreter symlink stays resolvable.

*Alternative:* everything under one directory owned by the service user, data in
`backend/data`. Rejected: a compromised service could rewrite its own code, and
"delete the checkout and reinstall" would delete the database.

### 3. `install.sh` is both the bootstrap and the installer

The one-liner pipes `deploy/install.sh` from `raw.githubusercontent.com` into
`sudo bash`. The script does not depend on any other file being present when it starts:
it clones the repository first and takes the unit and the example environment file from
the clone. Run from inside an existing clone it behaves identically (it still manages
`/opt/paper-trading/repo`, not the directory it was run from).

Steps, each announced and each fatal on error (`set -euo pipefail`, an `ERR` trap naming
the step):

1. Preflight, changing nothing: root, `systemctl` present and PID 1 is systemd,
   `apt-get` present, architecture is x86-64 or arm64.
2. `apt-get install -y --no-install-recommends ca-certificates curl git`.
3. Node.js: keep the installed one if `node --version` is `>=22.12.0`; otherwise install
   Node 22 from NodeSource. One threshold rather than Vite's two ranges.
4. uv: keep it if on `PATH`; otherwise run Astral's installer with
   `UV_INSTALL_DIR=/usr/local/bin` and `UV_NO_MODIFY_PATH=1`.
5. Create the system user: `useradd --system --no-create-home --shell /usr/sbin/nologin papertrading`, if absent.
6. Code: `git clone` if `/opt/paper-trading/repo` is absent, else `git fetch --tags origin`.
   Then `git checkout --force` the ref and, if it is a branch, `git reset --hard origin/<ref>`.
   The tree is installer-managed; local edits there are not preserved, and the docs say so.
7. Backend: `uv sync --frozen --no-dev` in `backend/`.
8. Frontend: `npm ci` then `npm run build` in `frontend/`.
9. Environment file: copy `deploy/paper-trading.env.example` to
   `/etc/paper-trading/paper-trading.env` only if it does not exist, applying `--host`
   and `--port`. If it exists, leave it alone and say so, including when `--host` or
   `--port` were passed and are therefore ignored.
10. Unit: render `deploy/paper-trading.service` to `/etc/systemd/system/`,
    `systemctl daemon-reload`, `systemctl enable paper-trading`.
11. `systemctl restart paper-trading`, poll `http://127.0.0.1:<port>/api/health` for up
    to 60 seconds, then print the URL and the `systemctl`/`journalctl` commands. On
    timeout, print `journalctl -u paper-trading -n 50` and exit non-zero.

The port polled in step 11 is read back from the environment file, not from the option,
so an upgrade of a reconfigured installation checks the right port.

Options are flags, passed through the pipe with `bash -s --`:
`--ref <branch|tag|commit>` (default `main`), `--host <addr>` (default `0.0.0.0`),
`--port <n>` (default `8000`), `--help`.

*Alternative for options:* environment variables (`curl … | sudo REF=x bash`). Rejected:
whether `sudo` passes them depends on the sudoers policy; flags always arrive.

*Alternative for the build user:* build as `papertrading`. Rejected for decision 2's
ownership model; see Risks for what building as root costs.

### 4. `start.sh --foreground` is the single launch command

```
exec "$BACKEND/.venv/bin/uvicorn" app.main:app --host "$APP_HOST" --port "$APP_PORT" \
    --timeout-graceful-shutdown 20
```

lives in one place. The unit's `ExecStart` calls `start.sh --foreground`; the
no-systemd path of `start.sh` runs the same thing under `nohup`. The graceful-shutdown
timeout makes uvicorn drop lingering connections after 20 seconds, so the lifespan
shutdown still runs inside the 30-second stop timeout. The venv's
uvicorn is called directly rather than through `uv run`, so the running service needs
neither uv nor network access nor write access to the code tree.

`APP_HOST` and `APP_PORT` are new variable names read only by the scripts. They are not
`HOST`/`PORT` because shells and tools commonly set `HOST`. `Settings` uses
`extra="ignore"` and only reads fields it declares, so they do not disturb the backend.
Script defaults without an environment file are `127.0.0.1` and `8000`, matching
uvicorn and the README; the installer's environment file sets `0.0.0.0` explicitly.

`start.sh` without `--foreground`, and `stop.sh`, pick a mode:

- **Service mode** — `/etc/systemd/system/paper-trading.service` exists and its
  `ExecStart` points at this checkout: `systemctl start|stop paper-trading` (through
  `sudo` when not root), then for start, wait for health. Matching on the checkout means
  a second, separately built checkout on the same machine runs standalone instead of
  controlling the installed service.
- **Standalone mode** — otherwise: take `APP_HOST` and `APP_PORT` from the environment
  (the backend still reads its own settings from `backend/.env`), start in the background from
  `backend/` with output appended to `backend/data/paper-trading.log` and the PID in
  `backend/data/paper-trading.pid`, wait for health. `stop.sh` sends SIGTERM to that
  PID, waits up to 30 seconds, then SIGKILL, and removes the PID file. A PID file whose
  process is gone, or is not the platform, is treated as "not running" and removed.
  `backend/data/` is already git-ignored.

Both scripts locate the repository from their own path, so they work from any directory.
What they share (paths, mode detection, PID lookup) is in `deploy/common.sh`, which they
source. `install.sh` does not use it, because it must run before any checkout exists.

### 5. The unit

```ini
[Unit]
Description=Paper Trading Platform
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=papertrading
Group=papertrading
WorkingDirectory=@REPO@/backend
EnvironmentFile=/etc/paper-trading/paper-trading.env
Environment=XDG_CACHE_HOME=/var/cache/paper-trading
ExecStart=@REPO@/deploy/start.sh --foreground
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StateDirectory=paper-trading
StateDirectoryMode=0750
CacheDirectory=paper-trading
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- `StateDirectory`/`CacheDirectory` have systemd create the two writable directories with
  the right owner; under `ProtectSystem=strict` they are the only writable paths. The
  state directory is `0750` because the database holds password hashes and sessions.
- `Restart=on-failure`, not `always`: `systemctl stop` and a clean exit stay stopped.
  A start that fails because another process holds the lock retries every 5 seconds and
  is visible in the journal.
- `TimeoutStopSec=30` gives the lifespan shutdown time before systemd sends SIGKILL.
- `@REPO@` is substituted by the installer. The file is a template because the path
  appears twice and systemd does not expand variables in `WorkingDirectory`.
- `XDG_CACHE_HOME` exists because yfinance caches under the user cache directory and the
  service user has no home.

### 6. Line endings

`.gitattributes` with `*.sh text eol=lf`, `*.service text eol=lf`, `*.env.example text eol=lf`.
The repository stores LF already; this guarantees an LF working tree on Windows too, so a
script copied from a Windows checkout with `scp` still runs. The scripts are committed
with the executable bit set (`git update-index --chmod=+x`), which Windows cannot express
through the filesystem.

### 7. Documentation

`docs/deployment.md` is the operator's document; the README gets a short section with the
one-liner and a link. The README's existing "Start it" section stays as the developer
path. The README currently ends with a stray UTF-16 encoded line
(`# paper-trading-platform` with NUL bytes); it is removed while the file is being edited.

## Risks / Trade-offs

- **`curl | sudo bash` runs unreviewed code as root** → The docs give the two-step
  alternative first-class treatment (`git clone`, read `deploy/install.sh`, run it), and
  the script is written to be short enough to read.
- **The build runs `npm ci` and `uv sync` as root**, so package install scripts run as
  root → Both install from committed lockfiles with integrity hashes (`npm ci`,
  `uv sync --frozen`). Accepted in exchange for a code tree the service cannot write.
- **Plain HTTP on all interfaces by default**; passwords and session cookies cross the
  network unencrypted → Stated in the installer's final output and in the docs, with
  `--host 127.0.0.1` and a reverse-proxy recipe (`COOKIE_SECURE=true`). The installer
  opens no firewall port, so on a machine with a default-deny firewall nothing is exposed
  until the operator allows it.
- **Third-party installers (NodeSource, Astral) are a supply-chain dependency and may
  change** → Both are skipped when a suitable version is already installed, so an
  operator can pre-install from a source they trust.
- **`git reset --hard` discards local edits in `/opt/paper-trading/repo`** → Documented;
  configuration lives in `/etc`, so there is no reason to edit the tree.
- **`ProtectSystem=strict` could block a write nobody anticipated** (a library cache, for
  instance) → The end-to-end verification exercises a Yahoo-priced and a ccxt-priced
  asset and checks the journal for permission errors. `ReadWritePaths=` is the escape
  hatch.
- **An upgrade across a migration is one-way** → `AUTO_MIGRATE` upgrades the schema at
  start; rolling back the code does not roll back the database. The docs tell the
  operator to copy `/var/lib/paper-trading` before upgrading.
- **Cannot be tested on the development machine** (Windows) → Static checks locally
  (`bash -n`, `shellcheck` if available), behavior on a real Debian/Ubuntu machine, VM or
  systemd-capable container; the task list keeps those as explicit steps.

## Migration Plan

Nothing is deployed anywhere yet, so there is nothing to migrate.

- **Before merge**, the one-liner is exercised from this branch:
  `curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/add-headless-linux-deployment/deploy/install.sh | sudo bash -s -- --ref add-headless-linux-deployment`.
  This needs the branch pushed.
- **After merge**, the documented one-liner (from `main`, no options) works.
- **Rollback on a machine**: re-run the installer with `--ref <previous tag or commit>`,
  restoring the database copy if a migration ran in between.

## Open Questions

- Whether to tag releases so operators can pin `--ref v…` instead of following `main`.
  It changes only an example in the docs.
