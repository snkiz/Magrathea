# Security checklist

Run this before merging anything that touches **root**, **keys** or **networking**, and before every release.
Every question should end in "yes" (or "not touched"). Each one comes from a real finding in the security review
(the F-numbers are in `SECURITY.md`) or from a design rule. When a change turns up a new kind of problem, add a
question for it here.

How to use it: do **Always**, then each section the change touches. Where a test in `v2/tests/` covers a question,
run the test on a throwaway machine instead of answering from memory.

## Always

- [ ] Does the change come with a plain-English explanation the maintainer can read and decide on? (`CLAUDE.md`)
- [ ] Does the line still hold: a keyholder, a mod or a helper (all of them act as `minecraft`) cannot reach root,
      other users, the off-box copy, the audit or key management?
- [ ] Were the matching tests in `v2/tests/` run on a throwaway machine, and do they pass?
- [ ] Was every suspected problem checked with a second tool, and as the right user, before calling it a bug or
      ruling it out? (Two early suspicions in the review were wrong.)
- [ ] Are there no secrets, real names, hostnames, IP addresses or local paths in code, comments, tests, logs or
      commit messages?
- [ ] Was the test machine cleaned up afterwards: test keys revoked, test users and sudoers files removed? (The
      review left a test user and a NOPASSWD sudoers file behind.)

## Root: anything that runs as root, or that root runs

Applies to: `MCSuser` and package scripts, unit files, the polkit rule, the command shims, `MCSkey`, `MCSoffbox`,
`MCScred`, `MCSrcon`, anything under `/etc/MCS`.

- [ ] Is all program code still root-owned under the program tree (`/opt/mcs`), with nothing in it the `minecraft`
      user can create, edit or rename? Does every unit run its program from there, never from `/opt/minecraft`?
      (Finding: the `minecraft` user could once edit the code its own units ran.)
      Check: `find /opt/mcs ! -user root` prints nothing; `sudo -u minecraft touch /opt/mcs/x` fails.
- [ ] Does no root tool run, import or read code from anywhere a lower-privileged user can write? (Python runs with
      `-I`; no bytecode cache in a writable folder.)
- [ ] Do root tools that touch `minecraft`-owned files refuse to follow links and avoid races: `O_NOFOLLOW`,
      `dir_fd`-relative opens, `follow_symlinks=False`, `O_EXCL` for new files, and a check of what was written?
      (The `MCSoffbox` pattern.)
- [ ] Does any new place root writes to refuse a destination the `minecraft` user can write? (`MCSoffbox` rule.)
- [ ] Is root config still locked down: `/etc/MCS` owned by root, the login keys file readable by `minecraft`
      (hashes only) but not writable, credential files root-only?
- [ ] Does the polkit rule still allow only start/stop/restart of `MCS@*` and `MCSinit@*` units, with no
      `manage-unit-files`, and does its unit-name pattern still match only valid server names?
- [ ] Are the command shims on the PATH plain root-owned files (not links into a writable folder), and do the sudo
      ones still run only the root-owned tool?
- [ ] Do units that run as `minecraft` keep their confinement (`NoNewPrivileges`, `ProtectSystem=strict`,
      `ProtectHome`, `PrivateTmp`), with `ReadWritePaths` no wider than needed (a server: only its own folder)?
- [ ] Is there still no way from the `minecraft` group to root: no new sudo rules, SUID files or group memberships?
- [ ] Do install, upgrade, remove and purge leave `instance/`, `backups/` and the login keys alone, and does an
      upgrade never loosen an owner or a mode?
- [ ] Is anything a mod can edit (`server.properties`, `service.conf`) treated as untrusted by root and by the units?
      (F-1: a setting alone can be undone, so the block has to live where `minecraft` cannot write.)

## Keys and secrets

Applies to: web login keys and sessions, `MCSkey`, the key broker (`MCScred`), API keys and tokens, RCON or
management-server passwords, any new secret.

- [ ] Are login keys still long random secrets, stored only as a hash, shown once, and created, reset and revoked
      only from the terminal as root? Can the web page and the `minecraft` user still not make or revoke a key?
- [ ] Does revoking a key still end its sessions at once, and does the change add no way in that survives a
      revoked key? (F-1: RCON did.)
