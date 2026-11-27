#!/usr/bin/env python3
"""MCSweb - local web interface for MCS. Python 3 standard library only.

  server list with start/stop/restart, live console, command box,
  settings forms (service.conf, server.properties), jar + mod management
  (through mcsfetch), uploads, backups, create new server.

Security model
  * Listens on 127.0.0.1 only unless you pass --listen. There is no TLS, so
    for phone/remote access put it behind Tailscale or a reverse proxy.
  * Every API call needs a session cookie, obtained by pasting an access key
    on the login page. Each person gets their own key, made ONLY from the
    terminal on the server (sudo MCSkey add <name>); keys are stored hashed in a
    root-owned file this service can only read. Actions go to the journal under the key's label.
  * Host header and Origin are checked (DNS-rebinding / CSRF), state-changing
    calls need an X-MCS header, cookies are HttpOnly + SameSite=Strict.
  * Runs as the minecraft user. systemctl access is limited by a polkit rule
    to MCS@*.service / MCS@*.socket, so it can never touch other units.
  * Never scrapes or embeds Modrinth/CurseForge pages: it only uses their
    APIs and links out to the project page.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import re
import secrets
import select
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
import mcsbackup as mb  # noqa: E402
import mcsfetch as mf  # noqa: E402
import mcsrcon as mr  # noqa: E402
import mcsmodcheck as modcheck  # noqa: E402

HOME, INSTANCES = mf.HOME, mf.INSTANCES
WEB = LIB / "web"
BACKUPS = HOME / "backups" / "instance"
CONF_DIR = HOME / ".config" / "MCS"
OLD_TOKEN_FILE = CONF_DIR / "web.token"  # pre-keys single token; deleted on start
# Keys are root-owned and only READABLE here: the web service (and so a mod, which runs as
# the same user) can check a key but cannot add or revoke one. Managed by MCSkey (sudo).
KEYS_FILE = Path("/etc/MCS/keys.json")
EULA_TOKEN = CONF_DIR / "EULA.token"  # in the data folder: the program folder is read-only
ORIG_ERR = sys.stderr

SESSION_SECONDS = 12 * 3600
MAX_UPLOAD = 1 << 30  # 1 GiB
MAX_JSON = 1 << 20
SESSIONS: dict[str, tuple[float, str]] = {}  # session id -> (expiry, key id)
FAILS: dict[str, list[float]] = {}  # client ip -> times of recent bad logins
MAX_FAILS, FAIL_WINDOW = 10, 600
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
CSP = ("default-src 'self'; img-src 'self' data: https://cdn.modrinth.com https://media.forgecdn.net "
       "https://*.forgecdn.net; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

UPLOAD_DIRS = {"root": ("", {".jar"}), "mods": ("mods", {".jar"}), "plugins": ("plugins", {".jar"}),
               "config": ("config", {".json", ".json5", ".toml", ".yml", ".yaml", ".properties", ".txt", ".cfg", ".conf"})}
SERVICE_FIELDS = ("SERVER", "JAVA_HOME", "RAM", "ARGS", "TIMER", "SIGNOFF", "SAVEnX")
PROP_KEY = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def log(msg: str) -> None:
    print(f"[MCSweb] {msg}", file=ORIG_ERR, flush=True)


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


# --------------------------------------------------------------------------
# Access keys (one per person) and the audit log
# --------------------------------------------------------------------------
_KEYS_CACHE: tuple[tuple[int, int], list] = ((0, 0), [])


def load_keys() -> list[dict]:
    """Keys on disk, re-read whenever the file changes so revokes bite at once."""
    global _KEYS_CACHE
    try:
        st = KEYS_FILE.stat()
    except OSError:
        return []
    sig = (st.st_mtime_ns, st.st_size)
    if _KEYS_CACHE[0] != sig:
        try:
            data = json.loads(KEYS_FILE.read_text())["keys"]
        except (OSError, ValueError, KeyError, TypeError):
            data = []
        _KEYS_CACHE = (sig, data)
    return _KEYS_CACHE[1]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def find_key(given: str) -> dict | None:
    """Match 'id.secret' against the stored hashes (constant-time compare)."""
    kid, _, secret = given.partition(".")
    found = None
    for k in load_keys():
        if k.get("id") == kid and hmac.compare_digest(k.get("hash", ""), _hash(secret)):
            found = k
    return found


def key_by_id(kid: str) -> dict | None:
    return next((k for k in load_keys() if k.get("id") == kid), None)


def audit(who: str, ip: str, what: str, detail: str = "") -> None:
    """One JSON line per event, into the journal of this service (read with: sudo MCSkey audit).
    The journal is not writable by the minecraft user, so nobody who can only act as that
    user (a mod, a helper in the group) can edit or erase it. Never put secrets in detail."""
    line = json.dumps({"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "key": who, "ip": ip, "what": what, "detail": detail[:300]})
    print(f"[MCSweb audit] {line}", file=ORIG_ERR, flush=True)


# --------------------------------------------------------------------------
# Instances / systemd
# --------------------------------------------------------------------------
def inst(name: str) -> Path:
    if not mf.NAME_RE.match(name or ""):
        raise ApiError(400, "invalid server name")
    d = INSTANCES / name
    if not d.is_dir():
        raise ApiError(404, f"no server called {name}")
    return d


def instance_names() -> list[str]:
    if not INSTANCES.is_dir():
        return []
    return sorted(p.name for p in INSTANCES.iterdir() if p.is_dir() and mf.NAME_RE.match(p.name))


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def read_conf(d: Path) -> dict:
    out: dict[str, str] = {}
    p = d / "service.conf"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m and m.group(1) not in out:
                out[m.group(1)] = m.group(2)
    return out


# Remote control settings: shown but not changeable here (MCSrcon on the server), passwords hidden.
SECRET_PROPS = ("rcon.password", "management-server-secret", "management-server-tls-keystore-password")
# Line breaks Python knows but a form field should never carry; they would split a settings line.
CONTROL_CHARS = r"[\x00-\x1f\x7f\x85\u2028\u2029]"


def remote_key(key: str) -> bool:
    k = key.strip()
    return k == "enable-rcon" or k.startswith(("rcon.", "management-server-"))


def read_props(d: Path) -> list[dict]:
    p = d / "server.properties"
    items = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                item = {"key": k, "value": v}
                if remote_key(k):
                    item["locked"] = True
                    if k in SECRET_PROPS and v:
                        item["value"] = "(hidden)"
                items.append(item)
    return items


def server_port(d: Path) -> int:
    for it in read_props(d):
        if it["key"] == "server-port" and it["value"].isdigit():
            return int(it["value"])
    return 25565


def unit_states(names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    props = ["Id", "ActiveState", "SubState", "MainPID", "UnitFileState", "NRestarts", "Result",
             "StatusText", "MemoryCurrent", "ActiveEnterTimestamp"]
    cmd = ["systemctl", "show"] + [x for p in props for x in ("-p", p)] + [f"MCS@{n}.service" for n in names]
    r = run(cmd)
    states: dict[str, dict] = {}
    for block in r.stdout.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        uid = kv.get("Id", "")
        m = re.match(r"^MCS@(.+)\.service$", uid)
        if m:
            states[m.group(1)] = kv
    return states


def describe(name: str, st: dict | None = None) -> dict:
    d = INSTANCES / name
    meta = mf.load_meta(d)
    conf = read_conf(d)
    st = st or unit_states([name]).get(name, {})
    mem = st.get("MemoryCurrent", "")
    return {"name": name, "type": meta.get("type", ""), "mc": meta.get("mc", ""), "loader": meta.get("loader", ""),
            "state": st.get("ActiveState", "unknown"), "sub": st.get("SubState", ""),
            "result": st.get("Result", ""), "status_text": st.get("StatusText", ""),
            "enabled": st.get("UnitFileState", "") == "enabled", "pid": int(st.get("MainPID", "0") or 0),
            "memory": int(mem) if mem.isdigit() else None, "since": st.get("ActiveEnterTimestamp", ""),
            "restarts": int(st.get("NRestarts", "0") or 0), "port": server_port(d),
            "jar": conf.get("SERVER", ""), "configured": conf.get("SERVER", "NULL") not in ("", "NULL")}


def list_jdks() -> list[str]:
    return sorted(os.path.dirname(os.path.dirname(p)) for p in
                  glob.glob("/usr/lib/jvm/*/bin/java") if os.access(p, os.X_OK))


def fifo_path(name: str) -> str:
    return f"/run/MCS@{name}.stdin"


def send_console(name: str, text: str) -> None:
    text = text.strip()
    if not text or len(text) > 1000 or re.search(r"[\x00-\x1f\x7f]", text):
        raise ApiError(400, "command must be a single line of printable text")
    path = fifo_path(name)
    try:
        if not stat.S_ISFIFO(os.stat(path).st_mode):
            raise ApiError(409, "console is not available")
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ApiError(409, "the server is not running (no console)") from None
    except OSError as e:
        raise ApiError(409, f"cannot open console: {e.strerror}") from None
    try:
        os.write(fd, (text + "\n").encode())
    finally:
        os.close(fd)


def unit_action(name: str, action: str) -> None:
    d = inst(name)
    if action not in ("start", "stop", "restart"):
        raise ApiError(400, "unknown action")
    if action in ("start", "restart") and read_conf(d).get("SERVER", "NULL") in ("", "NULL"):
        raise ApiError(400, "no server jar is set yet: download or upload one in the Mods tab, then pick it in Settings")
    run(["systemctl", "reset-failed", f"MCS@{name}.service", f"MCS@{name}.socket"])
    r = run(["systemctl", "--no-block", action, f"MCS@{name}.service"])
    if r.returncode != 0:
        raise ApiError(500, (r.stderr or r.stdout).strip() or f"systemctl {action} failed")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _no_constant(name: str):
    raise ValueError(name)


def values_object(body: dict) -> dict:
    """The 'values' of a settings request must be an object: a string or a list is a 400, not a 500."""
    values = body.get("values", {})
    if not isinstance(values, dict):
        raise ApiError(400, "values must be an object")
    return values


def validate_service(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k not in SERVICE_FIELDS:
            raise ApiError(400, f"unknown setting {k}")
        if not isinstance(v, str) or re.search(CONTROL_CHARS, v) or len(v) > 4000:
            raise ApiError(400, f"{k}: single line of text, please")
        v = v.strip()
        if k == "SERVER":
            try:
                mf.safe_name(v)
            except mf.McsError:
                raise ApiError(400, "SERVER must be a .jar file name in the server folder") from None
        elif k == "JAVA_HOME":
            if not os.access(os.path.join(v, "bin", "java"), os.X_OK):
                raise ApiError(400, f"JAVA_HOME: {v} has no bin/java")
        elif k == "RAM":
            if not re.fullmatch(r"(-X(?:mx|ms|mn|ss)\d+[kKmMgG]\s*)+", v):
                raise ApiError(400, "RAM must look like: -Xmx4G -Xms4G")
        elif k == "ARGS":
            if any(not t.startswith("-") for t in v.split()):
                raise ApiError(400, "every Java argument must start with -")
        elif k == "TIMER":
            if not v.isdigit() or not 0 <= int(v) <= 300:
                raise ApiError(400, "TIMER must be 0-300 seconds")
        out[k] = v
    return out


def save_properties(d: Path, updates: dict) -> None:
    for k, v in updates.items():
        if not PROP_KEY.match(k) or not isinstance(v, str) or re.search(CONTROL_CHARS, v) or len(v) > 1000:
            raise ApiError(400, f"bad value for {k}")
        if remote_key(k):
            raise ApiError(400, f"{k} cannot be changed from the web page; the owner uses MCSrcon on the server")
    p = d / "server.properties"
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    seen = set()
    for i, line in enumerate(lines):
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0]
            if k in updates:
                lines[i] = f"{k}={updates[k]}"
                seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    mf.write_atomic(p, "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Creating servers, EULA
# --------------------------------------------------------------------------
def stamp(text: str) -> str:
    """Give line 2 of a Minecraft file today's date, as Minecraft writes it (same as MCS.lib)."""
    now = time.strftime("%a %b %d %H:%M:%S %Z %Y")
    return re.sub(r"\A(.*\n)#.*", lambda m: f"{m.group(1)}#{now}", text)


