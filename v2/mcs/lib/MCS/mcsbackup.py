#!/usr/bin/env python3
"""MCSbackup - scheduled, checked and restorable backups for MCS. Python 3 stdlib only.

A backup you cannot restore is not a backup, so this does three things:

  * SCHEDULED (the safe way): warn the players, stop the server, copy it while it
    is not running, start it again if it was running. A copy of a stopped server
    is whole; a copy of a running one can be damaged.
  * CHECKED: every archive is read back end to end (gzip checksum + sane paths)
    before it counts as a backup.
  * RESTORABLE: restore first saves the current state as a "prerestore" backup, so
    a restore can itself be undone, then swaps the world in.

File names (per server <n>):
  <n>-YYYYmmdd-HHMMSS.tar.gz            clean copy taken with the server stopped
  <n>-YYYYmmdd-HHMMSS-live.tar.gz       taken while running (can be damaged)
  <n>-YYYYmmdd-HHMMSS-prerestore.tar.gz the state just before a restore

Run by the MCSbackup systemd timer every 10 minutes (`run-due`), by the web page,
or by hand:  MCSbackup --help
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
import mcsfetch as mf  # noqa: E402

HOME, INSTANCES = mf.HOME, mf.INSTANCES
BACKUPS = HOME / "backups" / "instance"
CONF_DIR = HOME / ".config" / "MCS" / "backup"
LOCK = CONF_DIR / ".lock"
STAGING = INSTANCES / ".restore-tmp"  # next to the servers: swapping folders only works inside one filesystem
SKIP_TOP = {"libraries", "versions", "logs", "crash-reports", "cache", ".fabric"}  # re-downloaded on start
GRACE_HOURS = 3  # a scheduled backup that could not start within this long is skipped, never run mid-afternoon
DEFAULTS = {"enabled": True, "time": "04:00", "days": [0, 1, 2, 3, 4, 5, 6], "keep": 7, "keep_weekly": 4, "warn_minutes": 2}
KINDS = ("clean", "live", "prerestore")
STALE_DAYS = 2        # no good backup for this long: tell the owner
TEST_EVERY_DAYS = 7   # the newest backup is test-restored this often, automatically


class BackupError(Exception):
    pass


class Busy(BackupError):
    """Another backup or restore holds the lock. The timer tries again in 10 minutes."""


def say(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Config and state (kept outside the server folder, so a restore never rewinds them)
# --------------------------------------------------------------------------
def _path(name: str) -> Path:
    if not mf.NAME_RE.match(name or ""):
        raise BackupError("invalid server name")
    return CONF_DIR / f"{name}.json"


def _load(name: str) -> dict:
    try:
        data = json.loads(_path(name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _state_lock():
    """The web page and the timer both write these files; take turns."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONF_DIR / ".state.lock", os.O_WRONLY | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(name: str, data: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def get_config(name: str) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in _load(name).get("config", {}).items() if k in DEFAULTS})
    return cfg


def get_state(name: str) -> dict:
    return _load(name).get("state", {})


def set_state(name: str, **kw) -> None:
    with _state_lock():
        data = _load(name)
        data.setdefault("state", {}).update(kw)
        _save(name, data)


def validate_config(raw: dict) -> dict:
    """Check a schedule coming from the web page or the command line."""
    out: dict = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise BackupError("enabled must be true or false")
        out["enabled"] = raw["enabled"]
    if "time" in raw:
        if not isinstance(raw["time"], str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw["time"]):
            raise BackupError("time must look like 04:00 (24 hour clock)")
        out["time"] = raw["time"]
    if "days" in raw:
        d = raw["days"]
        if not isinstance(d, list) or not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6 for x in d):
            raise BackupError("days must be numbers 0 (Monday) to 6 (Sunday)")
        out["days"] = sorted(set(d))
    for key, lo, hi in (("keep", 1, 60), ("keep_weekly", 0, 52), ("warn_minutes", 0, 30)):
        if key in raw:
            v = raw[key]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise BackupError(f"{key} must be a whole number from {lo} to {hi}")
            out[key] = v
    return out


def set_config(name: str, raw: dict) -> dict:
    new = validate_config(raw)
    with _state_lock():
        data = _load(name)
        data.setdefault("config", {}).update(new)
        _save(name, data)
    return get_config(name)