- [ ] Does every new secret stay out of reach of the `minecraft` user? Mods run as `minecraft`, so anything it can
      read, a mod can read. A key-backed call from that side goes through the broker, which makes the call itself
      and never hands the key back.
- [ ] Does the broker still take the caller from the kernel (socket peer credentials, then the unit from
      `/proc/<pid>/cgroup`), and log anything the request says about itself only as "claimed"?
- [ ] Do logs and the audit keep metadata only: no keys, passwords, session values, request bodies, search words or
      API responses? (F-4; CurseForge also forbids keeping its API data.) Console commands are logged on purpose;
      if that changes, the docs must say so.
- [ ] Is no secret returned by a GET (masked, like `rcon.password`), printed to a log or put in a URL?
- [ ] Is the audit still written by root to the journal, so a helper or a mod cannot forge or erase it?
- [ ] Is the project's CurseForge key still one constant in one file with its comment block, and does a blank key
      switch CurseForge off cleanly?
- [ ] Is the session cookie still `HttpOnly`, `SameSite=Strict` and an opaque random value? If the change adds TLS,
      is `Secure` now set? (F-5)

## Networking

Applies to: the web server and its routes, anything that listens on a port, `mcsfetch` and the broker, RCON and
the management server, TLS or a reverse proxy.

### Web server and routes

- [ ] Does every new route need a valid session, and every change-making route the `X-MCS` header (cross-site
      request guard)? The login stays the only open route.
- [ ] Is the Host header still checked (DNS rebinding), and is the default bind still `127.0.0.1`? Network access
      stays opt-in: no UPnP, never a bare internet-facing page.
- [ ] Does all bad input get a clean 400, never a 500 or a dropped connection? Wrong JSON types, deep nesting,
      `NaN`/`Infinity`, bodies over the 1 MB cap (413), long or too many headers (431). (F-2, F-3, I-1)
- [ ] Does every name (server, player, backup, file, mod) pass its pattern, and does every path stay inside its
      folder? Try `../`, encoded, null-byte and backslash variants against a canary file.
- [ ] Can no value reach a unit file, a shell, the console or a properties file with a newline or a shell
      character in it?
- [ ] Do uploads check what a file is (a jar is really a jar), not only its name, and land only in allowed folders?
- [ ] Are the security headers intact: strict content-security policy (no inline script), `nosniff`,
      `frame-ancestors 'none'`, `no-referrer`, `no-store` on login?
- [ ] Is the lockout still keyed to the socket address, ignoring `X-Forwarded-For`? (Only an owner-enabled proxy
      mode, where nothing but the proxy can reach the port, may change that.)

### Remote control (RCON and the management server)

- [ ] Can the web page still not set or read `enable-rcon`, `rcon.*` or the management-server settings, with the
      password masked? (F-1)
- [ ] Is turning either on still owner-only (`sudo MCSrcon enable`), kept in the root-owned `/etc/MCS/remote.json`,
      applied by `MCS-run` at every start, with a loud log line while it is on?

### Downloads and platform APIs (`mcsfetch`, `MCScred`)

- [ ] Does every request go through the one API boundary and send `Magrathea/<version> (<repo URL>)` as its
      User-Agent (a contact, never a personal email)?
- [ ] HTTPS only, allowlisted hosts only, and redirects followed only to allowlisted hosts? Can user input never
      build an arbitrary URL?
- [ ] Is every download checked against the hash the platform publishes (sha256 recorded when there is none),
      size-capped, with timeouts, written to a temp file and moved into place only after the checks pass?
- [ ] Are each platform's rules kept: CurseForge, no caching and only IDs stored; Hangar, cache and at most 20
      requests per 5 seconds, documented routes only; Modrinth, fetch only when someone acts; PaperMC, stable
      builds by default and the download link read from the response?

### TLS and reverse proxy (when it is built)

- [ ] Is it switched on by the owner from the terminal, does the Host check accept the proxied name, and is the
      cookie's `Secure` flag set only when TLS is guaranteed?

## Before you finish

- [ ] Is `SECURITY.md` still true? Update its findings table and "What held up" if the change touched them.
- [ ] Did the change turn up a new kind of problem? Add a question for it here, tied to the finding.
