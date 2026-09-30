# Deploying on Proxmox VE

This creates an LXC container on a Proxmox VE host and installs the platform in it with
the one-line installer. [deployment.md](deployment.md) explains the installer, the
configuration and daily operation; this page covers what is specific to Proxmox and
records the test run the deployment was verified with.

Commands marked `pve#` run on the Proxmox host as root, `ct#` inside the container.

## 1. Create the container

Pick a free container ID and make sure a template is there:

```sh
pve# pvesh get /cluster/nextid            # 108 in this walkthrough
pve# pveam list local                     # templates already downloaded
pve# pveam update && pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst   # if it is missing
```

Create and start it:

```sh
pve# pct create 108 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
       --hostname paper-trading \
       --cores 2 --memory 2048 --swap 512 \
       --rootfs local-lvm:8 \
       --net0 name=eth0,bridge=vmbr0,ip=dhcp \
       --unprivileged 1 --features nesting=1 \
       --onboot 1 --ostype ubuntu
pve# pct start 108
pve# pct exec 108 -- hostname -I          # the address it got
```

| Option | Why |
| --- | --- |
| `--cores 2 --memory 2048` | Comfortable for the frontend build. The running service uses about 250 MB. |
| `--rootfs local-lvm:8` | 8 GB on the `local-lvm` storage. The installed system uses 1.8 GB. |
| `--net0 …,ip=dhcp` | An address from the router. Use `ip=<addr>/24,gw=<gateway>` for a fixed one. |
| `--unprivileged 1` | Root in the container is not root on the host. |
| `--features nesting=1` | Needed for systemd in current distributions and for the sandboxing the service unit asks for. |
| `--onboot 1` | The container starts when the Proxmox host boots. |

A Debian 12 template (`debian-12-standard_…`) works the same way with `--ostype debian`.

## 2. Install the platform

The container templates have `wget` but no `curl`, and `pct exec` gives a root shell, so
no `sudo` is needed:

```sh
pve# pct exec 108 -- bash -c 'wget -qO- https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | bash'
```

This takes between one and two minutes and ends with the address of the UI. To deploy
another branch, tag or commit, append `-s -- --ref <ref>` after the last `bash`.

## 3. Open it

Open `http://<container address>:8000` from any machine on the network. Where the router
registers DHCP host names, `http://paper-trading:8000` works too.

