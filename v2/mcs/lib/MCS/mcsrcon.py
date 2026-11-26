#!/usr/bin/env python3
"""MCSrcon - the owner's switch for remote control of a Minecraft server.

Minecraft has two ways to be controlled over the network: RCON, and (1.21.9 and newer)
the "management server". Both are OFF in MCS unless the owner allows one, per server,
with sudo. The permission is kept in /etc/MCS/remote.json, owned by root and only
readable by the minecraft group, so the web page, a mod or a helper cannot grant it.

server.properties itself belongs to the minecraft user and can be edited by many
things, so it is not trusted. Every time a server starts, MCS-run calls
"mcsrcon.py apply", which switches off anything the owner has not allowed.

This is a guard against remote control being turned on by accident or from the web
page. It cannot stop a mod, which is a program and can open its own network port.

Python 3 standard library only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

STATE_DIR = "/etc/MCS"
STATE_FILE = STATE_DIR + "/remote.json"
INSTANCES = Path("/opt/minecraft/instance")
GROUP = "minecraft"
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")

# channel name -> the server.properties key that switches it on
CHANNELS = {"rcon": "enable-rcon", "management": "management-server-enabled"}

HELP = """\
MCSrcon: remote control of a server over the network. Off unless you allow it here.
  sudo MCSrcon enable <server> [rcon|management]    allow it (default: rcon)
  sudo MCSrcon disable <server> [rcon|management]   switch it off again (default: rcon)
  sudo MCSrcon show <server>                        the port and password to connect with
  MCSrcon list                                      what is allowed
Restart the server after enable or disable.
"""

WARNING = {
    "rcon": "RCON sends its password and commands UNENCRYPTED. Anyone who gets the password has the\n"
            "server console, and it keeps working after a web login key is revoked. Do not open its\n"
            "port to the internet.",
    "management": "The management server can op and kick players and stop the server. Anyone who gets its\n"
                  "secret can do the same, and it keeps working after a web login key is revoked. MCS does\n"
                  "not set it up: configure the management-server-* lines in server.properties yourself.",
}


# --- server.properties, read the way Java reads it ----------------------------------------
# Minecraft accepts "key = value", "key:value", backslash escapes and lines continued with a
# backslash. A simple search for "enable-rcon=true" would miss those, so parse them all.
def _entries(text: str) -> list[tuple[str | None, str, str]]:
    """-> [(key or None for comments/blank, value, the original text of the entry)]"""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    out, i = [], 0
    while i < len(lines):
        raw = [lines[i]]
        logical = lines[i].lstrip(" \t\f")
        i += 1
        if not logical or logical[0] in "#!":
            out.append((None, "", raw[0]))
            continue
        while (len(logical) - len(logical.rstrip("\\"))) % 2:  # odd number of \ : continues
            logical = logical[:-1]
            if i >= len(lines):
                break
            raw.append(lines[i])
            logical += lines[i].lstrip(" \t\f")
            i += 1
        m = re.match(r"((?:\\.|[^\\=: \t\f])*)[ \t\f]*[=:]?[ \t\f]*(.*)$", logical, re.S)
        out.append((_unescape(m.group(1)), _unescape(m.group(2)), "\n".join(raw)))
    return out


def _unescape(s: str) -> str:
    def one(m):
        if m.group(1):
            return chr(int(m.group(1), 16))
        return {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(m.group(2), m.group(2))
    return re.sub(r"\\(?:u([0-9a-fA-F]{4})|(.))", one, s)


def values(text: str) -> dict[str, str]:
    return {k: v for k, v, _ in _entries(text) if k is not None}  # the last one wins, as in Java


def with_values(text: str, new: dict[str, str]) -> str:
    """Set each key in `new`, removing every other spelling or repeat of it."""
    out, done = [], set()
    for key, _, raw in _entries(text):
        if key not in new:
            out.append(raw)
        elif key not in done:
            out.append(f"{key}={new[key]}")
            done.add(key)
    out += [f"{k}={v}" for k, v in new.items() if k not in done]
    return "\n".join(out) + "\n"


# --- the permission file -------------------------------------------------------------------
def load() -> dict[str, list[str]]:
    """server name -> allowed channels. Anything unreadable or odd means: nothing is allowed."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)["servers"]
        return {k: [c for c in v if c in CHANNELS] for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"MCS WARNING: cannot read {STATE_FILE} ({e}); treating remote control as not allowed.", file=sys.stderr)
        return {}


def save(servers: dict[str, list[str]]) -> None:
    import grp
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        sys.exit(f"MCSrcon: there is no '{GROUP}' group. Run MCSuser first.")
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"servers": servers}, f, indent=1)
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, STATE_FILE)