def eula_agreed() -> bool:
    return EULA_TOKEN.is_file() and EULA_TOKEN.read_text().strip() == "true"


def create_instance(name: str, agree: bool, port: int | None = None, op_name: str = "", seed: str = "") -> None:
    if not mf.NAME_RE.match(name or ""):
        raise ApiError(400, "names may only use letters, numbers, dot, underscore and dash")
    if len(seed) > 128 or re.search(r"[\x00-\x1f\x7f]", seed):
        raise ApiError(400, "the world seed can be up to 128 characters, on one line")
    d = INSTANCES / name
    if d.exists():
        raise ApiError(409, f"{name} already exists")
    port = mf.check_port(port) if port is not None else mf.suggest_port()
    # The whitelist is on by default, so a server nobody is allowed on is useless:
    # insist on a first player, and check the name exists before creating anything.
    mf.lookup_player(op_name, True)
    if not eula_agreed():
        if not agree:
            raise ApiError(400, "you must accept the Minecraft EULA (https://aka.ms/MinecraftEULA) to run a server")
        mf.write_atomic(EULA_TOKEN, "true\n")
    INSTANCES.mkdir(parents=True, exist_ok=True)
    d.mkdir()
    os.chmod(d, 0o2770)
    eula = (LIB / "eula.txt").read_text(encoding="utf-8").replace("eula=false", "eula=true")
    mf.write_atomic(d / "eula.txt", stamp(eula))
    props = stamp((LIB / "server.properties").read_text(encoding="utf-8"))
    props = re.sub(r"^level-name=NULL", f"level-name={name}", props, flags=re.M)
    props = re.sub(r"^motd=.*", f"motd=Welcome to {name}", props, flags=re.M)
    mf.write_atomic(d / "server.properties", props)
    mf.ensure_conf(d)
    for f in ("eula.txt", "server.properties", "service.conf"):
        os.chmod(d / f, 0o660)
    save_properties(d, {"server-port": str(port), **({"level-seed": seed} if seed else {})})
    try:
        mf.add_player(d, op_name, op=True)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