The platform serves plain HTTP. On a home network that may be acceptable; otherwise see
[Serve it over HTTPS](deployment.md#serve-it-over-https).

## 4. Operate it from the Proxmox host

```sh
pve# pct exec 108 -- systemctl status paper-trading
pve# pct exec 108 -- journalctl -u paper-trading -n 50
pve# pct exec 108 -- systemctl restart paper-trading
pve# pct enter 108                        # a shell in the container
```

Upgrade by running the install command from step 2 again.

A snapshot before an upgrade gives you a way back that includes the database:

```sh
pve# pct snapshot 108 before-upgrade
pve# pct rollback 108 before-upgrade      # only if the upgrade went wrong
pve# pct delsnapshot 108 before-upgrade
```

## 5. Remove it

```sh
pve# pct stop 108
pve# pct destroy 108 --purge
```

This deletes the container with every account, portfolio and trade in it.

## Test record

The deployment was tested on 2026-09-30 from the branch
`add-headless-linux-deployment`.

| | |
| --- | --- |
| Proxmox host | `pve`, Proxmox VE 8.4.1, kernel 6.8.12-10-pve |
| Container 108 | `paper-trading`, Ubuntu 24.04 LTS, unprivileged LXC, 2 cores, 2 GB memory, 8 GB disk, systemd 255 |
| Container 109 | `paper-trading-deb12`, Debian 12, same sizing, systemd 252. Created for the test and destroyed afterwards. |
| Installed | Node.js 22.23.3, uv 0.12.21, Python 3.11.16 (uv-managed on Ubuntu; the system's 3.11.2 on Debian) |

### What was checked on container 108

| Check | Result |
| --- | --- |
| One-line install on the fresh container | Exit 0 after 78 s; health endpoint answered before the installer returned. |
| UI from another machine | `/` and a client-side route returned the app; `/api/health` returned `{"status":"ok"}`. |
| Account, portfolio, prices, a trade | Registered an account, created a portfolio, got a BTC quote from OKX and an AAPL quote from Yahoo Finance, bought 0.01 BTC at market. Done through the API the UI uses, not by clicking through the UI. |
| Asset availability check | All 50 seeded assets were marked available. |
| Permissions | No `Permission denied` or `Read-only file system` in the journal; the Yahoo cache landed in `/var/cache/paper-trading`. |
| Process owner | `papertrading`, not root. |
| Unit file | `systemd-analyze verify` reported nothing. |
| Crash | After `kill -9` systemd restarted the service; it answered again 10 s after the kill. |
| Reboot | After `pct reboot` the service answered 7 s later without any action. |
| Graceful stop | `systemctl stop` returned in 0.6 s; the journal shows the backend's own shutdown sequence; the next start had no lock conflict; the service stayed stopped until started. |
| Hung backend | With a stand-in process that ignores SIGTERM, `systemctl stop` returned after 30 s and systemd had killed it. |
| `start.sh` / `stop.sh` with the service | As root and as a non-root user with `sudo`: stop, stop again, start, start again all exited 0 and did the right thing; never more than one backend. |
| `start.sh` / `stop.sh` without systemd | From a second checkout built by an unprivileged user, on port 8100 while the service kept running: background start, survived the closed session, "already running", clean stop in 1 s, immediate restart, stop twice, a stale PID file, a port that already answers, `--foreground`. |
| Upgrade | Re-run took 11 s. With the port changed to 8080 in the settings file, the file was byte-identical afterwards, the health check used 8080, and the account, portfolio and trade were still there. `--port` on the command line was reported as ignored. |
| Code directory deleted | After `rm -rf /opt/paper-trading/repo` and a re-run (10 s), the data was intact. |
| Refs | A branch, a commit hash and a tag each deployed the right commit. |
| Refusals | Without root, and with systemd hidden, the installer exited 1 with a specific message and changed no file. |
| HTTPS | With `APP_HOST=127.0.0.1` and `COOKIE_SECURE=true` the port was closed from outside. Through Caddy with an internal certificate, and through nginx with a self-signed one and the documented `location` block, registration worked and the session cookie carried `Secure`. The SSH tunnel reached the platform too. |
| Backup, removal, manual installation | The commands in [deployment.md](deployment.md) were run as written: backup, removal (nothing left behind), installation by hand, then the one-line install again and a restore of the backup. |
| Proxmox snapshot | `pct snapshot` and `pct delsnapshot` worked on the container. |

### What was checked on container 109 (Debian 12)

The one-line install as root without `sudo` exited 0, the service ran as `papertrading`
on the system's Python 3.11, all 50 assets were marked available, the journal had no
permission errors, and `stop.sh` and `start.sh` worked.

### What the test found

- **The installer failed as root without `sudo`.** A root shell from `pct exec` has no
  `/usr/local/bin` on its `PATH`, so the installer did not find the `uv` it had just
  installed. Through `sudo` it worked. Fixed: the installer now adds `/usr/local/bin` to
  its `PATH`.
- **The container templates have no `curl`.** The documentation now gives the `wget`
  form next to the `curl` one.

### Not tested

- Ubuntu 22.04, arm64, and a virtual machine or bare-metal host instead of a container.
- The `ufw` command in deployment.md.
- A certificate for a real domain. HTTPS was tested with certificates browsers do not
  trust.
- A reboot of the Proxmox host itself, which `--onboot 1` is for.
- `pct rollback`.
- Standalone `stop.sh` having to kill a backend that does not exit.

After the test, the accounts and the trade it created were deleted, as were the proxies,
the test user and the temporary SSH key. Container 108 was left running a clean
installation.
