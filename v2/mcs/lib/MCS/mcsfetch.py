#!/usr/bin/env python3
"""MCSfetch - download server jars and mods/plugins for MCS instances.

Python 3 standard library only (nothing to pip install). Used by the MCSfetch
command line tool, and importable by the web front end.

  versions  list Minecraft versions a server type supports
  jar       download a server jar into an instance, point service.conf at it
            and pick the Java version that Minecraft version needs
  search    search Modrinth / CurseForge
  add       install mods (or Paper plugins) plus their required dependencies
  list      show what MCSfetch installed in an instance
  update    check for newer versions of installed mods (--apply to install)
  remove    remove a mod (--prune also removes orphaned dependencies)

Safety: HTTPS only, downloads are limited to a short list of known hosts
(redirects included), every file is checked against the hash the API published
before it replaces anything, file names are sanitised, and writes are atomic.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

VERSION = "2.0"
# Sent with every request so the services can tell who is calling and where to complain.
UA = f"Magrathea/{VERSION} (https://github.com/snkiz/Magrathea)"

LIB_DIR = Path(__file__).resolve().parent  # the program (root-owned): templates, web files
# The data folder (servers, backups, config) is separate from the program. MCS_HOME overrides it.
HOME = Path(os.environ.get("MCS_HOME") or "/opt/minecraft")
INSTANCES = HOME / "instance"

ALLOWED_HOSTS = {
    "piston-meta.mojang.com", "piston-data.mojang.com",
    "fill.papermc.io", "fill-data.papermc.io",
    "meta.fabricmc.net", "maven.fabricmc.net",
    "api.modrinth.com", "cdn.modrinth.com",
    "api.curseforge.com", "edge.forgecdn.net", "mediafilez.forgecdn.net",
    "api.mojang.com",
}
MAX_DOWNLOAD = 2 << 30  # 2 GiB

# Every API MCS asks questions of. All requests go through api_get(), so the rules for talking to
# each one live here. "cache" is how many seconds an answer may be reused; 0 means ask every time.
PLATFORMS = {
    "mojang": {"base": "https://piston-meta.mojang.com", "cache": 0},
    "mojang-profile": {"base": "https://api.mojang.com", "cache": 0},
    "paper": {"base": "https://fill.papermc.io/v3/projects/paper", "cache": 300},
    "fabric": {"base": "https://meta.fabricmc.net/v2/versions", "cache": 0},
    "modrinth": {"base": "https://api.modrinth.com/v2", "cache": 0},
    # CurseForge's API terms (3.1e) forbid saving or caching anything from it. Leave this at 0.
    "curseforge": {"base": "https://api.curseforge.com/v1", "cache": 0},
}
MOJANG_MANIFEST = "/mc/game/version_manifest_v2.json"
MAX_WAIT = 60  # longest pause, in seconds, when a service says to slow down
BROKER_SOCKET = "/run/MCScred.sock"  # CurseForge requests are made by the key broker, mcscred.py
CF_GAME_MINECRAFT = 432
CF_CLASS_MODS = 6
CF_LOADERS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
# Modrinth loader tags to accept for each instance loader.
MR_LOADERS = {
    "fabric": ["fabric"], "quilt": ["quilt", "fabric"],
    "forge": ["forge"], "neoforge": ["neoforge"],
    "paper": ["paper", "spigot", "bukkit"],
}
JAR_TYPES = ("vanilla", "paper", "fabric")
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


class McsError(Exception):
    pass


class Manual(McsError):
    """A file the author does not allow third-party tools to download."""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def check_url(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise McsError(f"refusing to contact {u.hostname or url!r} (not on the allowed host list)")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # an API key is only for the host it was meant for: do not carry it to another one
        if new and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            new.remove_header("X-api-key")
        return new


_opener = urllib.request.build_opener(_SafeRedirect)
_cache: dict[str, tuple[float, bytes]] = {}  # in memory only: gone when the program stops
_wait_until: dict[str, float] = {}


def broker_ask(request: dict) -> dict:
    """Send one request to the key broker and return its reply."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(240)  # the broker may be waiting because CurseForge said to slow down
            s.connect(BROKER_SOCKET)
            # "claimed" is only a hint for the log; the broker finds out who is calling by itself
            s.sendall(json.dumps({**request, "claimed": Path(sys.argv[0]).name}).encode() + b"\n")
            with s.makefile("rb") as f:
                reply = json.loads(f.read())
    except (AttributeError, OSError, ValueError) as e:
        raise McsError(f"CurseForge is not available: the MCS key broker did not answer ({e})") from None
    if not reply.get("ok"):
        raise McsError(reply.get("error") or "the MCS key broker refused the request")
    return reply


def curseforge_available() -> bool:
    try:
        return bool(broker_ask({"status": True}).get("available"))
    except McsError:
        return False


def _seconds(value, default: float) -> float:
    """A wait time from a response header, kept between 0 and MAX_WAIT."""
    try:
        return min(max(float(value), 0), MAX_WAIT)
    except (TypeError, ValueError):
        return default