# --- at server start (runs as the minecraft user, in the server's folder) ------------------
def apply(name: str, path: Path = Path("server.properties")) -> int:
    allowed = load().get(name, [])
    if not path.is_file():
        return 0  # no file yet: Minecraft writes one with both switched off
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    now, new = values(text), {}
    on = {c: now.get(key, "").strip().lower() == "true" for c, key in CHANNELS.items()}

    if "rcon" in allowed:
        if not on["rcon"]:
            new["enable-rcon"] = "true"
        if not now.get("rcon.password"):
            new["rcon.password"] = secrets.token_urlsafe(18)
        print("MCS WARNING: RCON is ON for this server (allowed with MCSrcon). It is not encrypted. "
              "Turn it off: sudo MCSrcon disable " + name)
    else:
        if on["rcon"]:
            new["enable-rcon"] = "false"
            print("MCS: switched RCON off; it was set on without the owner allowing it "
                  f"(sudo MCSrcon enable {name}).")
        if now.get("rcon.password"):
            new["rcon.password"] = ""  # an old password must not come back to life later

    if "management" in allowed:
        if on["management"]:
            print("MCS WARNING: the management server is ON for this server (allowed with MCSrcon). "
                  f"Turn it off: sudo MCSrcon disable {name} management")
    elif on["management"]:
        new["management-server-enabled"] = "false"
        print("MCS: switched the management server off; it was set on without the owner allowing it "
              f"(sudo MCSrcon enable {name} management).")

    if new:
        mode = path.stat().st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(prefix=".mcs-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
            f.write(with_values(text, new))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    return 0


# --- the owner's commands ------------------------------------------------------------------
def need_root() -> None:
    if os.geteuid() != 0:
        print("This needs sudo; asking for it (Ctrl+C to cancel).", file=sys.stderr)
        os.execvp("sudo", ["sudo", "-H", "/usr/local/bin/MCSrcon", *sys.argv[1:]])


def server_exists(name: str) -> bool:
    if NAME_RE.match(name) and (INSTANCES / name).is_dir():
        return True
    print(f"MCSrcon: there is no server called {name!r} in {INSTANCES}.", file=sys.stderr)
    return False


def cmd_enable(name: str, channel: str) -> int:
    need_root()
    if not server_exists(name):
        return 1
    servers = load()
    servers[name] = sorted(set(servers.get(name, [])) | {channel})
    save(servers)
    print(f"WARNING: {WARNING[channel]}\n\nAllowed for {name}. Restart it to apply: sudo systemctl restart MCS@{name}")
    if channel == "rcon":
        print(f"Then get the port and password with: sudo MCSrcon show {name}")
    return 0


def cmd_disable(name: str, channel: str) -> int:
    need_root()
    if not NAME_RE.match(name):
        return 1
    servers = load()
    servers[name] = [c for c in servers.get(name, []) if c != channel]
    if not servers[name]:
        del servers[name]
    save(servers)
    print(f"No longer allowed for {name}. A running server keeps it until restarted: sudo systemctl restart MCS@{name}")
    return 0


def cmd_show(name: str) -> int:
    need_root()
    if not server_exists(name):
        return 1
    if "rcon" not in load().get(name, []):
        print(f"RCON is not allowed for {name}. See: sudo MCSrcon enable {name}", file=sys.stderr)
        return 1
    # server.properties is the minecraft user's file: read it as that user, never as root
    import pwd
    user = pwd.getpwnam(GROUP)
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    path = INSTANCES / name / "server.properties"
    now = values(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else {}
    if not now.get("rcon.password"):
        print(f"No password yet. It is made when the server starts: sudo systemctl restart MCS@{name}")
        return 1
    clean = {k: re.sub(r"[^ -~]", "?", now.get(k, "")) for k in ("rcon.port", "rcon.password")}
    print(f"port:     {clean['rcon.port'] or '25575'}\npassword: {clean['rcon.password']}")
    return 0


def cmd_list() -> int:
    servers = load()
    if not servers:
        print("Remote control is not allowed on any server.")
    for name, channels in sorted(servers.items()):
        print(f"{name}: {', '.join(channels)}")
    return 0


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd in ("enable", "disable") and len(rest) in (1, 2):
        channel = rest[1] if len(rest) == 2 else "rcon"
        if channel in CHANNELS:
            return (cmd_enable if cmd == "enable" else cmd_disable)(rest[0], channel)
    if cmd == "show" and len(rest) == 1:
        return cmd_show(rest[0])
    if cmd == "list" and not rest:
        return cmd_list()
    if cmd == "apply" and len(rest) == 1:
        return apply(rest[0])
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
