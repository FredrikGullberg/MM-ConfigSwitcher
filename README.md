# MM-ConfigSwitcher

Version 0.1.2 — a standalone configuration profile switcher for MagicMirror² on Linux with systemd user services.

One MagicMirror installation, multiple named profiles, independent config.js and CSS, one active profile at a time. No MagicMirror module or root privileges required. This is an initial version; test on your installation before relying on unattended switching.

## Architecture

The existing MagicMirror user service retains its ExecStart, display/Wayland settings and autostart. A managed systemd drop-in adds an EnvironmentFile with MM_CONFIG_FILE and, if specified, MM_CUSTOMCSS_FILE. The selected profile persists across reboot. A separate user service provides a small web interface even when MagicMirror is down.

Supports current MagicMirror versions with these environment variables and node --run config:check (tested target: v2.37; actual device integration not yet verified). Uses Python 3.10+ standard library, systemctl and Node.js already required by MagicMirror.

## Before installation

Inspect your existing service; do not share credentials from the output:

```sh
systemctl --user cat magicmirror.service
systemctl --user status magicmirror.service
ls -la ~/MagicMirror/config/
```

Check that the service starts your intended MagicMirror installation and does not override MM_CONFIG_FILE/MM_CUSTOMCSS_FILE in its start script or a later environment file. If you use another service, root directory or existing config/CSS path, supply the appropriate init flags. Configuration checking is executed with the installer's Node.js and environment, not the complete service environment; environment-dependent configs may need additional setup. The tool assumes profile config and CSS are trusted local files. Do not share real config files, since they may contain API credentials.

## Installation

Clone as the same normal user that runs MagicMirror (not root).

```sh
cd ~
git clone git@github.com:FredrikGullberg/MM-ConfigSwitcher.git
cd MM-ConfigSwitcher
bash install.sh --label "Staffanstorps JK" --health-port 8080
```

Private repository: the Pi needs authorized GitHub access. Port 8080 is an example; set the actual local HTTP port, or omit --health-port for systemd-only health checks (e.g. custom HTTPS). If config/custom.css is the file you want to explicitly select, add --css config/custom.css. Otherwise no CSS environment override is added to the baseline, and MagicMirror uses its configured CSS. Legacy css/custom.css installations may require manual migration or explicit selection to avoid MagicMirror moving the file automatically.

Install validates and records the existing default config as profile dojo, copies existing config/CSS to a private backup folder, creates its own drop-in, and starts only the switcher service. It does **not** restart MagicMirror. Review the configuration before switching. If installed without a custom path, files are under ~/.config/mm-configswitcher (or XDG_CONFIG_HOME). Include ~/.local/bin in PATH or use the full path to mm-switch.

### Symlinked configuration repositories

If MagicMirror's config/config.js or config/custom.css is a symbolic link to a trusted separate repository, explicitly allow its directory at initialization:

```sh
bash install.sh --label "Staffanstorps JK" --css config/custom.css --allow-root "$HOME/magicmirror-config"
```

Repeat --allow-root for additional trusted directories. Profile paths are still relative to MagicMirror; only resolved targets in MagicMirror or explicitly trusted directories are accepted. Existing links and their targets are not changed; the baseline backup copies file contents, not the links.

## Upgrade from 0.1.1

Run git pull, python3 -m unittest discover -v, then python3 switcher.py install. This migrates the exact old managed drop-in to an unquoted EnvironmentFile path and restarts only the switcher service. Existing profile settings and MagicMirror configuration are retained. Restart/switch MagicMirror explicitly afterwards. Version 0.1.2 checks the profile environment of the systemd MainPID during startup; status reports running_profile and environment_matches. This is not proof of Electron rendering or a check of separately scoped child processes.

## Adding an additional profile

First create the actual, valid config and CSS under your MagicMirror directory. Do not overwrite the dojo config. For instance:

```sh
cd ~/MM-ConfigSwitcher
python3 switcher.py add sm2027 --label "SM i Judo 2027" \
    --config config/config-sm2027.js --css config/custom-sm2027.css --health-port 8080
python3 switcher.py check sm2027
```

The referenced files must already exist. This project does not install the SM module or generate its MagicMirror configuration. Omit --health-port if needed. Profile configuration is stored locally in profiles.json; extra profiles can be added through the CLI.

## Switching via SSH

```sh
~/.local/bin/mm-switch status
~/.local/bin/mm-switch sm2027
~/.local/bin/mm-switch dojo
```

The tool validates both the target and previous profile, atomically changes the environment file, restarts the existing MagicMirror user service and checks stable systemd process identity for at least 8 seconds (45 second startup window by default). Optional local HTTP health check requires status 200 from the configured port. On detected failure, it attempts to restore and restart the previous profile. A pending record is retained if rollback fails or a transaction is interrupted; use mm-switch recover via SSH. This is not visual verification of Electron or proof that all modules render. Switching to the same profile restarts it.

## Web interface

The separate service binds ONLY to 127.0.0.1:8765 and requires a generated password. From the Pi, open http://127.0.0.1:8765. From a computer, use an SSH tunnel (replace pi-user and host):

```sh
ssh -N -L 8765:127.0.0.1:8765 pi-user@raspberrypi
```

Then open http://127.0.0.1:8765 on the computer. Username: admin. Retrieve the password privately on the Pi:

```sh
~/.local/bin/mm-switch password
```

The UI lists configured profiles, selected profile and service status; it provides authenticated POST requests with CSRF protection. It does not expose config contents, arbitrary shell commands or config upload. Direct LAN/mobile access is deliberately disabled by default; use a trusted HTTPS reverse proxy with access restrictions if needed. Do not port-forward the plain HTTP service to the internet. Selected profile is the desired persisted profile, not a guarantee of visible output.

## Logs, rollback and removal

```sh
journalctl --user -u mm-configswitcher.service -n 50 --no-pager
journalctl --user -u magicmirror.service -n 50 --no-pager
~/.local/bin/mm-switch recover
python3 switcher.py uninstall
systemctl --user restart magicmirror.service
```

Uninstall removes only the managed drop-in and switcher service, not the original service, configuration, backups or module files. The manual restart after uninstall restores the original service environment. The CLI symlink can be removed manually. For an existing headless user-service setup, check linger with loginctl show-user "$USER" -p Linger; do not change your working autostart without need.

## Tests

```sh
python3 -m unittest discover -v
```

Tests cover path checks, atomic writes, profile environment, successful switch and simulated failure with rollback. Real systemd, Wayland/Electron, web auth, and device startup are not covered by these unit tests. No runtime secrets or device profiles are stored in the repository.

Reference: https://docs.magicmirror.builders/configuration/introduction.html