# --------------------------------------------------------------------------
# Background jobs (one at a time; output is captured from mcsfetch)
# --------------------------------------------------------------------------
class Job:
    def __init__(self, title: str):
        self.id = secrets.token_hex(6)
        self.title, self.state, self.rc = title, "running", None
        self.lines: list[str] = []
        self.started = time.time()
        self._partial = ""

    def write(self, s: str) -> int:
        self._partial += s
        *done, self._partial = self._partial.split("\n")
        self.lines.extend(x for x in done if x != "")
        return len(s)

    def flush(self) -> None:
        pass

    def add(self, line: str) -> None:
        self.lines.append(line)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "state": self.state, "rc": self.rc,
                "lines": self.lines + ([self._partial] if self._partial else [])}


JOBS: dict[str, Job] = {}
JOB_LOCK = threading.Lock()


def start_job(title: str, fn) -> Job:
    if not JOB_LOCK.acquire(blocking=False):
        raise ApiError(409, "another task is still running, wait for it to finish")
    job = Job(title)
    JOBS[job.id] = job
    for old in sorted(JOBS.values(), key=lambda j: j.started)[:-15]:
        JOBS.pop(old.id, None)

    def target():
        try:
            with contextlib.redirect_stdout(job), contextlib.redirect_stderr(job):
                job.rc = fn(job)
            job.state = "done" if not job.rc else "failed"
        except Exception as e:  # noqa: BLE001 - report anything to the user
            job.add(f"error: {e}")
            job.rc, job.state = 1, "failed"
        finally:
            JOB_LOCK.release()

    threading.Thread(target=target, daemon=True).start()
    return job


