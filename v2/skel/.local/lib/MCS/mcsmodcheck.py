#!/usr/bin/env python3
"""mcsmodcheck - find out BEFORE a server starts that its mods cannot work together.

Reads what each jar says about itself (fabric.mod.json for Fabric mods, plugin.yml for Paper plugins)
and compares it with what is installed, the Minecraft version and the Java the server uses. It finds
the same problems the loader would complain about at startup, minutes earlier and in plain words:

  * a mod needs another mod that is not installed (for example Fabric API),
  * a mod needs a newer or older version of something than is installed,
  * a mod needs a newer Java, or a different Minecraft version, than this server has,
  * two mods say they cannot be used together,
  * a Forge or NeoForge mod sitting on a Fabric server.

It does not run any of the code in the jars; it only reads their metadata files. It works the same for
mods MCS installed and mods uploaded by hand. Not finding a problem does not promise the server will
start (mods can also clash in ways they do not declare), and it never blocks anything: MCS shows the
findings as warnings. Python 3 standard library only.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

MAX_JSON = 1 << 20
MAX_NESTED = 80
BUILTIN = {"minecraft", "java", "fabricloader", "fabric-loader"}
# Dependency ids people actually hit, with what to search for (id -> (name, search words)).
KNOWN = {
    "fabric": ("Fabric API", "Fabric API"), "fabric-api": ("Fabric API", "Fabric API"),
    "fabric-language-kotlin": ("Fabric Language Kotlin", "Fabric Language Kotlin"),
    "cloth-config": ("Cloth Config API", "Cloth Config"), "cloth-config2": ("Cloth Config API", "Cloth Config"),
    "architectury": ("Architectury API", "Architectury API"), "owo": ("oωo", "owo-lib"),
    "midnightlib": ("MidnightLib", "MidnightLib"), "geckolib": ("GeckoLib", "GeckoLib"),
}
_cache: dict[tuple, dict | None] = {}


# --------------------------------------------------------------------------
# Versions and version ranges (the small part of Fabric's syntax mods actually use)
# --------------------------------------------------------------------------
def parse_version(v: str):
    """-> (numbers, is_release) or None if it is not a number-like version."""
    v = str(v).strip().lstrip("v").split("+", 1)[0]
    core, _, pre = v.partition("-")
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return [int(p) for p in parts], not pre


def _cmp(a, b) -> int:
    (na, ra), (nb, rb) = a, b
    n = max(len(na), len(nb))
    na, nb = na + [0] * (n - len(na)), nb + [0] * (n - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    return (ra > rb) - (ra < rb)  # a release is newer than its own pre-release


def _bump(nums: list[int], idx: int) -> list[int]:
    out = nums[: idx + 1]
    out[idx] += 1
    return out


def _one(version, comp: str) -> bool:
    comp = comp.strip()
    if comp in ("", "*"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$", comp)
    op, rest = m.group(1) or "=", m.group(2).strip()
    if re.search(r"(^|\.)[xX*]($|\.)", rest):  # 1.21.x
        pre = [int(p) for p in re.split(r"\.", rest) if p.isdigit()]
        if not pre:
            return True
        return _cmp(version, (pre, False)) >= 0 and _cmp(version, (_bump(pre, len(pre) - 1), False)) < 0
    want = parse_version(rest)
    if want is None:
        return True  # cannot read it: do not guess a problem
    c = _cmp(version, want)
    if op == "=":
        return c == 0 or (version[0] == want[0] and not want[1])  # a pre-release spec matches the release of the same numbers
    if op == ">=":
        return c >= 0
    if op == ">":
        return c > 0
    if op == "<=":
        return c <= 0
    if op == "<":
        return c < 0
    if op == "^":
        return c >= 0 and _cmp(version, (_bump(want[0], 0), False)) < 0
    if op == "~":
        return c >= 0 and _cmp(version, (_bump(want[0], min(1, len(want[0]) - 1)), False)) < 0
    return True


def satisfies(version_str: str, spec) -> bool:
    """Does `version_str` meet `spec` (a range string, or a list of them meaning 'any of')?"""
    version = parse_version(version_str)
    if version is None:
        return True
    specs = spec if isinstance(spec, list) else [spec]
    for sp in specs:
        # "1.21.5 - 1.21.8" style is not used by Fabric; comparators are separated by spaces
        toks = re.findall(r"(?:>=|<=|>|<|=|\^|~)?\s*[^\s]+", str(sp).strip()) if str(sp).strip() else ["*"]
        merged, i = [], 0
        while i < len(toks):  # "> = 1" can never happen, but "<  1.2" can
            merged.append(toks[i].replace(" ", "")); i += 1
        try:
            if all(_one(version, t) for t in merged):
                return True
        except (ValueError, IndexError):
            return True
    return False


# --------------------------------------------------------------------------
# Reading jars
# --------------------------------------------------------------------------
def _json(z: zipfile.ZipFile, name: str) -> dict | None:
    try:
        info = z.getinfo(name)
        if info.file_size > MAX_JSON:
            return None
        d = json.loads(z.read(name).decode("utf-8", "replace"), strict=False)
        return d if isinstance(d, dict) else None
    except (KeyError, ValueError, zipfile.BadZipFile, RuntimeError):
        return None


def _mod_info(d: dict) -> dict:
    deps = d.get("depends") if isinstance(d.get("depends"), dict) else {}
    brk = d.get("breaks") if isinstance(d.get("breaks"), dict) else {}
    prov = d.get("provides") if isinstance(d.get("provides"), list) else []
    return {"id": str(d.get("id", "")), "name": str(d.get("name") or d.get("id", "")), "version": str(d.get("version", "")),
            "depends": deps, "breaks": brk, "provides": [str(p) for p in prov if isinstance(p, (str, int))], "env": d.get("environment", "*")}


def _nested(z: zipfile.ZipFile, d: dict, depth: int, budget: list[int]) -> list[dict]:
    """Mods bundled inside a mod (Fabric API is dozens of them): they count as installed."""
    out = []
    for ent in d.get("jars", []) if isinstance(d.get("jars"), list) else []:
        f = ent.get("file") if isinstance(ent, dict) else None
        if not isinstance(f, str) or budget[0] <= 0 or depth > 2:
            continue
        budget[0] -= 1
        try:
            if z.getinfo(f).file_size > 64 << 20:
                continue
            inner = zipfile.ZipFile(io.BytesIO(z.read(f)))
        except (KeyError, zipfile.BadZipFile, RuntimeError, MemoryError):
            continue
        meta = _json(inner, "fabric.mod.json")
        if meta:
            out.append(_mod_info(meta))
            out += _nested(inner, meta, depth + 1, budget)
    return out


def read_jar(path: Path) -> dict | None:
    """What a jar says about itself, or None when it declares nothing we understand. Cached by file time and size."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _cache:
        return _cache[key]
    info: dict | None = None
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            meta = _json(z, "fabric.mod.json") if "fabric.mod.json" in names else None
            if meta:
                info = {"kind": "fabric", **_mod_info(meta), "nested": _nested(z, meta, 0, [MAX_NESTED])}
            elif "plugin.yml" in names or "paper-plugin.yml" in names:
                info = {"kind": "paper", **_plugin_info(z.read("paper-plugin.yml" if "paper-plugin.yml" in names else "plugin.yml").decode("utf-8", "replace"))}
            elif "META-INF/mods.toml" in names or "META-INF/neoforge.mods.toml" in names:
                info = {"kind": "forge", "id": path.stem, "name": path.stem, "version": "", "depends": {}, "breaks": {}, "provides": [], "nested": []}
    except (OSError, zipfile.BadZipFile):
        info = {"kind": "broken", "id": path.stem, "name": path.stem, "version": "", "depends": {}, "breaks": {}, "provides": [], "nested": []}
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = info
    return info