# --------------------------------------------------------------------------
# When is a backup due?
# --------------------------------------------------------------------------
def last_scheduled(cfg: dict, now: dt.datetime) -> dt.datetime | None:
    """The most recent scheduled moment at or before `now` (looks back one week)."""
    hh, mm = (int(x) for x in cfg["time"].split(":"))
    for back in range(8):
        day = (now - dt.timedelta(days=back)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day <= now and day.weekday() in cfg["days"]:
            return day
    return None


def next_scheduled(cfg: dict, now: dt.datetime) -> dt.datetime | None:
    if not cfg["enabled"] or not cfg["days"]:
        return None
    hh, mm = (int(x) for x in cfg["time"].split(":"))
    for ahead in range(8):
        day = (now + dt.timedelta(days=ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day > now and day.weekday() in cfg["days"]:
            return day
    return None


def is_due(cfg: dict, state: dict, now: dt.datetime) -> tuple[bool, str]:
    """(run it?, why not / what happened). The same slot never runs twice."""
    if not cfg["enabled"]:
        return False, "off"
    sched = last_scheduled(cfg, now)
    if sched is None:
        return False, "no days chosen"
    if state.get("last_sched") == sched.isoformat():
        return False, "already handled"
    if now - sched > dt.timedelta(hours=GRACE_HOURS):
        return False, "missed"
    return True, sched.isoformat()


# --------------------------------------------------------------------------
# systemd helpers
# --------------------------------------------------------------------------
def _sc(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)


def unit_state(name: str) -> str:
    return _sc("is-active", f"MCS@{name}.service").stdout.strip() or "unknown"


def console(name: str, text: str) -> bool:
    """Send one line to the server console (FIFO). False if the server has none."""
    path = f"/run/MCS@{name}.stdin"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (text.strip() + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class NotStopped(BackupError):
    """systemctl would not stop the server, so it is still running as it was."""


def systemctl_error(r: subprocess.CompletedProcess) -> str:
    """What systemctl said when it failed. A refusal also gets a pointer to what decides it."""
    said = (r.stderr or r.stdout).strip() or f"systemctl failed (exit {r.returncode})"
    if re.search(r"access denied|authentication required", said, re.IGNORECASE):
        said += ("\nThe system refused permission. polkit decides this: look in /etc/polkit-1/rules.d/ "
                 "(MCS installs 50-mcs.rules; another rule there can override or break it) and run: "
                 "sudo journalctl -u polkit")
    return said


def stop_server(name: str) -> None:
    say(f"stopping {name} (it saves the world first)...")
    r = None
    with contextlib.suppress(subprocess.TimeoutExpired):
        r = _sc("stop", f"MCS@{name}.service", timeout=240)
    if r and r.returncode != 0:
        state = unit_state(name)
        if is_up(state) and state != "deactivating":  # not on its way down: waiting will not help
            raise NotStopped(f"{name} could not be stopped, so nothing was done: {systemctl_error(r)}")
    for _ in range(60):
        if unit_state(name) not in ("active", "activating", "deactivating"):
            return
        time.sleep(2)
    raise BackupError("the server did not stop; nothing was backed up")


def start_server(name: str) -> None:
    say(f"starting {name} again...")
    _sc("reset-failed", f"MCS@{name}.service", f"MCS@{name}.socket")
    try:
        r = _sc("start", f"MCS@{name}.service", timeout=420)
    except subprocess.TimeoutExpired:
        raise BackupError(f"the backup is safe, but {name} is taking too long to start again") from None
    if r.returncode != 0:
        raise BackupError(f"the backup is safe, but {name} did not start again: {systemctl_error(r)}")


def restart_after(name: str) -> None:
    """Start the server again and forget that we stopped it."""
    start_server(name)
    set_state(name, stopped_for_backup=0)


def stop_for_copy(name: str) -> None:
    """Stop a running server before a backup or restore. The note is made first: if this process dies
    now (restart of the web page, power cut), the timer sees it and starts the server again."""
    set_state(name, stopped_for_backup=int(time.time()), recover_tries=0)
    try:
        stop_server(name)
    except NotStopped:
        set_state(name, stopped_for_backup=0)  # we did not stop it: never "start it again" later
        raise


def is_up(state: str) -> bool:
    """Anything but cleanly stopped or crashed counts as running."""
    return state not in ("inactive", "failed", "unknown")


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def players_online(name: str) -> int | None:
    """How many players are on, asked the way the game's own server list asks. None if it cannot be told."""
    try:
        port = int(mf.read_prop(INSTANCES / name, "server-port", "25565") or 25565)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sk:
            sk.settimeout(3)

            def varint() -> int:
                n = shift = 0
                while True:
                    b = sk.recv(1)
                    if not b:
                        raise OSError("closed")
                    n |= (b[0] & 0x7F) << shift
                    shift += 7
                    if not b[0] & 0x80:
                        return n

            host = b"localhost"
            hs = b"\x00" + _varint(767) + _varint(len(host)) + host + struct.pack(">H", port) + b"\x01"
            sk.sendall(_varint(len(hs)) + hs + b"\x01\x00")
            varint(); varint()  # packet length, packet id
            size = varint()
            if size > 1 << 20:
                return None
            data = b""
            while len(data) < size:
                c = sk.recv(size - len(data))
                if not c:
                    raise OSError("closed")
                data += c
            return int(json.loads(data)["players"]["online"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def countdown(name: str, minutes: int) -> None:
    """Tell the players the server is about to restart, then wait."""
    if minutes <= 0:
        return
    if players_online(name) == 0:
        say("nobody is online, so no warning is needed")
        return
    if not console(name, "list"):
        return
    total = minutes * 60
    plural = "s" if minutes != 1 else ""
    console(name, f"say Backup time! The server will restart in {minutes} minute{plural}. It is back in a few minutes. Find a safe spot.")
    waited = 0
    for mark in sorted({t for t in (60, 15) if t < total}, reverse=True):
        time.sleep(total - mark - waited)
        waited = total - mark
        console(name, f"say Server restarting for a backup in {mark} seconds.")
    time.sleep(total - waited)


# --------------------------------------------------------------------------
# Archives
# --------------------------------------------------------------------------
def backup_re(name: str) -> re.Pattern:
    return re.compile(re.escape(name) + r"-(\d{8}-\d{6})(-live|-prerestore)?\.tar\.gz")


def kind_of(name: str, fname: str) -> str | None:
    m = backup_re(name).fullmatch(fname)
    if not m:
        return None
    return {None: "clean", "-live": "live", "-prerestore": "prerestore"}[m.group(2)]


def list_backups(name: str) -> list[dict]:
    out = []
    if BACKUPS.is_dir():
        for p in BACKUPS.iterdir():
            kind = kind_of(name, p.name)
            if kind and p.is_file():
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size, "time": int(st.st_mtime), "kind": kind})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def backup_path(name: str, fname: str) -> Path:
    if not mf.NAME_RE.match(name or "") or kind_of(name, fname) is None:
        raise BackupError("bad backup name")
    p = BACKUPS / fname
    if not p.is_file():
        raise BackupError("no such backup")
    return p


def _keep_filter(skipped: list):
    def keep(ti: tarfile.TarInfo):
        parts = ti.name.split("/")
        if len(parts) > 1 and parts[1] in SKIP_TOP:
            return None
        if ti.name.endswith(".part"):
            return None
        if not (ti.isfile() or ti.isdir()):  # links, sockets, devices: not copied; the owner is told
            if len(skipped) < 20:
                skipped.append("/".join(parts[1:]) or ti.name)
            return None
        return ti
    return keep


def _tree_size(d: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(d):
        if Path(root) == d:
            dirs[:] = [x for x in dirs if x not in SKIP_TOP]
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


def newest_change(d: Path) -> float:
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = os.lstat(d).st_mtime
    for root, dirs, files in os.walk(d):
        if Path(root) == d:
            dirs[:] = [x for x in dirs if x not in SKIP_TOP]
        for f in files + dirs:  # a folder's own time moves when a file in it is added or deleted
            with contextlib.suppress(OSError):
                newest = max(newest, os.lstat(os.path.join(root, f)).st_mtime)
    return newest


def verify_archive(name: str, path: Path) -> int:
    """Read the whole archive (checks the gzip checksum) and refuse odd paths.
    Returns the number of files inside."""
    return _verify(name, path)[0]


def _verify(name: str, path: Path) -> tuple[int, int]:
    """(files, bytes once unpacked)"""
    files = total = 0
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf:
                parts = m.name.split("/")
                if m.name.startswith("/") or ".." in parts or parts[0] != name:
                    raise BackupError(f"unsafe or foreign path inside the archive: {m.name}")
                if not (m.isfile() or m.isdir()):
                    raise BackupError(f"the archive holds something that is not a file or folder: {m.name}")
                if m.isfile():
                    files += 1
                    total += m.size
                    src = tf.extractfile(m)
                    while src and src.read(1 << 20):
                        pass
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f"the archive is damaged: {e}") from None
    if files == 0:
        raise BackupError("the archive is empty")
    return files, total


def make_archive(name: str, kind: str = "clean", skipped: list | None = None) -> Path:
    """Archive the server folder. The caller decides whether the server is stopped."""
    d = INSTANCES / name
    if not d.is_dir():
        raise BackupError(f"no server called {name}")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    need = _tree_size(d)
    free = shutil.disk_usage(BACKUPS).free
    if free < need:
        raise BackupError(f"not enough disk space: need about {need // 2**20} MB free, have {free // 2**20} MB")
    suffix = {"clean": "", "live": "-live", "prerestore": "-prerestore"}[kind]
    when = time.time()
    while True:  # never overwrite an existing backup, even one made in the same second
        final = BACKUPS / f"{name}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(when))}{suffix}.tar.gz"
        if not final.exists():
            break
        when += 1
    fd, tmp = tempfile.mkstemp(prefix=".mcs-", suffix=".part", dir=BACKUPS)
    os.close(fd)
    try:
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(d, arcname=name, filter=_keep_filter(skipped if skipped is not None else []))
        verify_archive(name, Path(tmp))
        os.chmod(tmp, 0o660)
        os.replace(tmp, final)
    except BaseException as e:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        if isinstance(e, (OSError, tarfile.TarError)):
            raise BackupError(f"could not write the backup: {e}") from None
        raise
    return final


def choose_keep(names: list[str], keep: int, weekly: int) -> set[str]:
    """Which clean backups to keep. `names` are newest first.
    The newest `keep` of them, plus the newest backup of each of the `weekly` most recent
    older weeks (a week already covered by the newest ones does not count again)."""
    chosen = set(names[:keep])

    def week(fname: str):
        m = re.search(r"(\d{8})-\d{6}", fname)
        return dt.datetime.strptime(m.group(1), "%Y%m%d").isocalendar()[:2] if m else None

    seen = {week(n) for n in names[:keep]}
    picked = 0
    for n in names[keep:]:
        wk = week(n)
        if picked >= weekly:
            break
        if wk is not None and wk not in seen:
            seen.add(wk)
            chosen.add(n)
            picked += 1
    return chosen


def prune(name: str, keep: int, weekly: int = 0) -> list[str]:
    """Daily backups: the newest `keep`. Plus one a week for `weekly` older weeks. 3 prerestore ones.
    Quick (live) copies are only ever deleted by hand."""
    gone = []
    every = list_backups(name)
    clean = [b["name"] for b in every if b["kind"] == "clean"]
    keepers = choose_keep(clean, keep, weekly)
    keepers |= {b["name"] for b in [b for b in every if b["kind"] == "prerestore"][:3]}
    for b in every:
        if b["kind"] in ("clean", "prerestore") and b["name"] not in keepers:
            with contextlib.suppress(OSError):
                (BACKUPS / b["name"]).unlink()
                gone.append(b["name"])
    return gone


# --------------------------------------------------------------------------
# The jobs
# --------------------------------------------------------------------------
@contextlib.contextmanager
def exclusive():
    """One backup or restore at a time, across the timer, the web page and the shell."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT, 0o660)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("another backup or restore is running right now") from None
        yield
    finally:
        os.close(fd)


def backup_clean(name: str, warn_minutes: int | None = None, only_if_changed: bool = False) -> dict:
    """Stop (if running), copy, check, start again; remember how it went for the web page."""
    try:
        res = _backup_clean(name, warn_minutes, only_if_changed)
    except Busy:
        raise
    except BackupError as e:
        set_state(name, last_status="failed", last_time=int(time.time()), last_message=str(e))
        raise
    kw = {"last_status": res["result"], "last_time": int(time.time()), "last_message": res["message"]}
    if res["result"] == "ok":  # an 'unchanged, skipped' run must not wipe what the last real one found
        kw.update(last_downtime=res["downtime"], last_file=res["file"], last_skipped=res.get("skipped", []))
    set_state(name, **kw)
    return res


def _backup_clean(name: str, warn_minutes: int | None, only_if_changed: bool) -> dict:
    d = INSTANCES / name
    if not d.is_dir():
        raise BackupError(f"no server called {name}")
    cfg = get_config(name)
    warn = cfg["warn_minutes"] if warn_minutes is None else warn_minutes
    with exclusive():
        state = unit_state(name)
        for _ in range(150):  # let a server that is still starting finish first
            if state != "activating":
                break
            time.sleep(2)
            state = unit_state(name)
        running = is_up(state)  # still "activating" after the wait counts too: stop it, never copy a half-started server
        if only_if_changed and not running:
            last = [b for b in list_backups(name) if b["kind"] == "clean"]
            if last and newest_change(d) <= last[0]["time"]:
                say(f"{name}: not running and unchanged since the last backup; nothing to do")
                return {"result": "skipped", "message": "unchanged since the last backup"}
        down_start = None
        final: Path | None = None
        skipped: list = []
        problem: Exception | None = None
        try:
            if running:
                countdown(name, warn)
                down_start = time.time()
                try:
                    stop_for_copy(name)
                except NotStopped:
                    running = False  # it never went down, so there is nothing to start again
                    raise
            say(f"copying {name}...")
            final = make_archive(name, "clean", skipped)
            files = verify_archive(name, final)
            if is_up(unit_state(name)):  # someone started it while we copied: this is not a clean copy
                with contextlib.suppress(OSError):
                    final.unlink()
                final = None
                raise BackupError("the server was started while it was being copied, so the copy was thrown away. Try again")
            say(f"saved {final.name} ({final.stat().st_size / 1e6:.1f} MB, {files} files, checked)")
        except Exception as e:  # noqa: BLE001 - always restart the server, then report
            problem = e
        finally:
            if running:
                try:
                    restart_after(name)
                except BackupError as e:
                    problem = problem or e
        down = int(time.time() - down_start) if down_start else 0
        if problem:
            raise problem
        gone = prune(name, cfg["keep"], cfg["keep_weekly"])
        if gone:
            say(f"removed {len(gone)} older backup(s): keeping the newest {cfg['keep']} plus one a week for {cfg['keep_weekly']} older weeks")
        if skipped:
            say(f"NOT copied (links or special files): {', '.join(skipped)}")
        return {"result": "ok", "file": final.name if final else "", "downtime": down, "skipped": skipped,
                "message": f"saved {final.name}" if final else ""}


def backup_live(name: str) -> Path:
    """Copy a running server without stopping it. Can be damaged: say so."""
    d = INSTANCES / name
    if not d.is_dir():
        raise BackupError(f"no server called {name}")
    with exclusive():
        running = is_up(unit_state(name))
        if running:
            say("asking the running server to flush its world to disk...")
            console(name, "save-off")
            console(name, "save-all flush")
            time.sleep(8)
        try:
            final = make_archive(name, "live" if running else "clean")
        finally:
            if running:
                console(name, "save-on")
        say(f"saved {final.name} ({final.stat().st_size / 1e6:.1f} MB)")
        if running:
            say("taken while the server was running: usually fine, but it can be damaged. The scheduled backup is the safe one.")
        say("not included: libraries, versions, logs (re-downloaded automatically)")
        return final


def test_backup(name: str, fname: str) -> dict:
    """Unpack a backup into a scratch folder and check it is whole. The live server is not touched."""
    src = backup_path(name, fname)
    with exclusive():
        say(f"testing {fname}: reading every file back...")
        res = _test(name, src)
    set_state(name, last_test=dict(res, time=int(time.time()), file=fname))
    say(res["message"])
    return res


def _region_cut_short(mca: Path) -> bool:
    """Is data missing from this region file? An empty file is fine. Minecraft does not always round the
    file up to a full 4096-byte block, so an odd size alone means nothing: then follow the table at the
    front and check that every chunk it lists is all there. A file of whole blocks is accepted without
    looking inside, and chunks are never unpacked."""
    size = mca.stat().st_size
    if size % 4096 == 0 and size != 4096:
        return False
    if size < 8192:  # too short to hold the two tables every region file starts with
        return True
    with mca.open("rb") as f:
        table = f.read(4096)
        for i in range(0, 4096, 4):
            start = int.from_bytes(table[i:i + 3], "big") * 4096  # 3 bytes: which block; 1 byte: how many
            if not any(table[i:i + 4]):
                continue
            f.seek(start)
            length = int.from_bytes(f.read(4), "big")  # each chunk begins with its own length
            if start < 8192 or length == 0 or start + 4 + length > size:
                return True
    return False


def _test(name: str, src: Path) -> dict:
    try:
        files, unpacked = _verify(name, src)
    except BackupError as e:
        return {"ok": False, "message": f"This backup is damaged: {e}"}
    free = shutil.disk_usage(INSTANCES).free
    if free < unpacked:
        return {"ok": None, "message": f"The file reads fine ({files} files), but there is not enough free space to unpack it for the full test (needs about {unpacked // 2**20} MB, has {free // 2**20} MB)."}
    scratch = _scratch("test", name, int(time.time()))
    try:
        scratch.mkdir(parents=True)
        with tarfile.open(src, "r:gz") as tf:
            _safe_extract(tf, scratch)
        root = scratch / name
        level = mf.read_prop(root, "level-name", "world") or "world"
        world = root / level
        problems: list[str] = []
        regions = 0
        if (world / "level.dat").is_file():
            try:
                with gzip.open(world / "level.dat", "rb") as g:
                    if g.read(1) != b"\x0a":
                        problems.append("level.dat is not a Minecraft world file")
            except (OSError, EOFError) as e:
                problems.append(f"level.dat cannot be read ({e})")
            for mca in world.rglob("*.mca"):
                regions += 1
                if _region_cut_short(mca):
                    problems.append(f"{mca.relative_to(root)} is damaged (part of it is missing)")
        elif (INSTANCES / name / level / "level.dat").is_file():
            problems.append(f"the world folder '{level}' is missing from the backup")
        mods = 0
        if (root / "mods").is_dir():
            for jar in sorted((root / "mods").glob("*.jar")):
                mods += 1
                if not zipfile.is_zipfile(jar):
                    problems.append(f"mods/{jar.name} is damaged")
        if problems:
            return {"ok": False, "message": "Problem found: " + "; ".join(problems[:5])}
        return {"ok": True, "message": f"Backup tested: {files} files unpacked, world '{level}' readable ({regions} region files), {mods} mod(s) intact. "
                                       "This shows the backup is whole and readable. It does not start Minecraft from it."}
    except (OSError, tarfile.TarError, BackupError) as e:
        return {"ok": False, "message": f"The test could not unpack it: {e}"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def maybe_test(name: str, fname: str) -> None:
    """Weekly, after a good backup: test it, so a bad backup is found long before it is needed."""
    last = (get_state(name).get("last_test") or {}).get("time", 0)
    if time.time() - last < TEST_EVERY_DAYS * 86400:
        return
    try:
        test_backup(name, fname)
    except Busy:
        pass
    except BackupError as e:
        say(f"{name}: test skipped: {e}")


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    for m in tf:
        parts = m.name.split("/")
        if m.name.startswith("/") or ".." in parts:
            raise BackupError(f"unsafe path in the archive: {m.name}")
        target = dest / m.name
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o2770)
        elif m.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                src = tf.extractfile(m)
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o660)
        else:
            raise BackupError(f"unexpected item in the archive: {m.name}")


def _scratch(kind: str, name: str, stamp: int) -> Path:
    """A folder in STAGING. kind: new (being unpacked), old (the folder just swapped out), test.
    '~' cannot be part of a server name, so what a folder is can never be mistaken."""
    return STAGING / f"{kind}~{name}~{stamp}"


def _move_skipped(src: Path, dest: Path) -> None:
    """Move the folders backups leave out (libraries, logs...) from one copy of a server to another."""
    for sub in SKIP_TOP:
        if (src / sub).exists() and not (dest / sub).exists():
            os.rename(src / sub, dest / sub)


def clean_leftovers() -> None:
    """A restore or test that was cut off (crash, power cut, web page restarted) leaves folders in
    STAGING. Put back whatever a server still needs, then delete the rest. Anything unclear is kept."""
    if not STAGING.is_dir():
        return
    try:
        with exclusive():  # restores and tests hold this lock, so anything found now was abandoned
            found: dict = {}
            for p in STAGING.iterdir():
                m = re.fullmatch(r"(new|old|test)~([^~]+)~(\d+)", p.name)
                if m and mf.NAME_RE.match(m.group(2)) and p.is_dir() and not p.is_symlink():
                    found.setdefault((m.group(2), m.group(3)), {})[m.group(1)] = p
            for (name, _stamp), parts in sorted(found.items()):
                try:
                    _clean_one(name, parts)
                except OSError as e:
                    say(f"{name}: could not tidy up after an interrupted restore, left in {STAGING}: {e}")
    except Busy:
        return  # something is using STAGING right now; next time


def _clean_one(name: str, parts: dict) -> None:
    d = INSTANCES / name
    old, new = parts.get("old"), parts.get("new")
    unswapped = new / name if new and (new / name).is_dir() else None  # unpacked, but never moved into place
    cut_off = unswapped is not None
    if old and not d.exists():  # stopped between the two halves of the swap: this is the only live copy
        os.rename(old, d)
        old = None
        cut_off = True
        say(f"{name}: a restore was interrupted; the server folder has been put back as it was")
    elif old and unswapped:  # the folder was swapped out, yet something else now has its name
        say(f"{name}: not touching {old}: it may be the only copy of the server from before an interrupted restore. "
            "Check it, then delete it by hand")
        return
    if unswapped and d.is_dir():
        _move_skipped(unswapped, d)
    if cut_off and d.is_dir():
        set_state(name, restore_interrupted=int(time.time()))
    for p in (old, new, parts.get("test")):
        if p:
            shutil.rmtree(p, ignore_errors=True)


def restore(name: str, fname: str) -> dict:
    """Put a backup back. The current state is saved first so this can be undone."""
    src = backup_path(name, fname)
    d = INSTANCES / name
    if not d.is_dir():
        raise BackupError(f"no server called {name}")
    with exclusive():
        say(f"checking {fname}...")
        _, unpacked = _verify(name, src)
        free = shutil.disk_usage(INSTANCES).free  # the unpacked copy sits beside the server until the swap
        if free < unpacked:
            raise BackupError(f"not enough disk space to restore: need about {unpacked // 2**20} MB free, have {free // 2**20} MB. Nothing was changed")
        running = is_up(unit_state(name))
        stamp = int(time.time())
        staging = _scratch("new", name, stamp)
        old = None
        try:
            if running:
                try:
                    stop_for_copy(name)
                except NotStopped:
                    running = False
                    raise
            say("saving the current state first, so this restore can be undone...")
            pre = make_archive(name, "prerestore")
            say(f"saved {pre.name}")
            staging.mkdir(parents=True, exist_ok=True)
            say("unpacking...")
            with tarfile.open(src, "r:gz") as tf:
                _safe_extract(tf, staging)
            new = staging / name
            if not new.is_dir():
                raise BackupError("the archive does not contain the server folder")
            _move_skipped(d, new)  # keep downloaded libraries so the server can start offline
            old = _scratch("old", name, stamp)
            os.rename(d, old)
            try:
                os.rename(new, d)
            except OSError:
                os.rename(old, d)
                old = None
                raise
            set_state(name, restore_interrupted=0)
            say("restored.")
        except Exception as e:
            if running:  # the restore failed before the swap: put the server back as it was
                with contextlib.suppress(BackupError):
                    restart_after(name)
            if isinstance(e, (OSError, tarfile.TarError)):
                raise BackupError(f"the restore failed and nothing was changed: {e}") from None
            raise
        finally:
            if old and not d.exists():  # never leave the server without a folder
                with contextlib.suppress(OSError):
                    os.rename(old, d)
                    old = None
            if (staging / name).is_dir() and d.is_dir():  # no swap: give the server back what was moved out of it
                with contextlib.suppress(OSError):
                    _move_skipped(staging / name, d)
            shutil.rmtree(staging, ignore_errors=True)
        if old:
            shutil.rmtree(old, ignore_errors=True)
        if running:
            restart_after(name)
        return {"result": "ok", "undo": pre.name}


def recover(name: str, state: dict) -> None:
    """A backup or restore stopped this server and never started it again (the web page was
    restarted, the power went). Start it, a few tries at most."""
    try:
        with exclusive():
            if is_up(unit_state(name)):
                set_state(name, stopped_for_backup=0)
                return
            tries = int(state.get("recover_tries", 0))
            if tries >= 6:
                return
            set_state(name, recover_tries=tries + 1)
            say(f"{name}: it was stopped for a backup or restore that never finished; starting it again")
            restart_after(name)
            set_state(name, last_status="warning", last_time=int(time.time()),
                      last_message="The server was left stopped by an interrupted backup or restore, so it was started again.")
    except Busy:
        return  # a backup is running right now; it will start the server itself
    except BackupError as e:
        say(f"{name}: could not start it again: {e}")
        if int(state.get("recover_tries", 0)) + 1 >= 6:
            set_state(name, last_status="failed", last_time=int(time.time()),
                      last_message=f"The server was stopped for a backup and could not be started again: {e}. Start it by hand.")


def run_due() -> int:
    """Called by the timer. Handles every server whose slot has come."""
    rc = 0
    now = dt.datetime.now()
    try:
        clean_leftovers()  # first: a server may be missing its folder until this puts it back
    except Exception as e:  # noqa: BLE001 - tidying must never stop the backups
        say(f"could not tidy up after an interrupted restore: {e}")
    for d in sorted(INSTANCES.iterdir()) if INSTANCES.is_dir() else []:
        name = d.name
        if not d.is_dir() or not mf.NAME_RE.match(name):
            continue
        try:
            if _run_one(name, now):
                rc = 1
        except Exception as e:  # noqa: BLE001 - one server's trouble must not stop the others' backups
            say(f"{name}: unexpected error: {e}")
            rc = 1
    return rc


def _run_one(name: str, now: dt.datetime) -> bool:
    """One server's turn. True if its backup failed."""
    cfg, state = get_config(name), get_state(name)
    if state.get("stopped_for_backup"):
        recover(name, state)
        state = get_state(name)
    if "first_seen" not in state:
        set_state(name, first_seen=int(time.time()))
    if "last_sched" not in state:  # first time this server is seen: start from the next slot, don't call earlier ones "missed"
        sched = last_scheduled(cfg, now)
        set_state(name, last_sched=sched.isoformat() if sched else "")
        return False
    due, why = is_due(cfg, state, now)
    if not due:
        if why == "missed":
            sched = last_scheduled(cfg, now)
            if sched and state.get("last_sched") != sched.isoformat():
                set_state(name, last_sched=sched.isoformat(), last_status="missed", last_time=int(time.time()),
                          last_message=f"The {cfg['time']} backup was skipped: the machine was off or busy for over {GRACE_HOURS} hours.")
                say(f"{name}: missed the {cfg['time']} backup (skipped, not run mid-day)")
        return False
    say(f"{name}: scheduled backup ({why})")
    prev = state.get("last_sched")
    set_state(name, last_sched=why)  # one try per slot: a failing backup must not stop the server every 10 minutes
    try:
        res = backup_clean(name, only_if_changed=True)
        if res["result"] == "ok":
            maybe_test(name, res["file"])
    except Busy as e:  # nothing was touched, so this slot is still open: try again next time
        set_state(name, last_sched=prev)
        say(f"{name}: {e}; will try again in 10 minutes")
    except Exception as e:  # noqa: BLE001
        say(f"{name}: backup FAILED: {e}")
        return True
    return False


def notices(name: str) -> list[dict]:
    """Things the owner should hear about, for the banner at the top of the web page."""
    cfg, state = get_config(name), get_state(name)
    out: list[dict] = []

    def add(level: str, key: str, text: str) -> None:
        out.append({"id": f"{name}:{key}", "level": level, "server": name, "text": text, "tab": "backups"})

    cut = state.get("restore_interrupted") or 0
    if time.time() - cut < 7 * 86400:  # shown for a week, or until a restore succeeds
        add("warn", f"interrupted-{cut}", f"A restore of {name} was interrupted and did not happen: the server is as it was before. Restore again if you still want to.")
    if not cfg["enabled"]:
        add("info", "off", f"Automatic backups are off for {name}. If something goes wrong there would be nothing to go back to.")
        return out
    now = time.time()
    clean = [b for b in list_backups(name) if b["kind"] == "clean"]
    if state.get("last_status") == "failed":
        add("bad", "failed", f"The last backup of {name} failed: {state.get('last_message', '')}")
    elif clean and (now - clean[0]["time"]) > STALE_DAYS * 86400:
        add("bad", "old", f"The newest good backup of {name} is {int((now - clean[0]['time']) // 86400)} days old.")
    elif not clean and now - state.get("first_seen", now) > STALE_DAYS * 86400:
        add("bad", "none", f"{name} has no backup yet.")
    if state.get("last_skipped"):
        names = ", ".join(state["last_skipped"][:5]) + (" and more" if len(state["last_skipped"]) > 5 else "")
        add("warn", "skipped", f"Some things in {name} are not being backed up: {names}. They are links or special files, which MCS does not copy. Replace each with a real file or folder.")
    t = state.get("last_test") or {}
    if t.get("ok") is False:
        add("bad", "test", f"The last test of a {name} backup failed: {t.get('message', '')}")
    return out


OFFBOX_CONF = Path("/etc/MCS/offbox.json")
OFFBOX_STATUS = Path("/etc/MCS/offbox-status.json")
OFFBOX_HOWTO = [
    "Why: backups kept on the same computer disappear if its disk dies, the computer is stolen, or someone (or a bad mod) deletes them. A copy somewhere else is the real safety net.",
    "What you need: a second drive (a USB drive, or a second disk in the computer) or another computer on your network. Cloud storage works too if it shows up as a folder.",
    "1. Plug the drive in and make sure it is mounted: it should show up as a folder, usually under /media or /mnt. If you are not sure how, ask whoever set up the computer.",
    "2. On the server computer (at its keyboard, or over ssh) run: `sudo MCSoffbox set /path/to/that/folder`",
    "3. MCSoffbox refuses a folder that the Minecraft user or this web page could change, and a folder on the same disk as the servers. That is on purpose: it is what keeps the copy safe.",
    "4. Check it any time with `sudo MCSoffbox status`. This notice goes away once the first copy works.",
    "It copies new backups every hour and never deletes anything on the drive. When the drive fills up, you clear out old copies yourself.",
]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def offbox_status() -> dict:
    """The separate (off-box) copy: is it set up, and is it working? Written by the root-owned MCSoffbox."""
    conf = _read_json(OFFBOX_CONF)
    if not conf:
        return {"configured": False, "howto": OFFBOX_HOWTO}
    st = _read_json(OFFBOX_STATUS) or {}
    keys = ("last_ok", "last_run", "copied_files", "missing", "same_disk", "dest_free", "newest_size", "error", "copied_total")
    return {"configured": True, "dest": conf.get("dest"), **{k: st.get(k) for k in keys}}


def offbox_notices() -> list[dict]:
    s = offbox_status()

    def note(level: str, key: str, text: str, howto: list[str] | None = None) -> dict:
        return {"id": f"offbox:{key}", "level": level, "server": None, "tab": "backups", "text": text, "howto": howto}

    if not s["configured"]:
        return [note("info", "none", "Your backups are only on this computer. If its disk dies, or someone deletes them, they are gone too. A separate copy takes about five minutes to set up.", OFFBOX_HOWTO)]
    out = []
    now = time.time()
    if s.get("error"):
        out.append(note("bad", "error", f"The separate copy of your backups is not working: {s['error']}"))
    elif s.get("last_ok") and now - s["last_ok"] > STALE_DAYS * 86400:
        out.append(note("bad", "stale", f"The separate copy of your backups last worked {int((now - s['last_ok']) // 86400)} days ago."))
    if s.get("same_disk"):
        out.append(note("warn", "samedisk", "The separate copy is on the same disk as the servers. It protects against deletes and mistakes, not against the disk dying. A second drive is better."))
    if s.get("dest_free") is not None and s.get("newest_size") and s["dest_free"] < 3 * s["newest_size"]:
        out.append(note("warn", "full", "The drive holding the separate copy is almost full. Clear out some old copies there; MCS never deletes them."))
    return out


def notices_all() -> list[dict]:
    out: list[dict] = []
    servers = [d for d in (sorted(INSTANCES.iterdir()) if INSTANCES.is_dir() else []) if d.is_dir() and mf.NAME_RE.match(d.name)]
    for d in servers:
        out += notices(d.name)
    if servers:  # nothing to back up yet means nothing to nag about
        out += offbox_notices()
    return out


def status(name: str) -> dict:
    cfg, state = get_config(name), get_state(name)
    nxt = next_scheduled(cfg, dt.datetime.now())
    return {"config": cfg, "state": state, "next": nxt.isoformat(timespec="minutes") if nxt else None,
            "grace_hours": GRACE_HOURS}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="MCSbackup", description="Scheduled, checked, restorable backups for MCS.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-due", help="run every backup whose time has come (the timer does this)")
    p = sub.add_parser("now", help="safe backup now: warn, stop, copy, start again")
    p.add_argument("instance")
    p.add_argument("--warn", type=int, help="minutes of warning (default: the server's setting)")
    p = sub.add_parser("live", help="quick backup while running (can be damaged)")
    p.add_argument("instance")
    p = sub.add_parser("list", help="list backups")
    p.add_argument("instance")
    p = sub.add_parser("restore", help="restore a backup (saves the current state first)")
    p.add_argument("instance")
    p.add_argument("file")
    p = sub.add_parser("verify", help="check a backup can be read back")
    p.add_argument("instance")
    p.add_argument("file")
    p = sub.add_parser("test", help="unpack a backup into a scratch folder and check it is whole")
    p.add_argument("instance")
    p.add_argument("file")
    p = sub.add_parser("schedule", help="show or change the schedule")
    p.add_argument("instance")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--time", help="HH:MM, 24 hour clock")
    p.add_argument("--days", help="comma list, 0=Monday .. 6=Sunday, e.g. 0,1,2,3,4,5,6")
    p.add_argument("--keep", type=int, help="how many of the newest backups to keep")
    p.add_argument("--weekly", type=int, dest="keep_weekly", help="plus one a week for this many older weeks")
    p.add_argument("--warn", type=int, dest="warn_minutes")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # let the 'start the server again' step run
    try:
        if a.cmd == "run-due":
            return run_due()
        if a.cmd == "now":
            backup_clean(a.instance, a.warn)
        elif a.cmd == "live":
            backup_live(a.instance)
        elif a.cmd == "list":
            for b in list_backups(a.instance):
                print(f"{b['name']:<52} {b['kind']:<11} {b['size'] / 1e6:8.1f} MB")
        elif a.cmd == "restore":
            res = restore(a.instance, a.file)
            say(f"If that was a mistake, restore {res['undo']} to go back.")
        elif a.cmd == "verify":
            n = verify_archive(a.instance, backup_path(a.instance, a.file))
            say(f"ok: {n} files, readable end to end")
        elif a.cmd == "test":
            if test_backup(a.instance, a.file)["ok"] is False:
                return 1
        elif a.cmd == "schedule":
            raw = {}
            if a.on or a.off:
                raw["enabled"] = bool(a.on and not a.off)
            if a.time:
                raw["time"] = a.time
            if a.days is not None:
                raw["days"] = [int(x) for x in a.days.split(",") if x != ""]
            if a.keep is not None:
                raw["keep"] = a.keep
            if a.keep_weekly is not None:
                raw["keep_weekly"] = a.keep_weekly
            if a.warn_minutes is not None:
                raw["warn_minutes"] = a.warn_minutes
            if raw:
                set_config(a.instance, raw)
            print(json.dumps(status(a.instance), indent=1))
    except (BackupError, ValueError) as e:
        print(f"MCSbackup: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