def cli_job(title: str, argv: list[str]) -> Job:
    def fn(job):
        try:
            return mf.main(argv)
        except SystemExit as e:
            return int(e.code or 0) if isinstance(e.code, int) else 2
    return start_job(title, fn)


def backup_job(name: str, mode: str):
    """Run a backup through the shared code in mcsbackup (same checks as the timer)."""
    def fn(job: Job) -> int:
        try:
            with contextlib.redirect_stdout(job):
                if mode == "clean":
                    mb.backup_clean(name, only_if_changed=False)
                else:
                    mb.backup_live(name)
            return 0
        except mb.BackupError as e:
            job.add(f"backup failed: {e}")
            return 1
    return start_job(("Safe backup " if mode == "clean" else "Quick backup ") + name, fn)


def test_job(name: str, fname: str):
    def fn(job: Job) -> int:
        try:
            with contextlib.redirect_stdout(job):
                res = mb.test_backup(name, fname)
            return 1 if res["ok"] is False else 0
        except mb.BackupError as e:
            job.add(f"test failed: {e}")
            return 1
    return start_job(f"Test backup {fname}", fn)


def restore_job(name: str, fname: str):
    def fn(job: Job) -> int:
        try:
            with contextlib.redirect_stdout(job):
                res = mb.restore(name, fname)
            job.add(f"If that was a mistake, restore {res['undo']} to go back.")
            return 0
        except mb.BackupError as e:
            job.add(f"restore failed: {e}")
            return 1
    return start_job(f"Restore {name} from {fname}", fn)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def list_files(name: str) -> dict:
    d = inst(name)
    meta = mf.load_meta(d)
    managed = {m["file"] for m in meta.get("mods", {}).values()}

    def listing(sub: str, exts: set[str] | None = None) -> list[dict]:
        base = d / sub if sub else d
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and not p.name.startswith(".") and (not exts or p.suffix.lower() in exts):
                out.append({"name": p.name, "size": p.stat().st_size, "managed": p.name in managed and sub in ("mods", "plugins")})
        return out
    return {"root": listing("", {".jar"}), "mods": listing("mods"), "plugins": listing("plugins"), "config": listing("config")}


def safe_upload_name(name: str, exts: set[str]) -> str:
    name = os.path.basename(name or "")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._+()\[\]' -]{0,150}", name) or Path(name).suffix.lower() not in exts:
        raise ApiError(400, f"file name/type not allowed (allowed: {', '.join(sorted(exts))})")
    return name


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str, auth: bool = True):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, auth))
        return fn
    return deco


_VERSION_CACHE: dict[str, tuple[float, list]] = {}