def _plugin_info(text: str) -> dict:
    def field(k: str) -> str:
        m = re.search(rf"^{k}:\s*['\"]?([^\n'\"#]+)", text, re.M)
        return m.group(1).strip() if m else ""

    deps: list[str] = []
    m = re.search(r"^(?:depend|dependencies):\s*\[([^\]]*)\]", text, re.M)
    if m:
        deps = [x.strip(" '\"") for x in m.group(1).split(",") if x.strip(" '\"")]
    else:
        m = re.search(r"^depend:\s*\n((?:[ \t]+-[^\n]*\n?)+)", text, re.M)
        if m:
            deps = [x.strip(" -'\"\t") for x in m.group(1).splitlines() if x.strip(" -'\"\t")]
    name = field("name")
    return {"id": name, "name": name, "version": field("version"), "depends": {d: "*" for d in deps}, "breaks": {}, "provides": [], "nested": []}


def java_major(java_home: str) -> str:
    """The Java version string of a JDK folder ('21.0.8'), from its release file; '' if unknown."""
    try:
        for line in (Path(java_home) / "release").read_text().splitlines():
            if line.startswith("JAVA_VERSION="):
                v = line.split("=", 1)[1].strip().strip('"')
                return v[2:] if v.startswith("1.") else v  # 1.8.0_402 -> 8.0_402
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------
def _nice(dep_id: str) -> str:
    return KNOWN.get(dep_id, (dep_id, dep_id))[0]


