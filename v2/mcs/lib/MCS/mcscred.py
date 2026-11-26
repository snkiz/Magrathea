#!/usr/bin/env python3
"""MCScred - the key broker: it makes CurseForge requests so nothing else has to hold the key.

MCScred.service runs this as a throwaway user of its own (systemd DynamicUser). The web
page and MCSfetch send it "what to ask" over a local socket; it adds the key, asks
CurseForge, and hands back the answer. It never hands back the key.

Which key: the owner's own (sudo MCScred set curseforge) or else the project key. Both are
kept root-only in /etc/MCS/cred and passed to this service by systemd, so the minecraft
user, and so mods, cannot read either one on this machine. The project key does come from
public source, so someone can still copy it from there; the limits and the log are for that.

Every request is logged to the journal with who asked, taken from the kernel and not from
the request. Only metadata: never keys, search words or answers.

Python 3 standard library only.
"""
from __future__ import annotations

import collections
import datetime
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcsfetch as mf  # noqa: E402

KEY_DIR = "/etc/MCS/cred"
KEY_FILE = KEY_DIR + "/curseforge"
KEY_RE = re.compile(r"[A-Za-z0-9$./+=_-]{10,200}")

# The only things the key may be used for: search, one mod, one mod's files.
ROUTES = re.compile(r"/mods/search|/mods/\d{1,12}|/mods/\d{1,12}/files")
PARAMS = {"gameId", "classId", "slug", "searchFilter", "sortField", "sortOrder", "pageSize",
          "gameVersion", "modLoaderType"}

# MCS's own limits, so one bad mod cannot use up the key. CurseForge publishes no number.
WINDOW = 60          # seconds
PER_CALLER = 100     # requests per window from one systemd unit
TOTAL = 200          # requests per window from everyone
MAX_REQUEST = 4096   # bytes
AT_ONCE = 8          # connections being served
AT_ONCE_PER_CALLER = 3

HELP = """\
MCScred: the CurseForge key broker.
  sudo MCScred set curseforge       use your own CurseForge key (asks for it; not shown)
  sudo MCScred remove curseforge    go back to the project key, if this build has one
  MCScred status                    is CurseForge available?
  sudo MCScred audit [-n 50]        who asked CurseForge for what, newest last
"""


def key() -> tuple[str, str]:
    """-> (key, which one). The owner's own key wins over the project key."""
    folder = os.environ.get("CREDENTIALS_DIRECTORY")
    for name, which in (("keys_curseforge", "own"), ("keys_curseforge-project", "project")) if folder else ():
        try:
            lines = Path(folder, name).read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            continue
        found = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
        if found and KEY_RE.fullmatch(found[-1]):
            return found[-1], which
    return "", "none"


class Limiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.total: collections.deque = collections.deque()
        self.callers: dict[str, collections.deque] = {}

    def allow(self, caller: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            for q in (self.total, *self.callers.values()):
                while q and now - q[0] >= WINDOW:
                    q.popleft()
            self.callers = {k: v for k, v in self.callers.items() if v}
            mine = self.callers.setdefault(caller, collections.deque())
            if len(mine) >= PER_CALLER or len(self.total) >= TOTAL:
                return False
            mine.append(now)
            self.total.append(now)
            return True


limiter = Limiter()


def _clean(value, length: int = 60) -> str:
    """Text from a request, made safe to put in a log line."""
    return re.sub(r"[^A-Za-z0-9 ._/@-]", "?", str(value))[:length]


def audit(caller: dict, claimed: str, route: str, result: str, which: str) -> None:
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), **caller,
             "claimed": claimed, "platform": "curseforge", "route": route, "key": which, "result": result}
    print("[MCScred audit] " + json.dumps(entry), flush=True)


