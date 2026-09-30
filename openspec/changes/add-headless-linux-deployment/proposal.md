## Why

The platform can only be started by hand today: the README describes two terminals on a
developer machine, and nothing installs, supervises or restarts the backend. Running it
on a remote headless Linux machine means repeating those steps over SSH, and the process
dies with the session or the next reboot.

## What Changes

- Add a `deploy/` directory with:
  - `install.sh` — an idempotent installer for a headless Debian or Ubuntu machine. It
    installs the prerequisites (git, uv, Node.js), clones the repository, builds the
    backend environment and the frontend, writes a default environment file, installs
    and enables the systemd service, starts it and waits until it answers.
  - `start.sh` and `stop.sh` — start and stop the platform. They drive the systemd
    service when it is installed and fall back to a background process with a PID file
    when it is not.
  - `paper-trading.service` — the systemd unit, run as a dedicated unprivileged user,
    restarted on failure and started at boot.
  - `paper-trading.env.example` — the documented environment file the service reads.
- A one-line install from a bare machine to a running service:
  `curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash`
- Re-running the installer upgrades an existing installation in place and keeps its data
  and configuration.
- Add `docs/deployment.md` covering installation, configuration, daily operation
  (`systemctl`, logs), upgrade, backup, putting it behind HTTPS, and removal. Link it
  from the README.
- Add `.gitattributes` so shell scripts and the unit file are always checked out with LF
  line endings; the repository is developed on Windows with `core.autocrlf=true`, and a
  CRLF script does not run on Linux.
- No application code changes. The backend already serves the built frontend, takes its
  database and lock file locations from settings, and shuts down cleanly on SIGTERM.

Assumptions recorded here rather than asked, because each has a conventional default and
is a setting, not a structural choice:

- Target is Debian 12+ or Ubuntu 22.04+ with systemd, on x86-64 or arm64. Other
  distributions get a clear refusal from the installer and manual steps in the docs.
- The service listens on `0.0.0.0:8000` over plain HTTP after the one-liner, so the UI is
  reachable from the network straight away. The docs explain how to restrict it to
  localhost and put a TLS reverse proxy in front.
- This branch is cut from `main` and is independent of the existing `add-linux-deploy`
  branch, which was not consulted.

## Capabilities

### New Capabilities

- `linux-deployment`: installing, starting, stopping, supervising and upgrading the
  platform on a headless Linux machine, and the operator documentation for it.

### Modified Capabilities

None. No requirement of an existing capability changes.

## Impact

- **New files**: `deploy/install.sh`, `deploy/start.sh`, `deploy/stop.sh`,
  `deploy/common.sh` (shared by the start and stop scripts),
  `deploy/paper-trading.service`, `deploy/paper-trading.env.example`,
  `docs/deployment.md`, `.gitattributes`.
- **Changed files**: `README.md` (a short "Deploy on a Linux server" section linking to
  the docs).
- **Application code**: none. `backend/` and `frontend/` sources are untouched.
- **On the target machine**: a system user `papertrading`; code under
  `/opt/paper-trading`; data under `/var/lib/paper-trading`; configuration in
  `/etc/paper-trading/paper-trading.env`; the unit
  `/etc/systemd/system/paper-trading.service`; `uv` in `/usr/local/bin`; Node.js 22 from
  NodeSource when the machine has none or an older one.
- **External dependencies at install time**: GitHub (clone, raw installer), astral.sh
  (uv), NodeSource (Node.js), PyPI and the npm registry. None at run time beyond the
  market-data sources the backend already uses.
