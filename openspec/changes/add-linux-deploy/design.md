# Design: add-linux-deploy

## Context

See proposal.md — Why. Relevant current state:

- Production run mode already exists: `npm run build` in `frontend/`, then `uv run uvicorn app.main:app` in `backend/` serves API and built UI together on one port (default 8000).
- Exactly one backend process may run: a `fcntl.flock` advisory lock (`backend/data/backend.lock`) is held for the process lifetime and released by the kernel when the process dies — so crash restarts cannot leave a stale lock, but a second concurrent process fails fast.
- All runtime settings come from environment variables or `backend/.env` (pydantic-settings, see root README — Settings).
- The repo is public: `https://github.com/michaexel-copilot/paper-trading-platform.git`, so raw file URLs and unauthenticated `git clone` work for the one-liner.

## Goals / Non-Goals

**Goals:**

- One `curl | bash` command from fresh machine to running service; same script doubles as the updater.
- Everything lives under `deploy/` in the repo; no application code changes.
- Service managed by systemd; operator-facing start/stop scripts; docs in `deploy/README.md`.

**Non-Goals:**

- Docker/containers, Kubernetes, or any orchestration.
- Reverse proxy, TLS termination, or HTTPS hardening (documented as a follow-up consideration; `COOKIE_SECURE` already exists for when TLS is added).
- Multi-worker or multi-instance setups (forbidden by the single-process lock).
- Non-systemd init systems, macOS/Windows deployment, CI pipelines for the scripts.
- Automatic OS firewall configuration (documented instead).

## Decisions

### Plain bash + systemd, no containers

The request asks for scripts and systemctl files; the app has one build step (frontend) and one process (backend), so a container adds a dependency without simplifying anything. Alternative considered: a Dockerfile with docker-compose — rejected as out of scope and a heavier prerequisite than uv + Node.

### One self-locating `install.sh` for install and update

`deploy/install.sh` detects whether it runs inside a git checkout of the project:

- **Piped via curl** (no checkout): clones the repo to `~/paper-trading-platform` (or pulls if the clone exists), then re-executes itself from the checkout.
- **Run from a checkout**: operates in place — `git pull` (unless `DEPLOY_REF` pins a ref), build, install, restart.

One script for both paths keeps the one-liner and the update flow identical, satisfying "one-command installation" and "updating a deployment" with the same code path. Alternative considered: separate bootstrap and update scripts — rejected; two scripts to keep in sync for one job.

Steps inside: require systemd (`pidof systemd` / `systemctl --version`) → ensure uv (official astral.sh installer) → ensure Node 20+ (NodeSource setup on Debian/Ubuntu; otherwise stop with a clear message) → `uv sync` → `npm ci && npm run build` → render and install the unit → `systemctl enable --now` (or `restart` when updating).

### System-level unit, service runs as the installing user

`deploy/paper-trading.service` is a system unit (`/etc/systemd/system/`) with `User=` set to the user who ran the installer and the checkout owned by that user. Boot persistence without `loginctl enable-linger`, and no root-owned application process. sudo is used only for writing the unit and `systemctl` calls. Alternative considered: a dedicated `papertrading` system user — rejected; adds user/permission juggling for zero benefit on a single-purpose deployment. User units — rejected; need lingering and behave oddly on headless servers.

Unit shape (key directives):

- `WorkingDirectory=<checkout>/backend`, `ExecStart=<uv> run uvicorn app.main:app --host ${HOST} --port ${PORT}`
- `EnvironmentFile=<checkout>/backend/.env` (optional) — reuses the existing settings mechanism, so operator configuration survives updates because the installer never touches `backend/.env` or `backend/data/`.
- `Restart=on-failure`, `After=network-online.target`, `WantedBy=multi-user.target`.
- Defaults: `HOST=0.0.0.0`, `PORT=8000` (headless machine must be reachable from other hosts; overridable via the env file).

The checked-in unit uses placeholders; `install.sh` renders them (checkout path, user, uv path) with `sed` into `/etc/systemd/system/paper-trading.service` — no new templating dependency.

### Start/stop scripts wrap systemctl

`deploy/start.sh` and `deploy/stop.sh` are thin wrappers (`systemctl start|stop paper-trading`, falling back to `sudo` when not root). `start.sh` checks `systemctl is-active` first and reports "already running" instead of blindly restarting — this plus the flock guarantees no second process. They exist because the spec promises operators don't need to memorise the unit name; they intentionally do not duplicate any install logic.

### Logs and operations via the journal

stdout/stderr go to journald; docs use `journalctl -u paper-trading -f`. No log files, no rotation config.

## Risks / Trade-offs

- [curl | bash requires trusting the repo and the pipe] → Docs show the inspect-first alternative (download, read, run); the script is idempotent and safe to re-run.
- [NodeSource only covers Debian/Ubuntu; other distros fail the Node step] → Installer stops with a clear message naming the missing prerequisite; docs list supported targets and the manual prerequisite path.
- [0.0.0.0 binding exposes an unauthenticated-registration app on the network] → Docs call this out, note the firewall/port-forwarding consideration and the HTTPS/`COOKIE_SECURE` follow-up; default chosen because a headless server is otherwise unreachable.
- [A failed update (e.g. broken build) could leave the service down] → Build happens before the unit is touched; on build failure the script exits non-zero and the old service keeps running.
- [Concurrent installer runs could race] → Documented as unsupported; the backend flock limits the blast radius to a failed second start.

## Migration Plan

New capability, nothing to migrate. Rollback of a deployment: `deploy/stop.sh`, `sudo systemctl disable paper-trading`, remove the unit and (optionally) the checkout — documented in `deploy/README.md`.

## Open Questions

None.
