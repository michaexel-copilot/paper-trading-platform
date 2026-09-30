# Proposal: add-linux-deploy

## Why

Running the platform today means two terminals, `uv` and Node installed by hand, and no way to keep it alive on a server. There is no supported path from a fresh headless Linux machine to a running, boot-persistent service. Deploying to a remote machine should take one command, and the service should be manageable with standard `systemctl` operations.

## What Changes

- Add a `deploy/` directory with everything needed to deploy and run on a headless Linux host:
  - `install.sh` — idempotent deploy script: checks out or updates the repo, installs missing prerequisites (`uv`, Node.js 20+), builds the frontend, installs the systemd unit, starts the service. Designed to run as a one-liner via `curl | bash` on a fresh machine.
  - `paper-trading.service` — systemd unit running the backend as a single process (matching the lock-file constraint: no workers, no reload), restart on failure, start on boot.
  - `start.sh` / `stop.sh` — thin startup and shutdown wrappers around the systemd unit for manual control.
  - `README.md` — deploy and run documentation for a remote machine: the one-liner, configuration, updates, logs, uninstall.
- The one-liner from a fresh machine to a running service:

  ```sh
  curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash
  ```

- No changes to application code, the API, or existing behavior. Development workflow (two terminals, `npm run dev`) is untouched.

## Capabilities

### New Capabilities

- `linux-deployment`: Deploying and operating the platform on a headless Linux machine — one-command installation from a fresh host, systemd-managed service lifecycle (start, stop, restart, boot persistence, status, logs), manual start/stop scripts, configuration of the deployment, and updating an existing deployment.

### Modified Capabilities

(none)

## Impact

- **New files only**: `deploy/install.sh`, `deploy/paper-trading.service`, `deploy/start.sh`, `deploy/stop.sh`, `deploy/README.md`; a short deployment pointer added to the root `README.md`.
- **Target environment**: headless Linux with systemd (e.g. Ubuntu/Debian server), outbound internet access for market data. `sudo` is required for unit installation and `systemctl`.
- **No impact** on `backend/`, `frontend/` source, database schema, or the existing specs.
- **CI/tests**: none exist for shell scripts today; verification is a manual run on a Linux host (documented in tasks).
