#!/usr/bin/env python3
"""MCSoffbox - keep a SEPARATE copy of the backups where the minecraft user and the web page cannot touch it.

Installed by MCSuser as a ROOT-OWNED copy (/usr/local/lib/MCS/mcsoffbox.py, run through
/usr/local/bin/MCSoffbox, which asks for sudo itself). Nothing the minecraft user can write is
ever run as root from here: mods, helpers and web logins all run as minecraft, so a backup copy
they can delete is not a safety net.

  sudo MCSoffbox set <folder>   start copying new backups into <folder> (every hour)
  sudo MCSoffbox status         what is set up and when the last copy worked
  sudo MCSoffbox run            copy now
  sudo MCSoffbox off            stop copying (copies already made are left alone)

What it does, and does not do:
  * copies finished backups (never quick copies, which can be damaged) that are not there yet;
  * NEVER deletes or overwrites anything in <folder> (you decide when to clear old ones);
  * refuses a <folder> the minecraft user could change: it must not be writable by that user,
    nor sit inside a folder that user can write (they could swap it for another);
  * warns, and refuses unless you say --same-disk-ok, if <folder> is on the same disk as the
    servers: one dead disk would take both;
  * stops copying if a folder that was on another disk now looks like the same disk (the
    drive got unplugged and the empty mount point is filling the main disk instead);
  * checks every copy by reading it back before it counts.

Python 3 standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

USER = "minecraft"
SRC = Path("/opt/minecraft/backups/instance")
CONF_DIR = Path("/etc/MCS")
CONF = CONF_DIR / "offbox.json"
STATUS = CONF_DIR / "offbox-status.json"
SHIM = "/usr/local/bin/MCSoffbox"
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*-\d{8}-\d{6}(-prerestore)?\.tar\.gz")
MARGIN = 100 * 2**20  # keep this much free on the destination


def die(msg: str) -> None:
    print(f"MCSoffbox: {msg}", file=sys.stderr)
    sys.exit(1)


def need_root() -> None:
    if os.geteuid() != 0:
        print("This needs sudo; asking for it (Ctrl+C to cancel).", file=sys.stderr)
        os.execvp("sudo", ["sudo", "-H", SHIM, *sys.argv[1:]])


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    gid = pwd.getpwnam(USER).pw_gid
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)  # the web page may read it; only root can write it
    os.replace(tmp, path)


def load_conf() -> dict | None:
    try:
        return json.loads(CONF.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        die(f"cannot read {CONF}: {e}")


def mc_can_write(path: Path) -> bool:
    return subprocess.run(["runuser", "-u", USER, "--", "test", "-w", str(path)], capture_output=True).returncode == 0


def check_destination(dest: Path) -> str | None:
    """None if the folder is safe to use, else why not."""
    uid = pwd.getpwnam(USER).pw_uid
    if not dest.is_absolute():
        return "give the full path, starting with /"
    if not dest.is_dir():
        return f"{dest} is not a folder. If it is a drive, plug it in and mount it first, then give the folder on it"
    child = None
    node = dest
    while True:
        st = node.stat()
        if st.st_uid == uid:
            return f"{node} belongs to the {USER} user, who could change or empty it"
        if mc_can_write(node):
            sticky_ok = bool(st.st_mode & stat.S_ISVTX) and child is not None and child.stat().st_uid == 0
            if not sticky_ok:
                return f"the {USER} user can write to {node}, so could delete or swap the copies"
        if node == node.parent:
            return None
        child, node = node, node.parent


def same_disk(dest: Path) -> bool:
    return os.stat(dest).st_dev == os.stat(SRC if SRC.exists() else "/opt/minecraft").st_dev


def listing() -> list[tuple[str, int]]:
    """(name, size) of the finished backups that could be copied. Links and odd files are ignored."""
    out = []
    try:
        dfd = os.open(SRC, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return out
    try:
        for name in sorted(os.listdir(dfd)):
            if not NAME.fullmatch(name):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((name, st.st_size))
    finally:
        os.close(dfd)
    return out


def copy_one(name: str, dest: Path) -> str:
    """Copy one backup. Returns 'copied', 'skipped' (already there) or raises OSError/ValueError."""
    target = dest / name
    if target.exists():
        return "skipped"
    dfd = os.open(SRC, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{name} is not a plain file")
        if shutil.disk_usage(dest).free < st.st_size + MARGIN:
            raise OSError(f"not enough free space in {dest} for {name}")
        tmp = dest / f".{name}.part"
        tmp.unlink(missing_ok=True)
        h = hashlib.sha256()
        try:
            out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(out_fd, "wb") as out:
                while chunk := src.read(1 << 20):
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            st2 = os.fstat(src.fileno())
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise ValueError(f"{name} changed while it was being copied; will try again")
            back = hashlib.sha256()
            with open(tmp, "rb") as chk:  # read the copy back: a copy that cannot be read is not a copy
                while chunk := chk.read(1 << 20):
                    back.update(chunk)
            if back.digest() != h.digest() or tmp.stat().st_size != st.st_size:
                raise OSError(f"the copy of {name} does not match; the drive may be failing")
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return "copied"


def run() -> int:
    conf = load_conf()
    if not conf:
        print("Nothing is set up. Use: sudo MCSoffbox set <folder>")
        return 0
    dest = Path(conf["dest"])
    st: dict = {"dest": str(dest), "last_run": int(time.time()), "version": 1}
    old = {}
    try:
        old = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        pass
    st["last_ok"] = old.get("last_ok", 0)
    st["copied_total"] = old.get("copied_total", 0)
    problems: list[str] = []
    copied = 0
    try:
        if not dest.is_dir():
            raise OSError(f"{dest} is not there (is the drive plugged in?)")
        if conf.get("separate") and same_disk(dest):
            raise OSError(f"{dest} used to be on another disk but now looks like the main disk: the drive is probably "
                          "unplugged, so copying was paused (the main disk would fill up instead). Plug it in again")
        st["same_disk"] = same_disk(dest)
        for name, _size in listing():
            try:
                if copy_one(name, dest) == "copied":
                    copied += 1
                    print(f"copied {name}")
            except (OSError, ValueError) as e:
                problems.append(str(e))
                print(f"problem: {e}", file=sys.stderr)
        have = {n for n, _ in listing() if (dest / n).exists()}
        sources = listing()
        st["newest_source"] = sources[-1][0] if sources else ""
        st["copied_files"] = len(have)
        st["missing"] = len(sources) - len(have)
        st["dest_free"] = shutil.disk_usage(dest).free
        st["newest_size"] = max((s for _, s in sources), default=0)
    except OSError as e:
        problems.append(str(e))
        print(f"problem: {e}", file=sys.stderr)
    st["copied_total"] += copied
    st["copied_now"] = copied
    st["error"] = "; ".join(problems[:3])
    if not problems:
        st["last_ok"] = int(time.time())
    _write_json(STATUS, st)
    return 1 if problems else 0


def cmd_set(args: list[str]) -> int:
    need_root()
    ok_same = "--same-disk-ok" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        die("usage: sudo MCSoffbox set <folder> [--same-disk-ok]")
    dest = Path(os.path.realpath(paths[0]))
    why = check_destination(dest)
    if why:
        die(why)
    sd = same_disk(dest)
    if sd and not ok_same:
        die(f"{dest} is on the SAME disk as the servers, so one dead disk would take the backups too.\n"
            "Use a second drive or another machine. If this is only a trial, add --same-disk-ok.")
    try:
        probe = dest / ".mcs-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        die(f"cannot write to {dest}: {e}")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(CONF, {"dest": str(dest), "separate": not sd, "set": int(time.time())})
    if sd:
        print("WARNING: same disk as the servers. This protects against deletes and mistakes, not against the disk dying.")
    subprocess.run(["systemctl", "enable", "--now", "MCSoffbox.timer"], capture_output=True)
    print(f"Copying new backups to {dest} every hour. First copy now:")
    return run()


def cmd_off() -> int:
    need_root()
    CONF.unlink(missing_ok=True)
    STATUS.unlink(missing_ok=True)
    subprocess.run(["systemctl", "disable", "--now", "MCSoffbox.timer"], capture_output=True)
    print("Stopped. Copies already made are left where they are.")
    return 0


def cmd_status() -> int:
    conf = load_conf()
    if not conf:
        print("No separate copy is set up. Use: sudo MCSoffbox set <folder>")
        return 0
    try:
        st = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        st = {}
    ago = lambda t: "never" if not t else f"{int((time.time() - t) // 60)} minutes ago"  # noqa: E731
    print(f"copying to:   {conf['dest']}")
    print(f"last success: {ago(st.get('last_ok'))}")
    print(f"copied:       {st.get('copied_files', 0)} backups there, {st.get('missing', 0)} still to copy")
    if st.get("same_disk"):
        print("WARNING: same disk as the servers")
    if st.get("error"):
        print(f"PROBLEM: {st['error']}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "help"
    if cmd == "set":
        return cmd_set(argv[1:])
    if cmd == "off":
        return cmd_off()
    if cmd == "run":
        need_root()
        return run()
    if cmd == "status":
        if os.geteuid() != 0 and not os.access(CONF, os.R_OK):
            need_root()
        return cmd_status()
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
