#!/usr/bin/env python3
"""MCSkey - make, list and revoke MCSweb login keys, and read the audit log.

Runs from the root-owned MCS program folder (/opt/mcs/lib/MCS/mcskey.py, through
/usr/local/bin/MCSkey). That folder is deliberately not writable by the minecraft
user: mods run as that user, and anything it can edit must never be able to hand
out web logins.

The keys live in /etc/MCS/keys.json, owned by root and only READABLE by the
minecraft group. The web service can check a key but cannot add, change or
revoke one, and neither can a mod, a helper in the minecraft group, or a web
login. Changing keys needs sudo (this script asks for it itself).

MCS cannot tell who typed a command. The protection is the file permissions:
whoever has root on this machine can always make or revoke keys.

Python 3 standard library only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys

KEYS_DIR = "/etc/MCS"
KEYS_FILE = KEYS_DIR + "/keys.json"
GROUP = "minecraft"
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")

HELP = """\
MCSkey: one MCSweb login key per person, made here on the server and nowhere else.
  sudo MCSkey add <name>      make a key for <name> (shown ONCE; only a hash is kept)
  sudo MCSkey list            who has a key
  sudo MCSkey revoke <name>   remove a key; its sessions end immediately
  sudo MCSkey audit [-n 50]   what each key did (from the MCSweb log), newest last
Keys can only be changed as root. The web page, the minecraft user, mods and
anyone signed in to the web page cannot make or revoke keys. There is no
sign-up or "forgot key" over the network: if a key is lost or leaked, revoke it
and make a new one. A key identifies the key, not the person, so give one to
each person.
"""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def load() -> list[dict]:
    try:
        with open(KEYS_FILE) as f:
            return list(json.load(f)["keys"])
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError) as e:
        sys.exit(f"MCSkey: cannot read {KEYS_FILE}: {e}")


def save(keys: list[dict]) -> None:
    import grp
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        sys.exit(f"MCSkey: there is no '{GROUP}' group. Run MCSuser first.")
    os.makedirs(KEYS_DIR, mode=0o750, exist_ok=True)
    os.chown(KEYS_DIR, 0, gid)
    os.chmod(KEYS_DIR, 0o750)
    tmp = KEYS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump({"keys": keys}, f, indent=1)
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, KEYS_FILE)


def need_root() -> None:
    if os.geteuid() != 0:
        print("Changing keys needs sudo; asking for it (Ctrl+C to cancel).", file=sys.stderr)
        os.execvp("sudo", ["sudo", "-H", "/usr/local/bin/MCSkey", *sys.argv[1:]])


def cmd_add(label: str, quiet: bool) -> int:
    need_root()
    if not LABEL.match(label):
        print("A key name is 1-32 letters, numbers, space, dot, underscore or dash.", file=sys.stderr)
        return 1
    keys = load()
    if any(k["label"].lower() == label.lower() for k in keys):
        print(f"{label} already has a key. Revoke it first to replace it.", file=sys.stderr)
        return 1
    kid, secret = secrets.token_hex(3), secrets.token_urlsafe(24)
    keys.append({"id": kid, "label": label, "hash": _hash(secret), "created": datetime.date.today().isoformat()})
    save(keys)
    full = f"{kid}.{secret}"
    if quiet:
        print(full)
    else:
        print(f"Key for {label}:\n\n  {full}\n\n"
              "This is the only time it is shown; MCS keeps just a hash. Give it to that one person.\n"
              "It signs them in to the web page, and what they do is logged under their name.\n"
              f"Lost or leaked? Run: sudo MCSkey revoke {label}   then make a new one.")
    return 0


def cmd_list() -> int:
    keys = load()  # listing shows no secrets, so any user in the minecraft group may read it
    if not keys:
        print("No keys yet. Make one with: sudo MCSkey add <name>")
    for k in keys:
        print(f"{k['label']:<32} id {k['id']}  made {k.get('created', '?')}")
    return 0


def cmd_revoke(label: str) -> int:
    need_root()
    keys = load()
    left = [k for k in keys if k["label"].lower() != label.lower()]
    if len(left) == len(keys):
        print(f"No key called {label}. See: MCSkey list", file=sys.stderr)
        return 1
    save(left)
    print(f"Revoked {label}. Anyone signed in with it is signed out.")
    return 0


def cmd_audit(n: int) -> int:
    # The web service writes one line per event to its own journal. Only root can
    # write to another unit's journal, so the minecraft user cannot edit or forge it.
    try:
        out = subprocess.run(["journalctl", "_SYSTEMD_UNIT=MCSweb.service", "-o", "cat", "--no-pager", "-n", "100000"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"MCSkey: cannot read the journal: {e}", file=sys.stderr)
        return 1
    rows = []
    for ln in out.splitlines():
        if ln.startswith("[MCSweb audit] "):
            try:
                rows.append(json.loads(ln[15:]))
            except ValueError:
                pass
    if not rows:
        print("The audit log is empty (or you cannot read the journal: try sudo, or join the systemd-journal group).")
    for e in rows[-n:]:
        print(f"{e.get('time', '?')}  {e.get('key', '?'):<16} {e.get('ip', '?'):<15} {e.get('what', '')}  {e.get('detail', '')}".rstrip())
    return 0


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    args = [a for a in argv if a != "--quiet"]
    cmd = args[0] if args else ""
    if cmd == "add" and len(args) == 2:
        return cmd_add(args[1], quiet)
    if cmd == "list" and len(args) == 1:
        return cmd_list()
    if cmd == "revoke" and len(args) == 2:
        return cmd_revoke(args[1])
    if cmd == "audit" and (len(args) == 1 or (len(args) == 3 and args[1] == "-n" and args[2].isdigit())):
        return cmd_audit(int(args[2]) if len(args) == 3 else 50)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
