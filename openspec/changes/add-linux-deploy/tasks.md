# Tasks: add-linux-deploy

## 1. systemd unit

- [ ] 1.1 Create `deploy/paper-trading.service` with placeholders for checkout path and service user: `WorkingDirectory=<checkout>/backend`, `ExecStart=<checkout>/backend/.venv/bin/uvicorn app.main:app --host ${HOST} --port ${PORT}` (direct venv binary, not `uv run` — the wrapper dies by SIGTERM on stop and leaves the unit "failed"), optional `EnvironmentFile=<checkout>/backend/.env`, `Restart=on-failure`, `After=network-online.target`, `WantedBy=multi-user.target` — verify the file parses with `systemd-analyze verify` on a Linux host (no errors after rendering placeholders)

## 2. Install script

- [x] 2.1 Create `deploy/install.sh` skeleton: `set -euo pipefail`, requirement checks (bash, curl, git, sudo, systemd via `systemctl --version`) that stop with a clear message naming what is missing — verify on a Linux host that running it on a machine without systemd exits non-zero before changing anything and prints that systemd is required
- [ ] 2.2 Add self-location: when not inside a project checkout (the curl case), clone `https://github.com/michaexel-copilot/paper-trading-platform.git` to `~/paper-trading-platform` (or `git -C` pull if it exists) and re-exec the checkout's `install.sh`; when inside a checkout, `git pull` unless `DEPLOY_REF` is set, then check out that ref — verify both paths: `curl | bash` on a fresh machine ends up running from `~/paper-trading-platform`, and running `deploy/install.sh` inside a clone updates it in place
- [ ] 2.3 Add prerequisite installation: install uv via the official astral.sh installer when missing; install Node.js 20+ via NodeSource on Debian/Ubuntu when missing or too old, otherwise stop with a clear message; skip both when already present — verify on a fresh Ubuntu machine that both get installed, and on a prepared machine that the step is a no-op
- [ ] 2.4 Add build steps: `uv sync` in `backend/`, `npm ci && npm run build` in `frontend/`; on any build failure exit non-zero before touching the service — verify by forcing a build error that the script fails and a previously running service is untouched
- [ ] 2.5 Add unit installation: render `deploy/paper-trading.service` placeholders (checkout path, current user, resolved uv path, `HOST`/`PORT` defaulting to `0.0.0.0`/`8000`) with `sed` into `/etc/systemd/system/paper-trading.service` via sudo, `systemctl daemon-reload`, then `systemctl enable --now` on first install or `systemctl restart` on update — verify `systemctl status paper-trading` reports active and the UI answers on the configured port
- [ ] 2.6 Make the script idempotent end-to-end — verify running it twice in a row completes both times, leaves one checkout, one unit and one running backend process (`pgrep -fa uvicorn` shows a single process), and preserves `backend/.env` and `backend/data/`

## 3. Start/stop scripts

- [ ] 3.1 Create `deploy/start.sh`: if `systemctl is-active --quiet paper-trading`, print that the service is already running and exit 0; otherwise `systemctl start` (with sudo fallback) and wait until the health endpoint answers or a timeout fails with the journal tail — verify start-when-stopped brings the UI up and start-when-running keeps exactly one backend process
- [ ] 3.2 Create `deploy/stop.sh`: `systemctl stop paper-trading` (with sudo fallback) and report the resulting state — verify the port stops answering and `systemctl status` shows inactive
- [x] 3.3 Make all four `deploy/` scripts executable and `bash -n`/`shellcheck`-clean where available — verify `bash -n deploy/*.sh` passes and the files have the executable bit in git (`git ls-files -s deploy`)

## 4. Documentation

- [x] 4.1 Write `deploy/README.md`: the one-liner (with the inspect-first alternative), supported targets and manual prerequisite path, configuration via `backend/.env` (`HOST`, `PORT`, and the app's documented settings), start/stop scripts and `systemctl` equivalents, logs via `journalctl -u paper-trading -f`, updating by re-running the one-liner, uninstall steps, and the security note (0.0.0.0 binding, firewall, HTTPS/`COOKIE_SECURE` follow-up) — verify a reader can go from fresh machine to running service using only this document
- [x] 4.2 Add a short "Deploy on a Linux server" section to the root `README.md` pointing at the one-liner and `deploy/README.md` — verify the link resolves and the section matches the documented flow

## 5. End-to-end verification on a Linux host

- [ ] 5.1 Fresh-machine run: on a clean supported VM, run the one-liner and confirm the UI answers on the configured port, `systemctl status paper-trading` is active and enabled — covers spec scenarios "Fresh machine" and "Defaults"
- [ ] 5.2 Reboot the VM and confirm the service comes up on its own — covers "Boot persistence"
- [ ] 5.3 Kill the backend process (`sudo pkill -f uvicorn`) and confirm systemd restarts it — covers "Crash restart"
- [ ] 5.4 Create an account, a portfolio and a trade; set a custom `PORT` in `backend/.env`; re-run the installer; confirm the service runs the new revision on the custom port with all data intact — covers "Update preserves data" and "Custom port survives update"
- [ ] 5.5 Record the verification (host OS, date, outcome) in the change or as a PR note so the manual-test gap is visible to reviewers
