## Purpose

Takes a fresh headless Linux machine to a running, boot-persistent paper-trading service with a single command, and makes that service operable with standard systemctl tooling.

## ADDED Requirements

### Requirement: One-command installation
The system SHALL provide a single command that takes a fresh headless Linux machine — needing only curl, bash, git and sudo access — to a running paper-trading service: it checks out the repository, installs missing prerequisites, builds the frontend, installs the service and starts it. The same command SHALL also work when run again on an existing deployment.

#### Scenario: Fresh machine
- **WHEN** an operator runs the documented one-liner on a fresh headless Linux machine with systemd
- **THEN** the repository is checked out, the frontend is built, the service is installed and started, and the platform's web UI answers on the configured port

#### Scenario: Re-run on an existing deployment
- **WHEN** the install command is run again on a machine that already has a deployment
- **THEN** it completes without error, does not create a second checkout or service, and leaves the service running

### Requirement: Prerequisite setup
The installer SHALL install the runtime prerequisites (uv, Node.js 20 or newer) when they are missing, and SHALL use existing installations when present. When the machine cannot be supported — no systemd, or no supported way to install a prerequisite — the installer MUST stop with a clear message stating what is missing, and MUST NOT leave a half-installed service.

#### Scenario: Missing prerequisites
- **WHEN** the installer runs on a machine without uv and Node.js
- **THEN** it installs them and proceeds with the deployment

#### Scenario: Unsupported machine
- **WHEN** the installer runs on a machine without systemd
- **THEN** it stops before changing the system and reports that systemd is required

### Requirement: systemd-managed service
The deployment SHALL install a systemd unit that runs the backend as a single process, starts it automatically on boot, and restarts it when it exits unexpectedly. The service SHALL be manageable with the standard systemctl commands start, stop, restart and status, and its logs SHALL be available through the journal.

#### Scenario: Boot persistence
- **WHEN** the machine reboots after a successful installation
- **THEN** the service starts automatically and the platform is reachable without any manual step

#### Scenario: Crash restart
- **WHEN** the backend process is killed unexpectedly
- **THEN** systemd restarts it and it resumes serving without manual intervention

#### Scenario: Operator control
- **WHEN** an operator runs `systemctl stop`, `start`, `restart` or `status` for the service
- **THEN** each behaves as it does for any other systemd service

### Requirement: Start and stop scripts
The deployment SHALL ship start and stop scripts so an operator can start and shut down the service without memorising unit names. Starting an already-running service MUST NOT spawn a second backend process, and stopping SHALL shut the service down gracefully.

#### Scenario: Start when stopped
- **WHEN** an operator runs the start script while the service is stopped
- **THEN** the service starts and the platform becomes reachable

#### Scenario: Start when already running
- **WHEN** an operator runs the start script while the service is already running
- **THEN** exactly one backend process remains and the script reports the service is already running

#### Scenario: Graceful stop
- **WHEN** an operator runs the stop script while the service is running
- **THEN** the backend shuts down and the platform stops answering

### Requirement: Service configuration
The deployment SHALL work with no configuration beyond the defaults and SHALL let the operator override the listen address, the port and the application's documented environment settings for the installed service. Configuration changes SHALL take effect on service restart and MUST NOT be lost when the deployment is updated.

#### Scenario: Defaults
- **WHEN** the installer completes without any operator configuration
- **THEN** the service listens on the documented default address and port

#### Scenario: Custom port survives update
- **WHEN** an operator sets a custom port and later re-runs the installer to update
- **THEN** the updated service still listens on the custom port

### Requirement: Updating a deployment
Re-running the installer on an existing deployment SHALL update the checkout to the requested revision, rebuild the frontend, and restart the service. The update MUST preserve the database, all user data and the operator's configuration.

#### Scenario: Update preserves data
- **WHEN** an operator updates a deployment that contains accounts, portfolios and trades
- **THEN** after the update the service runs the new revision and all accounts, portfolios and trades are still present

### Requirement: Deployment documentation
The repository SHALL contain documentation for deploying and running on a remote machine that covers the one-liner, prerequisites, configuration, updating, viewing logs, and removing the deployment.

#### Scenario: Operator follows the documentation
- **WHEN** an operator with no prior knowledge of the project follows the deployment documentation on a supported machine
- **THEN** they reach a running service and can later update, inspect and remove it using only the documented commands
