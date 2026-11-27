# CLAUDE.md: start here

Read this file first in every new chat. These files are the project's memory. Chats come and go; the files don't.
**If the files and your memory disagree, the files win.**

## The maintainer's rule

**I don't commit, push or post anything I can't understand.**

Every change Claude suggests comes with a plain-English explanation of what it
does, good enough for me to read it and decide. If I can't follow it, it waits.

Why: I don't trust Claude's output by default. Anything with my name on it is my
responsibility, even when Claude is the one who got it wrong. I'm here to learn and
get things done, not to have Claude do it for me (boilerplate aside).

## What this is

**Magrathea**: a manager for Minecraft servers, built on systemd. Linux and systemd only. The commands keep the `MCS`
prefix (`MCSuser`, `MCSweb`, and so on). Licensed AGPL-3.0. Repo: https://github.com/snkiz/Magrathea

Read `v2/docs/PRINCIPLES.md` for what the project is for and how it behaves, `v2/INSTALL.md` for how it is installed,
and `SECURITY.md` for the trust model and how to report a problem.

## Where this started

`Archive-v1/` is the first version of this project, and it is the maintainer's own work. It is kept to show where
Magrathea began. It is not supported: do not install it and do not change it. v2 grew from its ideas (a dedicated
`minecraft` user, a sandboxed systemd unit, and a socket for the console).

## How Claude is used here

Much of the code, including the Python and the permission setup, was written with Claude Code. The design, the
decisions about what Magrathea does and does not do, and the testing on real machines are the maintainer's. The README
carries the plain version of this for readers.

## How to work here

- Warn, don't block. Where something is risky, say so plainly (caution tape) and let the person running the server
  decide. Safe defaults matter because some players are children.
- Say what you could not verify. If you are going from memory, say so, and offer to read the source.
- Keep personal details and private names out of the repo: no real names beyond the GitHub handle, no hostnames,
  addresses, local paths or test machine details.
- Anything that touches login or keys, the web server and its routes, the downloader, file paths, the unit files, the
  sudo shims or permissions needs a security look before it goes in.
- The plan and notes are kept privately, outside this repo. Ask the maintainer for the current priorities.

## Issues: labels and milestones

Issues are drafted in chat and posted by the maintainer. Use the repo's own labels and milestones.

- Kind: `bug`, `enhancement`, `documentation`, `accessibility`.
- Area: `Backend`, `Backup`, `MCScommand`, `WebUI`, `Wiki`, `Packaging`.
- Triage: `duplicate`, `invalid`, `wontfix`, `question`, `help wanted`, `good first issue`.
- Milestones: Release (what must be done before going public), Packaging, Loadouts (mod lists), Modrinth API key (the
  operator's personal key), Geyser - Floodgate (one-click and easy), Paper server run (testing Paper in real use),
  RPM (includes distrobox testing for atomic distros), WebUI theming (palettes, seasonal themes, fonts).