@route("POST", "/api/login", auth=False)
def api_login(h, m, q, body):
    ip = h.client_address[0]
    now = time.time()
    recent = [t for t in FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    FAILS[ip] = recent
    if len(recent) >= MAX_FAILS:
        raise ApiError(429, "too many wrong keys from this address; wait ten minutes and try again")
    time.sleep(0.5)
    given = str((body or {}).get("key", "")).strip()
    k = find_key(given) if given else None
    if not k:
        recent.append(now)
        log(f"failed login from {ip}")
        audit("-", ip, "login failed")
        time.sleep(1)
        raise ApiError(401, "that key is not right")
    FAILS.pop(ip, None)
    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = (time.time() + SESSION_SECONDS, k["id"])
    audit(k["label"], ip, "login")
    h.set_cookie = f"mcs_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
    return {"ok": True}


@route("POST", "/api/logout")
def api_logout(h, m, q, body):
    SESSIONS.pop(h.session_id or "", None)
    h.set_cookie = "mcs_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    return {"ok": True}


@route("GET", "/api/auth", auth=False)
def api_auth(h, m, q, body):
    return {"authed": h.authed, "who": h.key_label if h.authed else None}


@route("GET", "/api/instances")
def api_instances(h, m, q, body):
    names = instance_names()
    states = unit_states(names)
    return [describe(n, states.get(n, {})) for n in names]


@route("POST", "/api/instances")
def api_create(h, m, q, body):
    port = body.get("port")
    if port in (None, ""):
        port = None
    elif isinstance(port, bool) or not str(port).isdigit():
        raise ApiError(400, "the game port must be a number")
    create_instance(str(body.get("name", "")), bool(body.get("agree")), int(port) if port is not None else None,
                    str(body.get("op_name", "")).strip(), str(body.get("seed", "")).strip())
    return {"ok": True}


@route("GET", "/api/newserver")
def api_newserver(h, m, q, body):
    return {"agreed": eula_agreed(), "url": "https://aka.ms/MinecraftEULA", "port": mf.suggest_port()}


@route("POST", r"/api/instances/([^/]+)/players")
def api_players(h, m, q, body):
    name, pname, op = m.group(1), str(body.get("name", "")).strip(), bool(body.get("op"))
    inst(name)  # 404 for a server that does not exist
    if describe(name)["state"] == "active":
        # A running server rewrites whitelist.json and ops.json itself (a file edit made now can be lost when it
        # stops), so ask the server to do it through its own console.
        if not mf.PLAYER_RE.match(pname):
            raise ApiError(400, "a Minecraft name is 3-16 letters, numbers or underscores")
        send_console(name, f"whitelist add {pname}")
        if op:
            send_console(name, f"op {pname}")
        return {"name": pname, "live": True}
    r = mf.add_player(inst(name), pname, op=op)
    r["note"] = "A running server needs 'whitelist reload' (or a restart) to pick this up."
    return r


@route("GET", "/api/eula")
def api_eula(h, m, q, body):
    return {"agreed": eula_agreed(), "url": "https://aka.ms/MinecraftEULA"}


@route("GET", r"/api/instances/([^/]+)")
def api_instance(h, m, q, body):
    name = m.group(1)
    d = inst(name)
    info = describe(name)
    info.update(conf=read_conf(d), jars=[f["name"] for f in list_files(name)["root"]], jdks=list_jdks(),
                properties=read_props(d), mods=mf.load_meta(d).get("mods", {}))
    recorded = bool(info["type"] and info["mc"])  # if not, offer what the files suggest; the world's version is always useful
    info["detected"] = {"world_mc": mf.world_version(d)} if recorded else mf.detect(d, info["jar"])
    return info


@route("POST", r"/api/instances/([^/]+)/action")
def api_action(h, m, q, body):
    unit_action(m.group(1), str(body.get("action", "")))
    return {"ok": True}


@route("POST", r"/api/instances/([^/]+)/command")
def api_command(h, m, q, body):
    inst(m.group(1))
    send_console(m.group(1), str(body.get("command", "")))
    return {"ok": True}


@route("PUT", r"/api/instances/([^/]+)/service")
def api_service(h, m, q, body):
    d = inst(m.group(1))
    values = validate_service(values_object(body))
    if "SERVER" in values and not (d / values["SERVER"]).is_file():
        raise ApiError(400, f"{values['SERVER']} is not in the server folder")
    conf = mf.ensure_conf(d)
    for k, v in values.items():
        mf.set_conf(conf, k, v)
    return {"ok": True}


@route("PUT", r"/api/instances/([^/]+)/properties")
def api_properties(h, m, q, body):
    values = values_object(body)
    d = inst(m.group(1))
    if "server-port" in values:
        v = str(values["server-port"])
        if not v.isdigit():
            raise ApiError(400, "server-port must be a number")
        mf.check_port(int(v), skip=m.group(1))
    save_properties(d, values)
    return {"ok": True, "warnings": [mf.online_mode_warning()] if str(values.get("online-mode", "")).lower() == "false" else []}


@route("GET", r"/api/instances/([^/]+)/files")
def api_files(h, m, q, body):
    return list_files(m.group(1))


@route("DELETE", r"/api/instances/([^/]+)/files")
def api_delete_file(h, m, q, body):
    d = inst(m.group(1))
    where = q.get("dir", [""])[0]
    if where not in UPLOAD_DIRS or where == "root":
        raise ApiError(400, "can only delete from mods, plugins or config")
    name = safe_upload_name(q.get("name", [""])[0], UPLOAD_DIRS[where][1])
    target = d / UPLOAD_DIRS[where][0] / name
    if not target.is_file():
        raise ApiError(404, "no such file")
    target.unlink()
    meta = mf.load_meta(d)
    for k in [k for k, v in meta.get("mods", {}).items() if v["file"] == name]:
        meta["mods"].pop(k)
    if meta:
        mf.save_meta(d, meta)
    return {"ok": True}


@route("PUT", r"/api/instances/([^/]+)/upload")
def api_upload(h, m, q, body):
    d = inst(m.group(1))
    where = q.get("dir", [""])[0]
    if where not in UPLOAD_DIRS:
        raise ApiError(400, "dir must be root, mods, plugins or config")
    sub, exts = UPLOAD_DIRS[where]
    name = safe_upload_name(q.get("name", [""])[0], exts)
    length = int(h.headers.get("Content-Length", "-1"))
    if length < 0 or length > MAX_UPLOAD:
        raise ApiError(413, f"upload must be 1 byte to {MAX_UPLOAD >> 20} MiB")
    dest = d / sub if sub else d
    if not dest.is_dir():
        dest.mkdir(parents=True)
        os.chmod(dest, 0o2770)
    fd, tmp = tempfile.mkstemp(prefix=".mcs-", suffix=".part", dir=dest)
    try:
        with os.fdopen(fd, "wb") as out:
            left, first = length, True
            while left > 0:
                chunk = h.rfile.read(min(1 << 20, left))
                if not chunk:
                    raise ApiError(400, "upload ended early")
                if first and name.endswith(".jar") and not chunk.startswith(b"PK\x03\x04"):
                    raise ApiError(400, "that does not look like a jar file")
                first = False
                out.write(chunk)
                left -= len(chunk)
        os.chmod(tmp, 0o660)
        os.replace(tmp, dest / name)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return {"ok": True, "name": name, "size": length}


@route("GET", r"/api/versions")
def api_versions(h, m, q, body):
    kind = q.get("type", ["vanilla"])[0]
    if kind not in mf.JAR_TYPES:
        raise ApiError(400, "unknown server type")
    ts, cached = _VERSION_CACHE.get(kind, (0, []))
    if time.time() - ts > 600:
        try:
            cached = mf.list_versions(kind)
        except mf.McsError as e:
            raise ApiError(502, str(e)) from None
        _VERSION_CACHE[kind] = (time.time(), cached)
    return cached[:60]


@route("POST", r"/api/instances/([^/]+)/jar")
def api_jar(h, m, q, body):
    name = m.group(1)
    inst(name)
    kind, mc = str(body.get("type", "")), str(body.get("mc", "latest"))
    if kind not in mf.JAR_TYPES or not re.fullmatch(r"latest|[0-9][A-Za-z0-9._-]{0,30}", mc):
        raise ApiError(400, "bad server type or version")
    return cli_job(f"Download {kind} {mc} for {name}", ["jar", name, "--type", kind, "--mc", mc]).to_dict()


@route("POST", r"/api/instances/([^/]+)/record")
def api_record(h, m, q, body):
    name = m.group(1)
    inst(name)
    kind, mc = str(body.get("type", "")), str(body.get("mc", ""))
    if kind not in mf.JAR_TYPES or not mf.MC_VERSION_RE.fullmatch(mc):
        raise ApiError(400, "bad server type or version")
    return cli_job(f"Record {name} as {kind} {mc}", ["record", name, "--type", kind, "--mc", mc]).to_dict()


@route("GET", r"/api/instances/([^/]+)/search")
def api_search(h, m, q, body):
    d = inst(m.group(1))
    meta = mf.load_meta(d)
    query = q.get("q", [""])[0].strip()
    source = q.get("source", ["modrinth"])[0]
    if not query or len(query) > 100 or source not in ("modrinth", "curseforge", "both"):
        raise ApiError(400, "need a search term (and a valid source)")
    if not meta.get("mc") or not meta.get("loader"):
        raise ApiError(400, "download a server jar first so the version and loader are known")
    try:
        return mf.search_rows(query, meta["mc"], meta["loader"], source, 12)
    except mf.McsError as e:
        raise ApiError(502, str(e)) from None


@route("POST", r"/api/instances/([^/]+)/mods")
def api_add_mods(h, m, q, body):
    name = m.group(1)
    inst(name)
    specs = body.get("specs", [])
    if not specs or not all(isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9:_.-]{1,80}", s) for s in specs):
        raise ApiError(400, "bad mod list")
    argv = ["add", name, *specs, "--source", "curseforge" if body.get("source") == "curseforge" else "modrinth"]
    if body.get("allow_beta"):
        argv.append("--allow-beta")
    return cli_job(f"Install {', '.join(specs)}", argv).to_dict()


@route("POST", r"/api/instances/([^/]+)/mods/remove")
def api_remove_mod(h, m, q, body):
    name = m.group(1)
    inst(name)
    key = str(body.get("key", ""))
    if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,80}", key):
        raise ApiError(400, "bad mod key")
    argv = ["remove", name, key] + (["--prune"] if body.get("prune", True) else [])
    return cli_job(f"Remove {key}", argv).to_dict()


