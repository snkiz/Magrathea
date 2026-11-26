# Tests

These scripts check MCS on a real machine. They are **not** unit tests and they are **not safe on a
machine you care about.**

**Run them only on a throwaway Linux machine with systemd** (a VM, or a disposable WSL distro), as root.
They create and delete the real `minecraft` user, `/opt/minecraft`, the systemd units and test servers.
`wsl-full.sh teardown` removes the `minecraft` user and everything under `/opt/minecraft`, **including
worlds and backups**. Never run any of them where you have servers you want to keep.

The `wsl-` in the names is historical. They run on any disposable systemd machine, not only WSL.

## Order

```
sudo SUDO_USER=$USER bash tests/wsl-full.sh install     # install MCS the normal way
sudo SUDO_USER=$USER bash tests/wsl-full.sh create      # make the 'smoke' server
sudo SUDO_USER=$USER bash tests/wsl-full.sh fetch       # download a Fabric server and one mod
bash tests/wsl-web.sh                                   # the web API (as a user in the minecraft group)
sudo bash tests/wsl-backup.sh                           # backups, restore, retention, interruption
sudo bash tests/wsl-offbox.sh                           # the separate copy
sudo bash tests/wsl-modcheck.sh                         # the mod pre-check
sudo bash tests/wsl-remote.sh                           # RCON and the management server stay off unless allowed
sudo SUDO_USER=$USER bash tests/wsl-full.sh teardown    # remove everything again
```

`wsl-offbox-disk.sh` makes a small loop-mounted disk to test the separate copy on a real second device.
`remote-control.py` and `api-boundary.py` need only Python and are safe to run anywhere:
`python3 -I tests/remote-control.py`.

The scripts print `PASS` and `FAIL` lines and end with a count; read the `FAIL` lines first.

## Status

- Written for the current layout, a single `minecraft`-owned `/opt/minecraft`. When the program and data
  trees are split, the paths in these scripts will need updating.
- Not run automatically. There is no CI for them yet: a normal CI runner is not a disposable systemd
  machine with a real `minecraft` user.
- Not yet run on real hardware (a physical USB drive, a network share), on Debian, or in a browser.
