# Magrathea

A manager for Minecraft servers, built on systemd.

**NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.**

Magrathea runs several Minecraft servers on one Linux machine and gives you a web page, and some commands, to manage
them. It is a step above a desktop launcher: built for one owner or a small group, and open enough to learn from. It
installs and manages. You decide what runs. The commands start with `MCS`.

**Status:** early. Tested on Ubuntu under WSL2, not yet on real hardware.

## Principles

1. Your server, your call. The person who runs a server decides what it does.
2. Magrathea installs and manages. It does not decide for you. It tells you what is risky and lets you choose.
3. Safety is a choice you make with the facts in front of you: whitelist on, backups kept somewhere safe, and plain
   words about the risks, because some players are kids.
4. Nothing hidden: what Magrathea downloads, from where, and what it changes on your machine is visible.
5. No lock-in: your worlds, mods and settings are ordinary minecraft installs in each instance you can take with you.
6. Simple on purpose. One server user, ordinary files, no Docker-style images: you can see how it works. That has limits, and
   the docs say what they are.
7. Meant to be learned from. Magrathea lets you see how Linux, permissions, web pages and small programs work, so you
   can fix it. The teaching is the point, and there is no charge.

## What it does

- Creates and runs vanilla, Paper and Fabric servers, each as its own systemd service.
- Downloads server jars, and mods from Modrinth.
- Turns the whitelist on by default.
- Gives each person their own login key for the web page. Only the owner (root) can make or revoke keys.
- Backs up every day, and lets you test and restore a backup.
- Copies backups to a second drive, once you set that up.
- Keeps Minecraft's own remote control off unless the owner allows it.

## Before you start

- You need Linux with systemd, and a user who can use `sudo`.
- The web page listens on your own machine only and has no HTTPS yet. **Do not put it on the internet.**
- Mods and plugins are code. They can reach what the server can reach, including the backups kept on the same
  machine. Only install mods you trust, and set up the separate backup copy.
- Everyone with a login key has the same access in the web page.

## Install

Follow [INSTALL.md](v2/INSTALL.md).

## Not planned

Not planned means not now. Things can change.

- Windows or Bedrock.
- Docker-style images. Others already do that well.
- Paid or multi-customer hosting.
- Installing mods on players' computers.

NeoForge, CurseForge, Hanger downloads, modpak support and HTTPS are not done yet.

## What it talks to

Magrathea downloads from Mojang, PaperMC, Fabric and Modrinth, and from CurseForge if that is set up. Their terms apply
to what you do with their services and files.

## How it was made

Much of the code, including the Python and the permission setup, was written with Claude Code. The design, the decisions
and the testing are the maintainer's. [Archive-v1](Archive-v1) is where the project started. It is the maintainer's own
work, kept for history. Do not install it.

### Why systemd

Few public projects use systemd for more than starting things at boot. Magrathea leans on it for most of the
plumbing, so there is less of its own code to read and trust. Each part below replaces something you would
otherwise write by hand.

- **One unit per server** (`MCS@.service`). `MCS@survival` is the server called survival. systemd runs it as the
  `minecraft` user, restarts it if it crashes, stops it cleanly and sends its output to the journal.
- **A console pipe** (`MCS@.socket`). Each server gets an input pipe that only the `minecraft` user can write to.
  Commands from the web page reach the server through it.
- **Timers** (`MCSbackup.timer`, `MCSoffbox.timer`). Scheduled backups and the off-box copy run from systemd
  timers.
- **The web page as a service** (`MCSweb.service`).
- **A polkit rule** (`MCS.rules`). It lets the web page start, stop and restart MCS server units and nothing
  else. It cannot enable units or touch any other service.
- **A key broker** (`MCScred.socket`, `MCScred.service`). See Security.

Considered, not used: socket activation, which starts a server on the first connection. It works in principle,
but a Minecraft server boots too slowly, so the first player's client would time out.

## Security

Keys for outside services are held by a key broker (`MCScred`); today that means CurseForge. It runs as a
throwaway user of its own, and systemd hands it the keys from a root-only folder, so the `minecraft` user, and any
mods, never see them. The web page and MCSfetch tell it what to ask over a local socket; it makes the request and
hands back the answer, never the key.

[SECURITY.md](SECURITY.md) says what was reviewed, what is still open, and how to report a problem.

## License

AGPL-3.0, see [LICENSE](LICENSE). The bundled Shantell Sans font has its own license, see
[NOTICE.txt](v2/mcs/lib/MCS/web/fonts/NOTICE.txt).

Magrathea is not affiliated with or endorsed by Mojang, Microsoft, Modrinth or CurseForge.