def handle(request, caller: dict) -> dict:
    """One request -> one reply. `caller` comes from the kernel; `request` is not trusted."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "bad request"}
    if request.get("status") is True:
        return {"ok": True, "available": bool(key()[0])}
    claimed = _clean(request.get("claimed", ""))
    route, params = request.get("route"), request.get("params", {})
    secret, which = key()

    def refuse(why: str) -> dict:
        audit(caller, claimed, _clean(route), "refused: " + why, which)
        return {"ok": False, "error": why}

    if not isinstance(route, str) or not ROUTES.fullmatch(route):
        return refuse("that CurseForge address is not allowed")
    if not isinstance(params, dict) or not all(
            k in PARAMS and isinstance(v, (str, int)) and len(str(v)) <= 200 for k, v in params.items()):
        return refuse("those search options are not allowed")
    if not secret:
        return refuse("CurseForge is not available in this build. Modrinth works without a key; the owner "
                      "can add a CurseForge key with: sudo MCScred set curseforge")
    if not limiter.allow(caller.get("unit", "?")):
        return refuse("too many CurseForge requests; wait a minute")
    url = mf.PLATFORMS["curseforge"]["base"] + route
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        mf.check_url(url)
        body = mf._fetch("curseforge", url, {"x-api-key": secret}).decode("utf-8", "replace")
    except mf.McsError as e:
        audit(caller, claimed, route, _clean(e, 120), which)
        return {"ok": False, "error": str(e)}
    audit(caller, claimed, route, "ok", which)
    return {"ok": True, "body": body}


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------
def peer(conn) -> dict:
    """Who is on the other end, as the kernel sees it: the process and its systemd unit."""
    pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    unit = "?"
    try:
        path = Path(f"/proc/{pid}/cgroup").read_text().strip().rsplit(":", 1)[-1]
        unit = next((p for p in reversed(path.split("/")) if p.endswith((".service", ".scope"))), "?")
    except OSError:
        pass
    return {"unit": unit, "pid": pid, "uid": uid}


def _serve_one(conn, caller: dict, done) -> None:
    try:
        with conn:
            data, deadline = b"", time.monotonic() + 10
            while b"\n" not in data and len(data) <= MAX_REQUEST:
                conn.settimeout(max(deadline - time.monotonic(), 0.01))
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            try:
                request = json.loads(data) if len(data) <= MAX_REQUEST else None
            except (ValueError, RecursionError):
                request = None
            conn.settimeout(30)
            conn.sendall(json.dumps(handle(request, caller)).encode())
    except OSError:
        pass
    finally:
        done()


def serve() -> int:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        print("MCScred serve is started by systemd (MCScred.socket).", file=sys.stderr)
        return 2
    server = socket.socket(fileno=3)
    lock, busy = threading.Lock(), collections.Counter()

    while True:
        conn, _ = server.accept()
        caller = peer(conn)
        unit = caller["unit"]
        with lock:
            full = sum(busy.values()) >= AT_ONCE or busy[unit] >= AT_ONCE_PER_CALLER
            if not full:
                busy[unit] += 1
        if full:
            conn.close()
            continue

        def done(unit=unit):
            with lock:
                busy[unit] -= 1

        threading.Thread(target=_serve_one, args=(conn, caller, done), daemon=True).start()


# --------------------------------------------------------------------------
# The owner's commands
# --------------------------------------------------------------------------
def need_root() -> None:
    if os.geteuid() != 0:
        print("This needs sudo; asking for it (Ctrl+C to cancel).", file=sys.stderr)
        os.execvp("sudo", ["sudo", "-H", "/usr/local/bin/MCScred", *sys.argv[1:]])


def _reload() -> None:
    # systemd reads the key when the service starts; the socket starts it again on the next request
    subprocess.run(["systemctl", "try-restart", "MCScred.service"], check=False)


def cmd_set() -> int:
    need_root()
    if sys.stdin.isatty():
        import getpass
        value = getpass.getpass("Paste your CurseForge API key (it is not shown): ")
    else:
        value = sys.stdin.readline()
    value = value.strip()
    if not KEY_RE.fullmatch(value):
        print("MCScred: that does not look like a CurseForge API key; nothing changed.", file=sys.stderr)
        return 1
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
    fd = os.open(KEY_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(value + "\n")
    os.replace(KEY_FILE + ".tmp", KEY_FILE)
    _reload()
    print("Saved. Only root can read it; MCS uses it for CurseForge from now on.")
    return 0


def cmd_remove() -> int:
    need_root()
    try:
        os.unlink(KEY_FILE)
    except FileNotFoundError:
        print("You have no key of your own saved.")
        return 0
    _reload()
    print("Removed.")
    return 0


def cmd_status() -> int:
    try:
        available = mf.broker_ask({"status": True}).get("available")
    except mf.McsError as e:
        print(f"MCScred: {e}", file=sys.stderr)
        return 1
    print("CurseForge is available." if available else "CurseForge is not available: no key.")
    return 0


def cmd_audit(n: int) -> int:
    need_root()
    out = subprocess.run(["journalctl", "_SYSTEMD_UNIT=MCScred.service", "-o", "cat", "--no-pager", "-n", "100000"],
                         capture_output=True, text=True, timeout=60).stdout
    rows = []
    for line in out.splitlines():
        if line.startswith("[MCScred audit] "):
            try:
                rows.append(json.loads(line[16:]))
            except ValueError:
                pass
    if not rows:
        print("Nothing has been asked of CurseForge yet.")
    for e in rows[-n:]:
        print(f"{e.get('time', '?')}  {e.get('unit', '?'):<22} pid {e.get('pid', '?'):<7} {e.get('route', '')}  "
              f"[{e.get('key', '?')} key] {e.get('result', '')}  (says it is {e.get('claimed') or '?'})")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["serve"]:
        return serve()
    if argv == ["set", "curseforge"]:
        return cmd_set()
    if argv == ["remove", "curseforge"]:
        return cmd_remove()
    if argv == ["status"]:
        return cmd_status()
    if argv[:1] == ["audit"] and (len(argv) == 1 or (len(argv) == 3 and argv[1] == "-n" and argv[2].isdigit())):
        return cmd_audit(int(argv[2]) if len(argv) == 3 else 50)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
