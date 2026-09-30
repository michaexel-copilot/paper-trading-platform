# Verification record: add-linux-deploy

- **Date:** 2026-09-30
- **Host:** Proxmox VE 8.4.1 node `pve`, LXC 107 (`papertrading-test`), Ubuntu 24.04 (noble), systemd 255, 2 cores / 2 GB RAM, unprivileged, DHCP on vmbr0
- **Method:** real one-liner from the branch — `curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/add-linux-deploy/deploy/install.sh | DEPLOY_REF=add-linux-deploy bash` — plus in-checkout runs of `deploy/install.sh`

## Results

| Scenario | Outcome |
| --- | --- |
| Fresh machine (one-liner: clone, uv + NodeSource Node 20 installed, build, unit, service) | PASS |
| Defaults (0.0.0.0:8000, service enabled + active, UI reachable from another host) | PASS |
| `systemd-analyze verify` on rendered unit | PASS |
| Re-run on existing deployment (6 runs total: fresh, updates, custom port, broken/fixed build) | PASS — idempotent: one checkout, one unit, one uvicorn process, `backend/.env` and `backend/data/` preserved |
| Prepared machine prerequisite step | PASS — no reinstalls (0 matches in update logs) |
| stop.sh / start.sh / start-when-running | PASS — clean `inactive` state (`Result=success`), "already running", single process |
| Boot persistence (`pct reboot`) | PASS — service active after boot |
| Crash restart (`pkill` SIGTERM and `pkill -9`) | PASS — systemd restarts, health OK, one process |
| Update preserves data (account + portfolio + real BTC trade via OKX before update; all intact after) | PASS |
| Custom port survives update (`PORT=9000` in `backend/.env`) | PASS — unit default re-baked to 8000, `.env` wins at runtime |
| Build failure leaves service untouched (`frontend/src` moved away → exit 2, service active; restored → exit 0) | PASS |

## Issues found during verification and fixed

1. `DEPLOY_REF` ignored on fresh clone in the bootstrap (commit "Honor DEPLOY_REF when cloning in the installer bootstrap").
2. `uv run` wrapper died by SIGTERM on `systemctl stop`, leaving the unit permanently "failed" — unit now runs the venv's uvicorn directly (commit "Run uvicorn directly from the venv in the unit so stops are reported clean").
3. `Restart=on-failure` did not restart a SIGTERM-killed process (uvicorn exits 0 on SIGTERM) — changed to `Restart=always` (commit "Restart=always: ...").
4. Health check used the install-time default port instead of the effective `backend/.env` port, failing updates with a custom port (commit "Health-check the effective port from backend/.env, not the install default").

## Notes

- The repo was private at first; the documented anonymous one-liner requires a public repo. The repo was made public during verification — the design's assumption now holds.
- npm warned that react-router 8.4 and vitest 5 request Node >= 22 while the project documents Node 20+; the build succeeds on Node 20. Worth a separate look (out of scope here).