@route("POST", r"/api/instances/([^/]+)/mods/update")
def api_update_mods(h, m, q, body):
    name = m.group(1)
    inst(name)
    apply_ = bool(body.get("apply"))
    argv = ["update", name] + (["--apply"] if apply_ else [])
    if body.get("allow_beta"):
        argv.append("--allow-beta")
    return cli_job(("Update mods" if apply_ else "Check for mod updates"), argv).to_dict()


@route("GET", r"/api/jobs/([0-9a-f]+)")
def api_job(h, m, q, body):
    job = JOBS.get(m.group(1))
    if not job:
        raise ApiError(404, "no such task")
    return job.to_dict()


@route("GET", r"/api/instances/([^/]+)/backups")
def api_backups(h, m, q, body):
    name = m.group(1)
    inst(name)
    return mb.list_backups(name)


@route("POST", r"/api/instances/([^/]+)/backups")
def api_backup_create(h, m, q, body):
    name = m.group(1)
    inst(name)
    mode = str((body or {}).get("mode", "live"))
    if mode not in ("live", "clean"):
        raise ApiError(400, "mode must be clean (safe: restarts the server) or live")
    return backup_job(name, mode).to_dict()


def backup_file(name: str, fname: str) -> Path:
    inst(name)
    try:
        return mb.backup_path(name, fname)
    except mb.BackupError as e:
        raise ApiError(404 if "no such" in str(e) else 400, str(e)) from None


@route("DELETE", r"/api/instances/([^/]+)/backups/([^/]+)")
def api_backup_delete(h, m, q, body):
    backup_file(m.group(1), m.group(2)).unlink()
    return {"ok": True}