def _fetch(platform: str, url: str, headers: dict) -> bytes:
    last = None
    for attempt in range(3):
        pause = _wait_until.get(platform, 0) - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
        try:
            with _opener.open(req, timeout=30) as r:
                if r.headers.get("X-Ratelimit-Remaining") == "0":  # used up: hold the next request
                    _wait_until[platform] = time.monotonic() + _seconds(r.headers.get("X-Ratelimit-Reset"), 1)
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                told = e.headers.get("Retry-After") or e.headers.get("X-Ratelimit-Reset")
                time.sleep(_seconds(told, 2 * (attempt + 1)))
                continue
            raise McsError(f"HTTP {e.code} from {url.split('?')[0]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(1)
    raise McsError(f"cannot reach {url.split('?')[0]}: {last}")


def api_get(platform: str, route: str = "", params: dict | None = None):
    """The one way MCS asks an API something. Callers name the platform and the path under it,
    never a whole address, so a key can only be sent to the service it belongs to.
    CurseForge needs a key, so those requests are handed to the key broker."""
    rules = PLATFORMS[platform]
    if route and not route.startswith("/"):
        raise McsError(f"bad API route {route!r}")
    if platform == "curseforge":  # this program never holds the key, and nothing is cached
        return json.loads(broker_ask({"route": route, "params": params or {}})["body"])
    url = rules["base"] + route
    if params:
        url += "?" + urllib.parse.urlencode(params)
    check_url(url)
    hit = _cache.get(url)
    if rules["cache"] and hit and time.monotonic() - hit[0] < rules["cache"]:
        return json.loads(hit[1])
    body = _fetch(platform, url, {})
    data = json.loads(body)
    if rules["cache"]:
        _cache[url] = (time.monotonic(), body)
    return data


def safe_name(name: str) -> str:
    name = os.path.basename(name)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._+()\[\]' -]*\.jar", name):
        raise McsError(f"unsafe file name {name!r}")
    return name


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def download(url: str, dest_dir: Path, filename: str, hashes: dict) -> str:
    """Download to dest_dir/filename, verifying `hashes` ({algo: hex}).
    Returns the sha256. The old file is only replaced after the hash checks."""
    check_url(url)
    name = safe_name(filename)
    if not dest_dir.is_dir():
        dest_dir.mkdir(parents=True)
        os.chmod(dest_dir, 0o2770)  # group-writable + setgid, like the rest of an instance
    digests = {a: hashlib.new(a) for a in set(hashes) | {"sha256"}}
    fd, tmp = tempfile.mkstemp(prefix=".mcs-", suffix=".part", dir=dest_dir)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with os.fdopen(fd, "wb") as out, _opener.open(req, timeout=60) as r:
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise McsError(f"{name}: download is larger than {MAX_DOWNLOAD >> 20} MiB, aborting")
                for d in digests.values():
                    d.update(chunk)
                out.write(chunk)
        for algo, want in hashes.items():
            if digests[algo].hexdigest() != want.lower():
                raise McsError(f"{name}: {algo} checksum mismatch, file discarded")
        os.chmod(tmp, 0o660)
        os.replace(tmp, dest_dir / name)
    except urllib.error.HTTPError as e:
        _discard(tmp)
        raise McsError(f"{name}: download failed, HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        _discard(tmp)
        raise McsError(f"{name}: download failed: {e}") from None
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return digests["sha256"].hexdigest()


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".mcs-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(tmp, 0o660)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# --------------------------------------------------------------------------
# Instances, service.conf, metadata
# --------------------------------------------------------------------------
def inst_dir(name: str) -> Path:
    if not NAME_RE.match(name):
        raise McsError(f"invalid instance name {name!r}")
    d = INSTANCES / name
    if not d.is_dir():
        raise McsError(f"no instance called {name!r} in {INSTANCES} (create it with MCScreate)")
    return d


def load_meta(d: Path) -> dict:
    p = d / "mcs.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            raise McsError(f"{p} is not valid JSON") from None
    return {}


def save_meta(d: Path, meta: dict) -> None:
    write_atomic(d / "mcs.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")


def set_conf(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a systemd EnvironmentFile, replacing the first
    uncommented KEY= line or appending one."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    write_atomic(path, "\n".join(lines) + "\n")


def ensure_conf(d: Path) -> Path:
    conf = d / "service.conf"
    if not conf.exists():
        write_atomic(conf, (LIB_DIR / "service.conf").read_text(encoding="utf-8"))
    return conf


def find_java(major: int) -> str | None:
    for pat in (f"/usr/lib/jvm/java-{major}-openjdk-*", f"/usr/lib/jvm/jdk-{major}*",
                f"/usr/lib/jvm/temurin-{major}*"):
        for p in sorted(glob.glob(pat)):
            if os.access(os.path.join(p, "bin", "java"), os.X_OK):
                return p
    return None


# --------------------------------------------------------------------------
# Server jars
# --------------------------------------------------------------------------
def mojang_manifest() -> dict:
    return api_get("mojang", MOJANG_MANIFEST)


def mojang_entry(url: str) -> dict:
    """The manifest lists a full address for each version; accept only ones on the same host."""
    base = PLATFORMS["mojang"]["base"]
    if not url.startswith(base + "/"):
        raise McsError(f"unexpected address in Mojang's version list: {url!r}")
    return api_get("mojang", url[len(base):])


def mojang_java(mc: str) -> int | None:
    entry = next((v for v in mojang_manifest()["versions"] if v["id"] == mc), None)
    if not entry:
        return None
    return (mojang_entry(entry["url"]).get("javaVersion") or {}).get("majorVersion")


def is_release(v: str) -> bool:
    return re.fullmatch(r"\d+(\.\d+)*", v) is not None


def paper_versions() -> list[str]:
    out: list[str] = []
    for group in api_get("paper")["versions"].values():
        out.extend(group)
    return out


def list_versions(kind: str) -> list[str]:
    if kind == "vanilla":
        return [v["id"] for v in mojang_manifest()["versions"] if v["type"] == "release"]
    if kind == "paper":
        return [v for v in paper_versions() if is_release(v)]
    if kind == "fabric":
        return [g["version"] for g in api_get("fabric", "/game") if g["stable"]]
    raise McsError(f"unknown server type {kind!r}")


def resolve_jar(kind: str, mc: str) -> dict:
    """-> {mc, url, file, hashes, java, note}"""
    if mc == "latest":
        vs = list_versions(kind)
        if not vs:
            raise McsError(f"no {kind} versions found")
        mc = vs[0]
    java = mojang_java(mc)
    if kind == "vanilla":
        entry = next((v for v in mojang_manifest()["versions"] if v["id"] == mc), None)
        if not entry:
            raise McsError(f"Mojang has no version {mc!r}")
        srv = mojang_entry(entry["url"])["downloads"].get("server")
        if not srv:
            raise McsError(f"{mc} has no server download")
        return {"mc": mc, "url": srv["url"], "file": f"minecraft_server.{mc}.jar",
                "hashes": {"sha1": srv["sha1"]}, "java": java, "note": ""}
    if kind == "paper":
        if mc not in paper_versions():
            raise McsError(f"Paper has no version {mc!r}")
        b = api_get("paper", f"/versions/{urllib.parse.quote(mc)}/builds/latest")
        dl = b["downloads"]["server:default"]
        note = "" if b.get("channel") == "STABLE" else f"this Paper build is on the {b.get('channel')} channel"
        return {"mc": mc, "url": dl["url"], "file": dl["name"],
                "hashes": {"sha256": dl["checksums"]["sha256"]}, "java": java, "note": note}
    if kind == "fabric":
        loaders = api_get("fabric", f"/loader/{urllib.parse.quote(mc)}")
        if not loaders:
            raise McsError(f"Fabric does not support Minecraft {mc!r}")
        loader = next((x["loader"]["version"] for x in loaders if x["loader"]["stable"]),
                      loaders[0]["loader"]["version"])
        installer = next((x["version"] for x in api_get("fabric", "/installer") if x["stable"]), None)
        if not installer:
            raise McsError("could not find a Fabric installer version")
        url = f"{PLATFORMS['fabric']['base']}/loader/{urllib.parse.quote(mc)}/{loader}/{installer}/server/jar"
        return {"mc": mc, "url": url, "file": f"fabric-server-mc.{mc}-loader.{loader}-launcher.{installer}.jar",
                "hashes": {}, "java": java, "loader_version": loader,
                "note": "Fabric publishes no checksum for this jar; its sha256 is recorded in mcs.json"}
    raise McsError(f"unknown server type {kind!r}")


# --------------------------------------------------------------------------
# server.properties: MCS writes only its own choices, then the server jar fills in the rest
# --------------------------------------------------------------------------
# "--initSettings" makes the jar write its settings and stop without making a world. 1.16.5 is the
# oldest version this was tested on; 1.12.2 ignores the option and starts the whole server.
INIT_SINCE = (1, 16, 5)


def jar_mc_version(jar: Path) -> str | None:
    """The Minecraft version a server jar is for, read from inside the jar."""
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "install.properties" in names:  # Fabric's launcher
                m = re.search(r"^game-version=(\S+)", z.read("install.properties").decode("utf-8", "replace"), re.M)
                return m.group(1) if m else None
            if "version.json" in names:  # Mojang's jar, and Paper's
                return str(json.loads(z.read("version.json")).get("id") or "") or None
    except (OSError, ValueError, KeyError, AttributeError, zipfile.BadZipFile):
        pass
    return None


def settings_blocker(d: Path, jar: str) -> str | None:
    """Why the jar must not be asked to write its settings, or None if it is safe to."""
    if (d / (read_prop(d, "level-name", "world") or "world")).exists():
        return "the world already exists"
    if not jar or jar == "NULL" or not (d / jar).is_file():
        return "no server jar is set"
    mc = jar_mc_version(d / jar)
    if not mc or not is_release(mc):
        return "cannot tell which Minecraft release this jar is"
    if tuple(int(x) for x in mc.split(".")) < INIT_SINCE:
        return f"Minecraft {mc} is too old to write its settings without starting"
    return None


def configured_jar(d: Path) -> str:
    conf = d / "service.conf"
    lines = conf.read_text(encoding="utf-8", errors="replace").splitlines() if conf.is_file() else []
    return next((x[7:].strip().strip("\"'") for x in lines if x.startswith("SERVER=")), "")


def write_settings(name: str) -> bool:
    """Have the server complete server.properties, inside the same sandbox a running server has.
    Failing is harmless: the first start fills the file in anyway."""
    d = inst_dir(name)
    why = settings_blocker(d, configured_jar(d))
    if why:
        say(f"server.properties left as it is: {why}.")
        return False
    say("Asking the server to complete server.properties (no world is made yet)...")
    try:
        ok = subprocess.run(["systemctl", "start", f"MCSinit@{name}.service"], timeout=660).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        warn(f"that did not work (see: journalctl -u MCSinit@{name}). "
             "The first start will fill in server.properties instead.")
    return ok


def cmd_settings(a) -> int:
    if a.check is not None:  # used by MCS-init, inside the sandbox
        why = settings_blocker(inst_dir(a.instance), a.check)
        if why:
            say(f"MCS: not writing settings: {why}.")
        return 3 if why else 0
    write_settings(a.instance)
    return 0


def cmd_versions(a) -> int:
    vs = list_versions(a.type)
    if a.json:
        say(json.dumps(vs))
    else:
        for v in vs[: a.limit]:
            say(v)
    return 0


def cmd_jar(a) -> int:
    d = inst_dir(a.instance)
    r = resolve_jar(a.type, a.mc)
    say(f"{a.type} {r['mc']}: {r['file']}" + (f"  (needs Java {r['java']})" if r["java"] else ""))
    if r["note"]:
        warn(r["note"])
    if a.dry_run:
        say(f"would download {r['url']}")
        return 0
    sha = download(r["url"], d, r["file"], r["hashes"])
    say(f"saved {d / r['file']}  sha256 {sha}")
    conf = ensure_conf(d)
    set_conf(conf, "SERVER", r["file"])
    if r["java"]:
        home = find_java(int(r["java"]))
        if home:
            set_conf(conf, "JAVA_HOME", home)
            say(f"service.conf: SERVER={r['file']}  JAVA_HOME={home}")
        else:
            warn(f"Minecraft {r['mc']} needs Java {r['java']} but it is not installed under /usr/lib/jvm. "
                 f"Install it (Debian: sudo apt install openjdk-{r['java']}-jre-headless) "
                 f"and set JAVA_HOME in service.conf.")
    meta = load_meta(d)
    prev = meta.get("mc")
    meta.update({"type": a.type, "mc": r["mc"], "jar": r["file"], "jar_sha256": sha})
    if a.type == "vanilla":
        meta.pop("loader", None)  # vanilla takes no mods
    else:
        meta["loader"] = a.type  # "paper" or "fabric"
    if prev and prev != r["mc"] and meta.get("mods"):
        warn(f"Minecraft version changed {prev} -> {r['mc']}; run 'MCSfetch update {a.instance}' to move your mods.")
    save_meta(d, meta)
    if INSTANCES == Path("/opt/minecraft/instance") and not settings_blocker(d, r["file"]):
        write_settings(a.instance)
    return 0


# --------------------------------------------------------------------------
# Mods / plugins
# --------------------------------------------------------------------------
def target_dir(d: Path, meta: dict) -> Path:
    return d / ("plugins" if meta.get("type") == "paper" else "mods")


def need_ctx(d: Path, a) -> dict:
    meta = load_meta(d)
    if getattr(a, "mc", None):
        meta["mc"] = a.mc
    if getattr(a, "loader", None):
        meta["loader"] = a.loader
        if "type" not in meta:
            meta["type"] = "paper" if a.loader == "paper" else a.loader
    if not meta.get("mc") or not meta.get("loader"):
        raise McsError("don't know this instance's Minecraft version / loader. "
                       "Run 'MCSfetch jar' first, or pass --mc VERSION --loader fabric|quilt|forge|neoforge|paper")
    if meta["loader"] not in MR_LOADERS:
        raise McsError(f"unsupported loader {meta['loader']!r}")
    return meta


class Resolver:
    """Works out which files to install: the mod plus its required dependencies."""

    def __init__(self, meta: dict, allow_beta: bool, deps: bool = True):
        self.meta, self.allow_beta, self.deps = meta, allow_beta, deps
        self.mc, self.loader = meta["mc"], meta["loader"]
        self.installed = meta.get("mods", {})
        self.plan: dict[str, dict] = {}
        self.manual: list[str] = []
        self._projects: dict[str, dict] = {}

    # -- Modrinth --
    def mr_project(self, ref: str) -> dict:
        if ref not in self._projects:
            self._projects[ref] = api_get("modrinth", f"/project/{urllib.parse.quote(ref)}")
        return self._projects[ref]

    def mr_version(self, project: str) -> dict:
        vs = api_get("modrinth", f"/project/{urllib.parse.quote(project)}/version", {
            "loaders": json.dumps(MR_LOADERS[self.loader]), "game_versions": json.dumps([self.mc])})
        vs.sort(key=lambda v: v["date_published"], reverse=True)
        pick = [v for v in vs if v["version_type"] == "release"]
        if not pick and self.allow_beta:
            pick = vs
        if not pick:
            hint = " (only beta/alpha builds exist; use --allow-beta)" if vs else ""
            raise McsError(f"{project}: no {self.loader} build for Minecraft {self.mc}{hint}")
        return pick[0]

    def add_modrinth(self, ref: str, by: str | None, version_id: str | None = None, force: bool = False) -> None:
        proj = self.mr_project(ref)
        key = f"modrinth:{proj['id']}"
        if key in self.plan or (key in self.installed and not force):
            if key in self.plan and by:
                self.plan[key]["required_by"].append(by)
            return
        if proj.get("server_side") == "unsupported":
            raise McsError(f"{proj['title']} is client-only and does nothing on a server")
        v = api_get("modrinth", f"/version/{version_id}") if version_id else self.mr_version(proj["id"])
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        hashes = {k: f["hashes"][k] for k in ("sha512", "sha1") if k in f["hashes"]}
        self.plan[key] = {"source": "modrinth", "project": proj["id"], "slug": proj["slug"],
                          "title": proj["title"], "version_id": v["id"], "version": v["version_number"],
                          "file": f["filename"], "url": f["url"], "hashes": hashes,
                          "explicit": by is None, "required_by": [by] if by else []}
        if self.deps:
            for dep in v.get("dependencies", []):
                pid, vid = dep.get("project_id"), dep.get("version_id")
                if not pid and vid:
                    pid = api_get("modrinth", f"/version/{vid}")["project_id"]
                if not pid:
                    continue
                if dep["dependency_type"] == "required":
                    self.add_modrinth(pid, key, vid)
                elif dep["dependency_type"] == "incompatible" and f"modrinth:{pid}" in self.installed:
                    warn(f"{proj['title']} is incompatible with installed {self.installed[f'modrinth:{pid}']['title']}")

    # -- CurseForge --
    def cf_mod(self, ref: str) -> dict:
        if ref not in self._projects:
            if ref.isdigit():
                self._projects[ref] = api_get("curseforge", f"/mods/{ref}")["data"]
            else:
                hits = api_get("curseforge", "/mods/search", {"gameId": CF_GAME_MINECRAFT, "classId": CF_CLASS_MODS, "slug": ref})["data"]
                if not hits:
                    raise McsError(f"CurseForge has no mod with slug {ref!r}")
                self._projects[ref] = hits[0]
        return self._projects[ref]

    def add_curseforge(self, ref: str, by: str | None, force: bool = False) -> None:
        if self.loader == "paper":
            raise McsError("CurseForge mods are not Paper plugins; use --source modrinth for plugins")
        mod = self.cf_mod(ref)
        key = f"curseforge:{mod['id']}"
        if key in self.plan or (key in self.installed and not force):
            if key in self.plan and by:
                self.plan[key]["required_by"].append(by)
            return
        files = api_get("curseforge", f"/mods/{mod['id']}/files", {
            "gameVersion": self.mc, "modLoaderType": CF_LOADERS[self.loader], "pageSize": 50})["data"]
        files.sort(key=lambda x: x["fileDate"], reverse=True)
        pick = [x for x in files if x["releaseType"] == 1] or (files if self.allow_beta else [])
        if not pick:
            raise McsError(f"{mod['name']}: no {self.loader} build for Minecraft {self.mc} on CurseForge")
        f = pick[0]
        if not f.get("downloadUrl"):
            site = (mod.get("links") or {}).get("websiteUrl", "")
            self.manual.append(f"{mod['name']}: the author does not allow downloads through third-party tools. "
                               f"Download {f['fileName']} yourself from {site} and put it in the mods folder.")
            return
        hashes = {"sha1": h["value"] for h in f.get("hashes", []) if h.get("algo") == 1}
        self.plan[key] = {"source": "curseforge", "project": str(mod["id"]), "slug": mod.get("slug", ""),
                          "title": mod["name"], "version_id": str(f["id"]), "version": f["displayName"],
                          "file": f["fileName"], "url": f["downloadUrl"], "hashes": hashes,
                          "explicit": by is None, "required_by": [by] if by else []}
        if self.deps:
            for dep in f.get("dependencies", []):
                if dep.get("relationType") == 3:
                    self.add_curseforge(str(dep["modId"]), key)

    def add(self, spec: str, default_source: str, force: bool = False) -> None:
        src, _, ref = spec.partition(":")
        if not ref:
            src, ref = default_source, spec
        if src == "modrinth":
            self.add_modrinth(ref, None, force=force)
        elif src == "curseforge":
            self.add_curseforge(ref, None, force=force)
        else:
            raise McsError(f"unknown source {src!r} (use modrinth or curseforge)")


def apply_plan(d: Path, meta: dict, rs: Resolver, dry: bool, only_changed: bool = False) -> int:
    dest = target_dir(d, meta)
    mods = meta.setdefault("mods", {})
    todo = {k: e for k, e in rs.plan.items()
            if not (only_changed and k in mods and mods[k]["version_id"] == e["version_id"])}
    for k, e in todo.items():
        old = mods.get(k)
        tag = "update" if old else "install"
        why = f"  (needed by {', '.join(rs.plan[r]['title'] for r in e['required_by'] if r in rs.plan)})" if e["required_by"] else ""
        say(f"{tag}: {e['title']} {e['version']}  -> {e['file']}{why}")
    for m in rs.manual:
        warn(m)
    if dry or not todo:
        return 2 if rs.manual else 0
    for k, e in todo.items():
        sha = download(e["url"], dest, e["file"], e["hashes"])
        old = mods.get(k)
        if old and old["file"] != e["file"]:
            try:
                (dest / safe_name(old["file"])).unlink()
            except (FileNotFoundError, McsError):
                pass
        mods[k] = {x: e[x] for x in ("source", "project", "slug", "title", "version_id", "version", "file")}
        mods[k]["sha256"] = sha
        mods[k]["explicit"] = e["explicit"] or bool(old and old.get("explicit"))
        mods[k]["required_by"] = sorted(set(e["required_by"]) | set((old or {}).get("required_by", [])))
        save_meta(d, meta)  # after each file, so an interrupted run stays consistent
    say(f"done: {len(todo)} file(s) in {dest}")
    return 2 if rs.manual else 0


def cmd_add(a) -> int:
    d = inst_dir(a.instance)
    meta = need_ctx(d, a)
    rs = Resolver(meta, a.allow_beta, not a.no_deps)
    errors = 0
    for spec in a.mods:
        try:
            rs.add(spec, a.source)
        except McsError as e:
            warn(str(e))
            errors += 1
    rc = apply_plan(d, meta, rs, a.dry_run)
    return 1 if errors else rc


def cmd_update(a) -> int:
    d = inst_dir(a.instance)
    meta = need_ctx(d, a)
    mods = meta.get("mods", {})
    if not mods:
        say("nothing tracked yet; install mods with 'MCSfetch add'.")
        return 0
    rs = Resolver(meta, a.allow_beta)
    for key, m in mods.items():
        try:
            rs.add(f"{m['source']}:{m['project']}", m["source"], force=True)
        except McsError as e:
            warn(str(e))
    for k, e in rs.plan.items():  # keep dependencies marked as dependencies
        if k in mods:
            e["explicit"] = bool(mods[k].get("explicit"))
    changed = [k for k, e in rs.plan.items() if k not in mods or mods[k]["version_id"] != e["version_id"]]
    if not changed:
        say("everything is up to date.")
        return 0
    rc = apply_plan(d, meta, rs, dry=not a.apply, only_changed=True)
    if not a.apply:
        say("\n(dry run: add --apply to install)")
    return rc


def cmd_list(a) -> int:
    d = inst_dir(a.instance)
    meta = load_meta(d)
    mods = meta.get("mods", {})
    if a.json:
        say(json.dumps({k: v for k, v in meta.items()}, indent=2))
        return 0
    say(f"{a.instance}: {meta.get('type', '?')} {meta.get('mc', '?')}  loader={meta.get('loader', '-')}")
    for k, m in sorted(mods.items(), key=lambda kv: kv[1]["title"].lower()):
        say(f"  {m['title']:<32} {m['version']:<24} {'' if m.get('explicit') else '(dependency) '}[{k}]")
    if not mods:
        say("  no mods tracked by MCSfetch")
    return 0


def cmd_remove(a) -> int:
    d = inst_dir(a.instance)
    meta = load_meta(d)
    mods = meta.get("mods", {})
    dest = target_dir(d, meta)
    q = a.mod.lower()
    key = next((k for k, m in mods.items() if q in (k.lower(), m["slug"].lower(), m["title"].lower())), None)
    if not key:
        raise McsError(f"{a.mod!r} is not tracked; see 'MCSfetch list {a.instance}'")
    # required_by lists the mods that need this one.
    dependents = [mods[r]["title"] for r in mods[key].get("required_by", []) if r in mods]
    if dependents and not a.force:
        raise McsError(f"{mods[key]['title']} is required by: {', '.join(dependents)} (use --force)")
    removed = [key]
    if a.prune:
        while True:
            orphans = [k for k, m in mods.items() if k not in removed and not m.get("explicit")
                       and all(r in removed for r in m.get("required_by", []))]
            if not orphans:
                break
            removed += orphans
    for k in removed:
        m = mods.pop(k)
        say(f"remove: {m['title']} ({m['file']})")
        if not a.dry_run:
            try:
                (dest / safe_name(m["file"])).unlink()
            except FileNotFoundError:
                pass
    for m in mods.values():
        m["required_by"] = [r for r in m.get("required_by", []) if r not in removed]
    if not a.dry_run:
        save_meta(d, meta)
    return 0


def search_rows(query: str, mc: str | None, loader: str | None, source: str = "modrinth",
                limit: int = 10) -> list[dict]:
    """Search Modrinth and/or CurseForge. Each row: source, id, title, downloads,
    description, icon (image URL or ""), url (the project's page, for linking out)."""
    rows: list[dict] = []
    if source in ("modrinth", "both"):
        facets = [["project_type:plugin" if loader == "paper" else "project_type:mod"],
                  ["server_side:required", "server_side:optional"]]
        if mc:
            facets.append([f"versions:{mc}"])
        if loader:
            facets.append([f"categories:{x}" for x in MR_LOADERS.get(loader, [loader])])
        r = api_get("modrinth", "/search", {"query": query, "limit": limit, "facets": json.dumps(facets)})
        rows += [{"source": "modrinth", "id": h["slug"], "title": h["title"], "downloads": h["downloads"],
                  "description": h["description"], "icon": h.get("icon_url") or "",
                  "url": f"https://modrinth.com/project/{h['slug']}"} for h in r["hits"]]
    if source in ("curseforge", "both"):
        p = {"gameId": CF_GAME_MINECRAFT, "classId": CF_CLASS_MODS, "searchFilter": query,
             "sortField": 2, "sortOrder": "desc", "pageSize": limit}
        if mc:
            p["gameVersion"] = mc
        if loader in CF_LOADERS:
            p["modLoaderType"] = CF_LOADERS[loader]
        try:
            rows += [{"source": "curseforge", "id": str(m["id"]), "title": m["name"],
                      "downloads": m["downloadCount"], "description": m["summary"],
                      "icon": (m.get("logo") or {}).get("thumbnailUrl") or "",
                      "url": (m.get("links") or {}).get("websiteUrl") or ""}
                     for m in api_get("curseforge", "/mods/search", p)["data"]]
        except McsError as e:
            if source == "curseforge":
                raise
            warn(f"skipping CurseForge: {e}")
    return rows


def cmd_search(a) -> int:
    meta = load_meta(inst_dir(a.instance)) if a.instance else {}
    rows = search_rows(a.query, a.mc or meta.get("mc"), a.loader or meta.get("loader"), a.source, a.limit)
    if a.json:
        say(json.dumps(rows))
        return 0
    for r in rows:
        say(f"{r['source']}:{r['id']}  -  {r['title']}  ({r['downloads']:,} downloads)")
        say(f"    {r['description'][:110]}")
    if not rows:
        say("no results")
    return 0


# --------------------------------------------------------------------------
# Players (whitelist / ops) and game ports
# --------------------------------------------------------------------------
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def read_prop(d: Path, key: str, default: str = "") -> str:
    p = d / "server.properties"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return default


def _dashed(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def offline_uuid(name: str) -> str:
    """The UUID a server with online-mode=false gives a player (name based)."""
    b = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return _dashed(b.hex())


def lookup_player(name: str, online: bool) -> tuple[str, str]:
    """-> (uuid, name with the exact capitalisation Mojang has)."""
    if not PLAYER_RE.match(name or ""):
        raise McsError("a Minecraft name is 3-16 letters, numbers or underscores")
    if not online:
        return offline_uuid(name), name
    try:
        r = api_get("mojang-profile", f"/users/profiles/minecraft/{urllib.parse.quote(name)}")
    except ValueError:  # empty body: Mojang's "no such player"
        raise McsError(f"Mojang has no player called {name!r}. Check the spelling.") from None
    except McsError as e:
        if "HTTP 404" in str(e) or "HTTP 204" in str(e):
            raise McsError(f"Mojang has no player called {name!r}. Check the spelling.") from None
        raise
    if not isinstance(r, dict) or not re.fullmatch(r"[0-9a-fA-F]{32}", str(r.get("id", ""))):
        raise McsError(f"Mojang has no player called {name!r}. Check the spelling.")
    return _dashed(r["id"].lower()), str(r.get("name") or name)


def _load_list(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except ValueError:
        raise McsError(f"{path.name} is not valid JSON; fix or delete it first") from None
    return data if isinstance(data, list) else []


def add_player(d: Path, name: str, op: bool = False) -> dict:
    """Whitelist a player (and optionally make them an operator). Works with the
    server stopped; a running server needs 'whitelist reload' / a restart."""
    online = read_prop(d, "online-mode", "true").lower() != "false"
    uuid, exact = lookup_player(name, online)
    wl = _load_list(d / "whitelist.json")
    if not any(e.get("uuid") == uuid for e in wl):
        wl.append({"uuid": uuid, "name": exact})
        write_atomic(d / "whitelist.json", json.dumps(wl, indent=2) + "\n")
    if op:
        ops = _load_list(d / "ops.json")
        if not any(e.get("uuid") == uuid for e in ops):
            ops.append({"uuid": uuid, "name": exact, "level": 4, "bypassesPlayerLimit": False})
            write_atomic(d / "ops.json", json.dumps(ops, indent=2) + "\n")
    for f in ("whitelist.json", "ops.json"):
        if (d / f).exists():
            os.chmod(d / f, 0o660)
    return {"name": exact, "uuid": uuid, "op": op, "online": online}


def used_ports(skip: str | None = None) -> dict[int, str]:
    out: dict[int, str] = {}
    if INSTANCES.is_dir():
        for d in sorted(INSTANCES.iterdir()):
            # NAME_RE keeps out the hidden restore-staging folder, which is not a server
            if d.is_dir() and NAME_RE.match(d.name) and d.name != skip:
                v = read_prop(d, "server-port", "25565")
                if v.isdigit():
                    out[int(v)] = d.name
    return out


def suggest_port(skip: str | None = None) -> int:
    used = used_ports(skip)  # skip: a server being created already has a folder with the template's port
    p = 25565
    while p in used:
        p += 1
    return p


def check_port(port: int, skip: str | None = None) -> int:
    if not 1024 <= port <= 65535:
        raise McsError("the game port must be between 1024 and 65535")
    other = used_ports(skip).get(port)
    if other:
        raise McsError(f"port {port} is already used by {other!r}; every server needs its own port")
    return port


def online_mode_warning() -> str:
    return ("online-mode=false: the server does not check players against Mojang. Anyone can join "
            "as any name, including an operator's. Only use it behind a trusted proxy or on purpose.")


def cmd_player(a) -> int:
    d = inst_dir(a.instance)
    r = add_player(d, a.name, op=a.op)
    say(f"whitelisted {r['name']} ({r['uuid']})" + (" and made them an operator" if r["op"] else ""))
    if not r["online"]:
        warn(online_mode_warning())
    say("If the server is running, apply it with:  whitelist reload  (console) or restart it.")
    return 0


def cmd_port(a) -> int:
    say(str(suggest_port(a.skip)))
    return 0


def cmd_setup(a) -> int:
    d = inst_dir(a.instance)
    port = check_port(a.port, skip=a.instance)
    set_prop(d, "server-port", str(port))
    say(f"{a.instance} will use game port {port}")
    return 0


def set_prop(d: Path, key: str, value: str) -> None:
    p = d / "server.properties"
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    write_atomic(p, "\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="MCSfetch", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("versions", help="list Minecraft versions for a server type")
    s.add_argument("type", choices=JAR_TYPES)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("jar", help="download a server jar into an instance")
    s.add_argument("instance")
    s.add_argument("--type", choices=JAR_TYPES, required=True)
    s.add_argument("--mc", default="latest", help="Minecraft version (default: latest release)")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(fn=cmd_jar)

    def ctx_args(sp, instance_optional=False):
        sp.add_argument("instance", nargs="?" if instance_optional else None)
        sp.add_argument("--mc", help="Minecraft version (default: the instance's)")
        sp.add_argument("--loader", choices=sorted(MR_LOADERS), help="mod loader (default: the instance's)")

    s = sub.add_parser("search", help="search Modrinth / CurseForge")
    s.add_argument("query")
    s.add_argument("--instance", help="filter by this instance's version and loader")
    s.add_argument("--mc")
    s.add_argument("--loader", choices=sorted(MR_LOADERS))
    s.add_argument("--source", choices=("modrinth", "curseforge", "both"), default="modrinth")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("add", help="install mods/plugins and their required dependencies")
    ctx_args(s)
    s.add_argument("mods", nargs="+", help="[modrinth:|curseforge:]slug-or-id")
    s.add_argument("--source", choices=("modrinth", "curseforge"), default="modrinth")
    s.add_argument("--no-deps", action="store_true")
    s.add_argument("--allow-beta", action="store_true")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("update", help="check for newer versions of installed mods")
    ctx_args(s)
    s.add_argument("--apply", action="store_true", help="install the updates (default: just show them)")
    s.add_argument("--allow-beta", action="store_true")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("list", help="show tracked mods")
    s.add_argument("instance")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("remove", help="remove a tracked mod")
    s.add_argument("instance")
    s.add_argument("mod", help="slug, title or modrinth:/curseforge: key")
    s.add_argument("--prune", action="store_true", help="also remove dependencies nothing else needs")
    s.add_argument("--force", action="store_true", help="remove even if other mods require it")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("player", help="whitelist a player (and optionally make them an operator)")
    s.add_argument("instance")
    s.add_argument("name", help="Minecraft player name")
    s.add_argument("--op", action="store_true", help="also make them an operator")
    s.set_defaults(fn=cmd_player)

    s = sub.add_parser("port", help="print the next free game port")
    s.add_argument("--skip", help="ignore this instance (the one being created)")
    s.set_defaults(fn=cmd_port)

    s = sub.add_parser("setup", help="set an instance's game port")
    s.add_argument("instance")
    s.add_argument("--port", type=int, required=True)
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("settings", help="have the server jar complete server.properties (before the world exists)")
    s.add_argument("instance")
    s.add_argument("--check", metavar="JAR", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_settings)

    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except McsError as e:
        print(f"MCSfetch: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
