> **Status (2026-09-30):** every file in groups 1–5 is written. A task is ticked only
> when its stated verification has been done. The unticked tasks in groups 2–5 are
> implemented and pass `bash -n` and `shellcheck`, and the start/stop control flow was
> exercised against a stand-in backend on the development machine, but their Linux
> verification is still open, as is all of group 6 except 6.8. No Linux machine was
> available during implementation.

## 1. Repository groundwork

- [x] 1.1 Add `.gitattributes` forcing LF for `*.sh`, `*.service` and `*.env.example`; verify with `git check-attr eol deploy/install.sh` reporting `lf` once the file exists
- [x] 1.2 Create `deploy/paper-trading.env.example` with `APP_HOST`, `APP_PORT`, `DATABASE_URL` and `LOCK_FILE` pointing at `/var/lib/paper-trading`, `COOKIE_SECURE=false`, and the remaining README settings as commented defaults; verify every variable name matches a field in `backend/app/config.py` or is `APP_HOST`/`APP_PORT`

## 2. Start and stop scripts

- [ ] 2.1 Write `deploy/start.sh` with `--foreground` (exec the venv's uvicorn from `backend/` with `APP_HOST`/`APP_PORT`, defaults `127.0.0.1`/`8000`) and a clear error when `backend/.venv` is missing; verify `bash -n` passes and, on Linux, that `--foreground` serves `/api/health`
- [ ] 2.2 Add service mode to `start.sh`: when the unit is installed, `systemctl start` it (through `sudo` when not root) and wait for health; verify on a machine with the service installed that it exits 0 with the service active, and exits 0 without a second process when already running
- [ ] 2.3 Add standalone mode to `start.sh`: background start with PID file and log under `backend/data/`, wait for health, non-zero exit pointing at the log on timeout; verify the process survives closing the terminal and that a second run reports "already running"
- [ ] 2.4 Write `deploy/stop.sh`: `systemctl stop` in service mode; in standalone mode SIGTERM, wait up to 30 s, SIGKILL, remove the PID file, and treat a stale PID file as not running; verify the backend log shows the lifespan shutdown, that an immediate `start.sh` succeeds without a lock conflict, and that stopping twice exits 0
- [x] 2.5 Mark both scripts executable in git (`git update-index --chmod=+x`); verify `git ls-files -s deploy/` shows mode `100755`

## 3. systemd unit

- [ ] 3.1 Write `deploy/paper-trading.service` as in design decision 5, with the `@REPO@` placeholder; verify a rendered copy passes `systemd-analyze verify` on Linux

## 4. Installer

- [x] 4.1 Write the skeleton of `deploy/install.sh`: strict mode, step announcements, `ERR` trap naming the failed step, `--ref`/`--host`/`--port`/`--help` parsing; verify `bash -n` passes and `--help` prints usage and exits 0
- [ ] 4.2 Add the preflight (root, systemd as PID 1, `apt-get`, x86-64 or arm64); verify that a non-root run and a run in a container without systemd each exit non-zero with a specific message and create nothing under `/opt`, `/etc/paper-trading` or `/var/lib/paper-trading`
- [ ] 4.3 Add prerequisite installation: apt packages, Node.js 22 from NodeSource only when `node` is missing or older than 22.12, uv into `/usr/local/bin` only when missing; verify on a fresh machine that `node --version` and `uv --version` work afterwards and that a second run skips both
- [ ] 4.4 Add the service user and the code checkout into `/opt/paper-trading/repo` (clone, or fetch and hard-reset to the ref); verify the tree is at the requested ref for a branch, a tag and a commit hash
- [ ] 4.5 Add the builds: `uv sync --frozen --no-dev` with `UV_PYTHON_INSTALL_DIR=/opt/paper-trading/python`, then `npm ci` and `npm run build`; verify `sudo -u papertrading /opt/paper-trading/repo/backend/.venv/bin/python -c "import app.main"` succeeds from `backend/` and `frontend/dist/index.html` exists
- [ ] 4.6 Add environment file creation (only when absent, mode `0640 root:papertrading`, `--host`/`--port` applied, a notice when an existing file makes those options ignored); verify an edited file is byte-identical after a second run
- [ ] 4.7 Add unit rendering, `daemon-reload`, `enable`, `restart`, the health wait on the port from the environment file, the final summary with URL and the plain-HTTP notice, and the journal tail on timeout; verify `systemctl is-enabled` and `is-active` both succeed and the summary's URL loads
- [x] 4.8 Mark `install.sh` executable in git and run `shellcheck deploy/*.sh` where available; verify no warnings remain or each remaining one carries a justified `# shellcheck disable`

## 5. Documentation

- [ ] 5.1 Write `docs/deployment.md`: requirements, the one-liner and the read-first two-step alternative, options, the layout table, configuration, daily operation (`systemctl`, `journalctl`, start/stop scripts), upgrade and rollback, backup of `/var/lib/paper-trading`, HTTPS behind a reverse proxy with `COOKIE_SECURE=true` and `--host 127.0.0.1`, firewall note, removal, manual steps for unsupported distributions, troubleshooting; verify every command in it was run during section 6 and every path matches the scripts
- [x] 5.2 Add a "Deploy on a Linux server" section to `README.md` with the one-liner and a link to `docs/deployment.md`, and remove the stray UTF-16 line at the end of the file; verify `git diff README.md` shows only those two changes and the file has no NUL bytes

## 6. End-to-end verification on Linux

- [ ] 6.1 Push the branch and run the one-liner with `--ref add-headless-linux-deployment` on a fresh Debian 12 or Ubuntu 22.04+ machine, VM or systemd container; verify exit 0, `curl http://127.0.0.1:8000/api/health` returns `{"status":"ok"}`, and the UI loads from another machine
- [ ] 6.2 In the UI, create an account and a portfolio and look up one crypto and one stock quote; verify both prices appear and `journalctl -u paper-trading` shows no permission errors
- [ ] 6.3 Verify supervision: the process runs as `papertrading`; after `kill -9` of the main PID the service is active again within 10 s; after a reboot the health endpoint answers without operator action
- [ ] 6.4 Verify graceful stop: `systemctl stop` completes well inside 30 s, the journal shows a clean uvicorn shutdown, and `systemctl start` succeeds
- [ ] 6.5 Verify upgrade: edit the port in the environment file, re-run the installer, and confirm the account from 6.2 can still log in, the environment file is unchanged, and the health check used the new port
- [ ] 6.6 Verify recovery: delete `/opt/paper-trading/repo`, re-run the installer, and confirm the account and portfolio are still there
- [ ] 6.7 Verify `deploy/start.sh` and `deploy/stop.sh` in service mode on that machine, and in standalone mode from a separately built checkout on a free port
- [x] 6.8 Run `openspec validate add-headless-linux-deployment --strict`; verify it passes