@route("POST", r"/api/instances/([^/]+)/backups/([^/]+)/restore")
def api_backup_restore(h, m, q, body):
    name, fname = m.group(1), m.group(2)
    backup_file(name, fname)
    if (body or {}).get("confirm") is not True:
        raise ApiError(400, "restoring replaces the server's files; send confirm: true")
    return restore_job(name, fname).to_dict()


@route("POST", r"/api/instances/([^/]+)/backups/([^/]+)/test")
def api_backup_test(h, m, q, body):
    name, fname = m.group(1), m.group(2)
    backup_file(name, fname)
    return test_job(name, fname).to_dict()


@route("GET", "/api/offbox")
def api_offbox(h, m, q, body):
    return mb.offbox_status()


def mod_problems(name: str) -> list[dict]:
    d = inst(name)
    meta = mf.load_meta(d)
    return modcheck.check(d, str(meta.get("mc", "")), read_conf(d).get("JAVA_HOME", ""), str(meta.get("loader", "")))


@route("GET", r"/api/instances/([^/]+)/modcheck")
def api_modcheck(h, m, q, body):
    return mod_problems(m.group(1))


@route("GET", "/api/notices")
def api_notices(h, m, q, body):
    out = mb.notices_all()
    remote = mr.load()
    for name in instance_names():
        if remote.get(name):
            what = " and ".join("RCON" if c == "rcon" else "the management server" for c in remote[name])
            out.append({"id": f"remote:{name}:{'+'.join(remote[name])}", "level": "bad", "server": name, "tab": "settings",
                        "text": f"{name}: remote control is allowed ({what}). It keeps working if a web key is revoked.",
                        "howto": None})
        try:
            problems = [p for p in mod_problems(name) if p["level"] == "bad"]
        except Exception:  # noqa: BLE001 - a odd jar must never break the notices
            continue
        for p in problems[:3]:
            out.append({"id": f"mods:{name}:{p['kind']}:{p['file']}", "level": "bad", "server": name, "tab": "mods",
                        "text": f"{name}: {p['text']} {p['fix']}".strip(), "howto": None})
        if len(problems) > 3:
            out.append({"id": f"mods:{name}:more", "level": "bad", "server": name, "tab": "mods",
                        "text": f"{name}: and {len(problems) - 3} more mod problems.", "howto": None})
    return out


@route("GET", r"/api/instances/([^/]+)/backup-schedule")
def api_backup_schedule(h, m, q, body):
    name = m.group(1)
    inst(name)
    return mb.status(name)


@route("PUT", r"/api/instances/([^/]+)/backup-schedule")
def api_backup_schedule_set(h, m, q, body):
    name = m.group(1)
    inst(name)
    try:
        mb.set_config(name, body or {})
    except mb.BackupError as e:
        raise ApiError(400, str(e)) from None
    return mb.status(name)


