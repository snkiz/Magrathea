# Security

MCS serves a web page that can start, stop, back up and change Minecraft servers, so it was reviewed
on purpose. This is a short summary of what was checked, what held up, what is still open, and what
you should know before you use it.

**How it was reviewed.** The owner's own software was tested on throwaway machines (Ubuntu 26.04 under
WSL2) with an AI assistant (Claude) doing the testing under the owner's direction: first as a black box
from outside the web page, then logged in, then as a low-privilege SSH user on the machine, and last by
following `INSTALL.md` on a clean install. It is **not** an independent third-party audit. If you find
something it missed, please tell us (see the end).

## What MCS is built to defend

Some things are powerful on purpose, and the review treated them that way:

- **Everyone with a login key has the same access in the web page.** There are no helper roles. Make one
  key per person and revoke it when they leave.
- **Mods and plugins run as the `minecraft` user, and so does the web page.** Anything a mod can reach, a
  keyholder can reach, and the other way round. Only install mods you trust.
- **Backups are the safety net.** The goal is that damage can be undone, not that it cannot happen.
  Because a mod can delete the backups on the same machine, MCS has a separate, root-owned copy tool
  (`MCSoffbox`) that never deletes. **It only protects you once you set it up**, and it is opt-in.
- **The page listens on `127.0.0.1` only, with no HTTPS yet.** Reach it from another computer through an
  SSH tunnel. Do not put it on the internet as it is.

The line the review defended: a keyholder, a bad mod or a helper must not reach root, other users on the
machine, or the off-box copy, and the owner must keep a trustworthy record and working key revocation.
That line held.

## What held up

- **Login:** keys are 192-bit random secrets and only a hash is stored; no user lookup is possible;
  five wrong keys lock the source out for ten minutes (behind the socket address, so a forged
  `X-Forwarded-For` does nothing); sessions are opaque random values, ended on the server when a key is
  revoked.
- **The web server:** a strict content-security policy, no inline scripts, host-header checks against
  DNS rebinding, a custom header against cross-site requests, a one-megabyte body limit, and clean
  handling of malformed or oversized input (no request smuggling).
- **The API:** names, paths, uploads and settings are validated; every `../` and encoded variant stayed
  inside its folder; service settings cannot inject into a systemd unit; mod uploads check the jar's
  contents, not only its name.
- **The machine:** the web page and the servers run as `minecraft` under systemd confinement
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`); the one polkit rule lets them
  manage only `MCS@*` units. A low-privilege SSH user in the `minecraft` group has no sudo and no path to
  root.
- **Root tools:** `MCSkey` and `MCSoffbox` are root-owned copies that never run anything a lower-privileged
  user can write. `MCSoffbox` opens files without following links, checks what it wrote, and refuses a
  destination the `minecraft` user can write.
- **The record:** every change made through the web page is written to the journal by root, under the
  key's name, and a helper or a mod cannot forge or erase it (`sudo MCSkey audit`).

## Findings and where they stand

No critical or high findings. One design finding, a few low ones.

| | Severity | What | Status |
|---|---|---|---|
| F-1 | Low to medium | RCON, a plaintext side channel to the server console, could be switched on from the web page, with its password readable there, and it survives a revoked key. | Being fixed before release: RCON off by default, not settable from the web page, enabled only by the owner from a terminal command, with a plain warning. |
| F-2 | Low | A very deeply nested request dropped the connection with no answer. | Fixed: a normal 400. |
| F-3 | Low | A setting sent as the wrong type gave a 500. | Fixed: a 400. |
| F-4 | Low | Possible secrets in the audit record. | Not reproduced: the record keeps only the console command text or the action name, never request bodies. A test guards it. A secret typed into the console would be recorded, on purpose. |
| F-5 | Low | The session cookie has no `Secure` flag. | Open: harmless over localhost HTTP; it will be set when HTTPS support lands. |
| I-1 | Info | `NaN` and `Infinity` were accepted as JSON. | Fixed. |
| I-2 | Info | A few ignored settings return success quietly. | Open, cosmetic. |
| D-1 | Doc | The installer did not warn about missing Java or `acl`. | Fixed. |

Two early suspicions were wrong and were withdrawn after they were checked with a second tool: a
supposed missing body-size limit, and a supposedly missing polkit rule.

## Known limits you should plan around

- **Off-box backups are opt-in.** Until `sudo MCSoffbox set <folder on another drive>` is done, a bad mod or
  a helper can delete the only copies. The page shows a notice until it is set up.
- **Everyone with a key can do a lot.** Keys are personal, but access is not tiered.
- **No HTTPS.** Do not expose the page to the internet without something in front of it that provides it.
- **Not yet tested on real hardware:** a physical USB drive, a network share, and a Debian machine.

## Reporting a problem

Please report security problems through this repository's **Security** tab ("Report a vulnerability") so
they are private until fixed, not as a public issue. Tell us what you did, what you expected and what
happened. We cannot promise a time, but we will answer.
