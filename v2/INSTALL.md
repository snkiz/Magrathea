# Installing MCS (v2)

MCS runs several Minecraft servers on one Linux machine under systemd, and gives you a web page
(and some commands) to manage them: create servers, add mods, back them up, restore them.
It is a package manager for servers, not a launcher: you decide what runs.

**Tested on:** Ubuntu 26.04 under WSL2, Python 3.14, systemd 259. Other Debian/Ubuntu versions should
work but have not been tried. A physical machine (Debian) is the next thing to test.

## What you need

- A Linux machine with **systemd** and a normal user who can use **sudo**.
- Internet access (Minecraft downloads, Mojang name lookups, Modrinth/CurseForge).
- These packages (Debian/Ubuntu names):

  ```
  sudo apt install python3 sudo polkitd acl openjdk-21-jre-headless
  ```

  - `polkitd`: without it the web page cannot start or stop servers. A desktop system (Wayland or X11)
    already has it, along with `pkexec`; MCS only needs the daemon, not `pkexec`. The installer notices
    when it is missing and says so.
  - `acl`: optional, keeps new files in the right group automatically.
  - **Java**: Minecraft 1.20.5 and newer needs Java 21. Older versions need Java 17 (1.18 to 1.20.4)
    or older. Install the Java a version needs before you start it; MCS lists the ones it finds in
    `/usr/lib/jvm` on the server's Settings tab. (Debian 12 only packages Java 17; for newer Minecraft use
    Debian 13 or a newer Ubuntu, or install Java 21 from another source such as Adoptium.)
- Python 3 only, nothing from pip. (Developed and tested with 3.14; older versions are untested.)

## Install

1. Put the MCS folder on the machine (clone it or copy it) and go into `v2`:

   ```
   cd MCS/v2
   ```

2. Run the installer **as your normal user** (it asks for sudo itself; do not run it as root):

   ```
   ./MCSuser
   ```

   It creates a `minecraft` system user, installs the MCS program in `/opt/mcs` (owned by root) and
   creates `/opt/minecraft` (owned by `minecraft`) for your servers and backups. It installs the systemd
   units, the polkit rule and the `MCS...` commands, turns on the backup timer, and adds you to the
   `minecraft` group. Read it first if you like; it is commented. It is safe to run again: it
   replaces the program and leaves your servers and backups alone.

3. **Log out and back in** so your new group membership takes effect.

## Start the web page

```
sudo systemctl enable --now MCSweb
sudo MCSkey add <your name>
```

`MCSkey` prints a key **once**. Only a hash is kept, and only root can make or revoke keys
(`sudo MCSkey list`, `sudo MCSkey revoke <name>`, `sudo MCSkey audit`). Make one per person.

Open <http://localhost:8080> and paste the key. The page listens on `127.0.0.1` only. To reach it from
another computer, use an SSH tunnel, for example:

```
ssh -L 8080:127.0.0.1:8080 you@the-server
```

then open <http://localhost:8080> on your own machine. Do not put it on the internet as it is: there is
no HTTPS yet.

## Your first server

1. **+ New server**: a name, your Minecraft name (you become the first whitelisted player and an operator),
   a port (each server needs its own), optionally a world seed, and accept the Minecraft EULA.
2. **Mods & jar** tab: pick *vanilla*, *paper* or *fabric* and a version, then **Download**.
3. Press **Start**. The first start takes a while (it downloads and builds the world).
4. Friends join with `server-address:port` once their names are on the whitelist (**Settings** tab, *Players*).
   The whitelist is on by default.

## Backups (on by default)

Every server is backed up at 04:00 each day: players are warned, the server stops, is copied and
checked, and starts again. The newest 7 daily and 4 weekly backups are kept. Change all of this on
the **Backups** tab, where you can also **Test** a backup (unpacks it into a scratch folder and checks it,
without touching the server) or **Restore** one (your current state is saved first so you can undo).

Backups on the same disk are not enough: if the disk dies they die with it. Keep a separate copy:

```
sudo MCSoffbox set /path/to/folder/on/another/drive
```

It copies new backups there every hour and never deletes anything. It refuses a folder the
`minecraft` user could change, and a folder on the same disk as the servers (add `--same-disk-ok`
for a trial). `sudo MCSoffbox status` shows how it is going; the page shows a notice until it is set up.

## Commands

`MCShelp` lists everything. The ones you will use: `MCScreate`, `MCSfetch` (jars and mods),
`MCSbackup`, `MCSconf`, `MCSbrowse`, `MCSkey`, `MCSoffbox`. Servers are ordinary systemd units:

```
systemctl status MCS@<server>
journalctl -u MCS@<server> -f      # the console
```

## Where things are

| Path | What |
| --- | --- |
| `/opt/mcs` | the MCS program: commands, web page, tools (owned by root; nothing the `minecraft` user runs can change it) |
| `/opt/minecraft/instance/<server>` | each server's files |
| `/opt/minecraft/backups/instance` | the backups |
| `/opt/minecraft/.config/MCS` | MCS settings |
| `/etc/MCS` | login keys and the off-box settings (root-owned) |

## Who can do what (so you know what to trust)

- Only root can make or revoke login keys and set up the off-box copy.
- Everyone with a key has the same access in the web page. There are no helper roles.
- Mods and plugins run as the `minecraft` user, and so does the web page. Anything they can read or
  delete on this machine, a mod can too, including the on-box backups. That is
  the reason for the separate copy, and for only installing mods you trust.
- The MCS program in `/opt/mcs` is owned by root. A mod, a helper in the `minecraft` group or the web page
  can read and run it but cannot change it, so a mod cannot rewrite the web page's code or the key tool.

## Updating

Pull the new files and run `./MCSuser` again. It replaces `/opt/mcs`; servers and backups are left
alone. An install from before the program and data were separated (the program used to sit inside
`/opt/minecraft`) is cleaned up automatically.

## There is no uninstaller yet

Removing MCS means stopping the `MCS@*` units, deleting `/opt/mcs`, `/opt/minecraft`, `/etc/MCS`,
the `MCS*` files in `/usr/local/bin` and `/etc/systemd/system`, and
`/etc/polkit-1/rules.d/50-mcs.rules`. The `teardown` step in `tests/wsl-full.sh` does exactly that, and
**deletes the servers**, so only use it on a test machine.

## Known rough edges

- Light mode is a placeholder; dark mode is the designed one.
- CurseForge search and downloads are not available yet. Modrinth works; for a CurseForge-only mod,
  download it from their site and upload the jar on the Files tab.
- Not done yet: NeoForge/Forge, HTTPS and internet-facing setup, a one-click "new world with this seed".

## Tests

`tests/wsl-*.sh` are the test scripts. They create and delete servers and use a real `minecraft` user, so
run them on a throwaway machine, as root: `sudo bash tests/wsl-full.sh install` first, then for example
`sudo bash tests/wsl-backup.sh`. `wsl-offbox-disk.sh` makes a small loop-mounted disk to test the separate copy.
Read `tests/README.md` first: `teardown` deletes the `minecraft` user and every server and backup.