@route("GET", r"/api/settings")
def api_settings(h, m, q, body):
    return {"curseforge_available": mf.curseforge_available(),
            "listen": h.server.server_address[0], "port": h.server.server_address[1]}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MCSweb"
    sys_version = ""
    set_cookie: str | None = None
    session_id: str | None = None
    authed = False
    key_label = ""

    def log_message(self, fmt, *args):
        pass  # quiet; failed logins are logged explicitly

    # -- plumbing --
    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
        return name.lower() in ALLOWED_HOSTS

    def _send(self, status: int, payload: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if self.set_cookie:
            self.send_header("Set-Cookie", self.set_cookie)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store"})

    def _check_session(self) -> None:
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)mcs_session=([A-Za-z0-9_-]+)", cookie)
        sid = m.group(1) if m else None
        now = time.time()
        for k in [k for k, v in SESSIONS.items() if v[0] < now]:
            SESSIONS.pop(k, None)
        self.session_id = sid
        self.authed, self.key_label = False, ""
        if sid and sid in SESSIONS:
            key = key_by_id(SESSIONS[sid][1])  # a revoked key ends its sessions at once
            if key:
                self.authed, self.key_label = True, key["label"]
            else:
                SESSIONS.pop(sid, None)

    def _audit(self, path: str, status: int, body) -> None:
        detail = ""
        if isinstance(body, dict):
            if path.endswith("/command"):
                detail = str(body.get("command", ""))
            elif path.endswith("/action"):
                detail = str(body.get("action", ""))
        audit(self.key_label, self.client_address[0], f"{self.command} {path} -> {status}", detail)

    def _dispatch(self) -> None:
        if not self._host_ok():
            return self._json(400, {"error": "bad Host header"})
        self._check_session()
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if not path.startswith("/api/"):
            return self._static(path)
        unsafe = self.command not in ("GET", "HEAD")
        if unsafe:
            origin = self.headers.get("Origin")
            if self.headers.get("X-MCS") != "1" or (origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host")):
                return self._json(403, {"error": "blocked (CSRF check)"})
        matched = False
        for method, pat, fn, auth in ROUTES:
            mt = pat.match(path)
            if not mt:
                continue
            matched = True
            if method != self.command:
                continue
            if auth and not self.authed:
                return self._json(401, {"error": "login required"})
            body = None
            if self.command in ("POST", "PUT") and not path.endswith("/upload"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_JSON:
                    return self._json(413, {"error": "request too large"})
                raw = self.rfile.read(n) or b"{}"
                try:
                    # Deeply nested input raises RecursionError, not ValueError: answer 400 like any other bad body.
                    # NaN and Infinity are not JSON; refuse them rather than let Python's parser accept them.
                    body = json.loads(raw, parse_constant=_no_constant)
                    if not isinstance(body, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    return self._json(400, {"error": "body must be a JSON object"})
            record = unsafe and auth  # who did what: every logged-in change
            try:
                result = fn(self, mt, query, body)
                if record:
                    self._audit(path, 200, body)
                if result is not None:
                    self._json(200, result)
            except ApiError as e:
                if record:
                    self._audit(path, e.status, body)
                self._json(e.status, {"error": e.msg})
            except mf.McsError as e:
                if record:
                    self._audit(path, 400, body)
                self._json(400, {"error": str(e)})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "systemd did not answer in time"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                log(f"{self.command} {path}: {type(e).__name__}: {e}")
                self._json(500, {"error": "internal error (see the MCSweb log)"})
            return
        self._json(405 if matched else 404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch

    # -- static files --
    def _static(self, path: str) -> None:
        if self.command not in ("GET", "HEAD"):
            return self._json(405, {"error": "method not allowed"})
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        p = (WEB / rel).resolve()
        if WEB.resolve() not in p.parents or not p.is_file():
            return self._json(404, {"error": "not found"})
        ctype = {".ttf": "font/ttf", ".woff2": "font/woff2"}.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        # The font is large and never changes: let the browser keep it. Everything else is checked every time.
        self._send(200, p.read_bytes(), ctype, {"Cache-Control": "public, max-age=604800" if p.suffix.lower() in (".ttf", ".woff2") else "no-cache"})

    # -- streaming endpoints (registered below, outside the JSON router) --
    def stream_console(self, name: str, n: int) -> None:
        proc = subprocess.Popen(["journalctl", "-u", f"MCS@{name}.service", "-f", "-n", str(n), "-o", "cat", "--no-pager"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            fd, buf = proc.stdout.fileno(), b""
            while True:
                ready, _, _ = select.select([fd], [], [], 15)
                if not ready:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\n")
                out = b"".join(b"data: " + ANSI.sub(b"", ln).replace(b"\r", b"") + b"\n\n" for ln in lines)
                if out:
                    self.wfile.write(out)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            proc.kill()
            proc.wait()

    def stream_backup(self, name: str, fname: str) -> None:
        p = backup_file(name, fname)
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)


_orig_dispatch = Handler._dispatch


def _dispatch_streams(self):
    """Handle the two streaming GET endpoints, then fall back to the JSON router."""
    if self.command == "GET" and self._host_ok():
        self._check_session()
        url = urllib.parse.urlsplit(self.path)
        m = re.fullmatch(r"/api/instances/([^/]+)/console", url.path)
        b = re.fullmatch(r"/api/instances/([^/]+)/backups/([^/]+)", url.path)
        if m or b:
            if not self.authed:
                return self._json(401, {"error": "login required"})
            try:
                if m:
                    inst(m.group(1))
                    n = int(urllib.parse.parse_qs(url.query).get("n", ["200"])[0])
                    return self.stream_console(m.group(1), max(1, min(n, 2000)))
                return self.stream_backup(b.group(1), b.group(2))
            except ApiError as e:
                return self._json(e.status, {"error": e.msg})
            except ValueError:
                return self._json(400, {"error": "bad request"})
    return _orig_dispatch(self)


Handler.do_GET = Handler.do_HEAD = Handler.do_POST = Handler.do_PUT = Handler.do_DELETE = _dispatch_streams


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["key"], ["audit"]):
        print("Keys and the audit log are managed with: sudo MCSkey add|list|revoke <name> | audit", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="MCSweb", description="Local web interface for MCS. "
                                 "Keys and the audit log: sudo MCSkey.")
    ap.add_argument("--listen", default="127.0.0.1", help="address to bind (default 127.0.0.1; there is no TLS)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--allow-host", action="append", default=[], help="extra Host name to accept (reverse proxy / Tailscale)")
    a = ap.parse_args(argv)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    for old in (OLD_TOKEN_FILE, CONF_DIR / "keys.json", CONF_DIR / "audit.log", CONF_DIR / "audit.log.1"):
        if old.exists():  # leftovers from earlier versions; keys now live in /etc/MCS and are never read from here
            old.unlink()
            log(f"removed old {old.name}")
    if not load_keys():
        log("no access keys yet. Make one on this server:  sudo MCSkey add <name>")
    ALLOWED_HOSTS.update(h.lower() for h in a.allow_host)
    if a.listen not in ("127.0.0.1", "::1", "localhost"):
        log(f"WARNING: listening on {a.listen} without TLS. Anyone who can reach the port can try to guess a key.")
    srv = Server((a.listen, a.port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    log(f"listening on http://{a.listen}:{a.port}/  (keys: sudo MCSkey add <name>)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
