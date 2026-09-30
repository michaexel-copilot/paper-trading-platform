## Purpose

Lets an operator install, run, supervise and upgrade the paper trading platform on a
headless Linux machine with one command, and documents how to operate it there.

## ADDED Requirements

### Requirement: One-command installation
The project SHALL provide an installer that takes a supported machine with nothing but a
network connection and root access from no installation to a running platform in a
single command, without interactive prompts. Supported machines are Debian 12 or newer
and Ubuntu 22.04 or newer, with systemd.

#### Scenario: Fresh machine
- **WHEN** an operator runs the documented one-line command as root on a supported machine that has never had the platform installed
- **THEN** the installer installs the missing prerequisites, fetches the code, builds the backend environment and the web UI, installs and starts the service, and exits with status 0
- **AND** the platform's health endpoint answers successfully on the configured port before the installer exits
- **AND** the installer prints the address the UI is reachable at and the commands to check status and logs

#### Scenario: Unsupported machine
- **WHEN** the installer runs on a machine without systemd or without a supported package manager
- **THEN** it exits with a non-zero status and a message naming what is missing, before changing anything on the machine

#### Scenario: Not run as root
- **WHEN** the installer runs without root privileges
- **THEN** it exits with a non-zero status and tells the operator to run it with `sudo`, before changing anything on the machine

#### Scenario: A step fails
- **WHEN** any installation step fails, for example a download or the frontend build
- **THEN** the installer stops at that step, exits with a non-zero status and reports which step failed

#### Scenario: Service does not become healthy
- **WHEN** the service is started but the health endpoint does not answer within the wait period
- **THEN** the installer exits with a non-zero status and shows the most recent service log lines

### Requirement: Installer options
The installer SHALL accept options for the git ref to deploy, the listen address and the
listen port, with defaults of the repository's default branch, all interfaces and port
8000.

#### Scenario: Defaults
- **WHEN** the installer runs with no options on a fresh machine
- **THEN** the default branch is deployed and the service listens on port 8000 on all interfaces

#### Scenario: Chosen ref and port
- **WHEN** the installer runs with a ref and a port option
- **THEN** that ref is deployed and the service listens on that port

### Requirement: Repeatable installation and upgrade
Running the installer on a machine that already has the platform SHALL upgrade it in
place to the requested ref and SHALL preserve the database and the operator's
configuration.

#### Scenario: Upgrade keeps data and configuration
- **WHEN** the installer runs again on a machine with an existing installation, accounts and portfolios, and an edited environment file
- **THEN** the code and builds are updated to the requested ref and the service is restarted
- **AND** the existing accounts, portfolios and trades are still present
- **AND** the environment file is unchanged

#### Scenario: Re-run with nothing new
- **WHEN** the installer runs again and the requested ref is already deployed
- **THEN** it completes with status 0 and the service is running afterwards

### Requirement: Supervised service
The platform SHALL run as a systemd service under a dedicated unprivileged user. The
service SHALL start at boot, SHALL be restarted when it exits unexpectedly, and SHALL be
controllable with the standard `systemctl` verbs.

#### Scenario: Survives a reboot
- **WHEN** the machine reboots after installation
- **THEN** the service starts without operator action and the health endpoint answers

#### Scenario: Restarted after a crash
- **WHEN** the service process is killed
- **THEN** systemd starts it again and the health endpoint answers again

#### Scenario: Not running as root
- **WHEN** the service is running
- **THEN** its process is owned by the dedicated service user, not by root

#### Scenario: Controlled with systemctl
- **WHEN** an operator runs `systemctl stop`, `start`, `restart` or `status` on the service
- **THEN** the service stops, starts, restarts or reports its state accordingly

#### Scenario: Logs in the journal
- **WHEN** the service writes log output
- **THEN** the output is readable with `journalctl` for the service unit

### Requirement: Graceful shutdown
Stopping the platform SHALL let the backend finish its shutdown sequence, so that the
order matcher stops and the database and lock file are released, before the process is
forced to end.

#### Scenario: Clean stop
- **WHEN** the service is stopped
- **THEN** the backend exits on its own within the stop timeout and the next start succeeds without a lock conflict

#### Scenario: Hung process
- **WHEN** the backend does not exit within the stop timeout
- **THEN** it is forcibly ended and the stop is reported as complete

### Requirement: Start and stop scripts
The project SHALL provide a start script and a stop script. When the systemd service is
installed they SHALL control that service. When it is not, they SHALL start the platform
as a background process and stop that same process. The start script SHALL report
success only once the health endpoint answers.

#### Scenario: Start with the service installed
- **WHEN** an operator runs the start script on a machine with the service installed
- **THEN** the systemd service is started and the script exits with status 0 once the health endpoint answers

#### Scenario: Stop with the service installed
- **WHEN** an operator runs the stop script on a machine with the service installed
- **THEN** the systemd service is stopped

#### Scenario: Start without systemd
- **WHEN** an operator runs the start script from a built checkout on a machine without the service installed
- **THEN** the platform runs in the background, detached from the terminal, and the script reports where its log is written

#### Scenario: Stop without systemd
- **WHEN** an operator runs the stop script after a background start
- **THEN** that process is asked to shut down and the script returns once it has exited

#### Scenario: Already running
- **WHEN** the start script runs while the platform is already running
- **THEN** it reports that the platform is running and exits with status 0 without starting a second process

#### Scenario: Already stopped
- **WHEN** the stop script runs while the platform is not running
- **THEN** it reports that nothing is running and exits with status 0

#### Scenario: Fails to start
- **WHEN** the platform does not become healthy after the start script launches it
- **THEN** the script exits with a non-zero status and points to the log

### Requirement: Configuration outside the code
The deployed service SHALL read its settings from one environment file outside the code
directory, and SHALL keep its database outside the code directory. The installer SHALL
create the environment file with documented defaults when it does not exist and SHALL
never overwrite an existing one. The file SHALL be readable only by root and the service
user.

#### Scenario: Setting changed by the operator
- **WHEN** an operator changes a setting in the environment file and restarts the service
- **THEN** the service runs with the new value

#### Scenario: Listen port changed
- **WHEN** an operator changes the listen port in the environment file and restarts the service
- **THEN** the platform answers on the new port and no longer on the old one

#### Scenario: Code directory replaced
- **WHEN** the code directory is deleted and the installer is run again
- **THEN** the platform comes back with its previous accounts, portfolios and settings

### Requirement: Deployment documentation
The project SHALL document deploying and running the platform on a remote machine: the
one-line install and its options, what the installer puts where, how to configure it,
how to start, stop, restart and check it, how to read its logs, how to upgrade, how to
back up the database, how to serve it over HTTPS, how to remove it, and the manual steps
for machines the installer does not support. The README SHALL link to this document.

#### Scenario: Operator follows the document
- **WHEN** an operator with SSH access to a fresh supported machine follows the document from the top
- **THEN** they reach a running platform and can open the UI from another machine without consulting any other source

#### Scenario: Plain HTTP is called out
- **WHEN** an operator reads the installation section
- **THEN** it states that the default installation serves plain HTTP on all interfaces and points to the HTTPS section