def check(server_dir: Path, mc: str, java_home: str, loader: str) -> list[dict]:
    """Problems for one server. Each: {level, kind, mod, file, text, fix}."""
    problems: list[dict] = []
    if loader not in ("fabric", "paper"):
        return problems
    sub = "mods" if loader == "fabric" else "plugins"
    folder = server_dir / sub
    jars = sorted(folder.glob("*.jar")) if folder.is_dir() else []
    mods = [(j, read_jar(j)) for j in jars]
    java_ver = java_major(java_home)

    def add(level, kind, jar, info, text, fix=""):
        problems.append({"level": level, "kind": kind, "mod": (info or {}).get("name") or jar.stem, "file": jar.name, "text": text, "fix": fix})

    provided: dict[str, str] = {}
    seen_ids: dict[str, Path] = {}
    for jar, info in mods:
        if not info or info["kind"] not in ("fabric", "paper"):
            continue
        for m in [info, *info.get("nested", [])]:
            for ident in [m["id"], *m["provides"]]:
                if ident:
                    provided.setdefault(ident, m["version"])
        if info["id"]:
            if info["id"] in seen_ids:
                add("bad", "duplicate", jar, info, f"{info['name']} is in the folder twice ({seen_ids[info['id']].name} and {jar.name}). The loader will refuse to start.",
                    f"Remove one of the two files.")
            seen_ids[info["id"]] = jar

    for jar, info in mods:
        if info is None:
            continue
        if info["kind"] == "broken":
            add("bad", "broken", jar, info, f"{jar.name} is not a readable jar file (damaged or incomplete upload).", "Delete it and add the mod again.")
            continue
        if info["kind"] == "forge":
            if loader == "fabric":
                add("bad", "wrong-loader", jar, info, f"{jar.name} is a Forge/NeoForge mod. This is a Fabric server, which cannot load it.",
                    "Find the Fabric version of that mod, or remove this file.")
            continue
        env = info.get("env")
        if loader == "fabric" and env == "client":
            add("warn", "client-only", jar, info, f"{info['name']} is a client-side mod. It does nothing on a server (players install it on their own game instead).", "Safe to remove.")
        for dep, spec in info["depends"].items():
            if dep in ("minecraft",):
                if mc and not satisfies(mc, spec):
                    add("bad", "minecraft", jar, info, f"{info['name']} is not built for Minecraft {mc} (it wants {_spec_text(spec)}).",
                        "Get the version of this mod for your Minecraft version, or remove it.")
            elif dep == "java":
                if java_ver and not satisfies(java_ver, spec):
                    add("bad", "java", jar, info, f"{info['name']} needs Java {_spec_text(spec)}, but this server uses Java {java_ver}.",
                        "Choose a newer Java in Settings if one is installed, or remove the mod.")
            elif dep in ("fabricloader", "fabric-loader"):
                continue  # the loader's version is not known here
            elif dep not in provided:
                nice = _nice(dep)
                hint = KNOWN.get(dep, (dep, dep))[1]
                add("bad", "missing-dep", jar, info, f"{info['name']} needs {nice}, which is not installed. The server will stop at startup.",
                    f"Search for \"{hint}\" under Add mods and install it.")
            elif provided[dep] and not satisfies(provided[dep], spec):
                add("bad", "dep-version", jar, info, f"{info['name']} needs {_nice(dep)} {_spec_text(spec)}, but version {provided[dep]} is installed.",
                    f"Update {_nice(dep)} (or {info['name']}) so the versions agree.")
        for other, spec in info["breaks"].items():
            if other in provided and (not provided[other] or satisfies(provided[other], spec)):
                add("bad", "conflict", jar, info, f"{info['name']} says it cannot be used with {_nice(other)}, which is installed.", "Remove one of them.")
    return problems


def _spec_text(spec) -> str:
    return " or ".join(str(s) for s in spec) if isinstance(spec, list) else str(spec)
