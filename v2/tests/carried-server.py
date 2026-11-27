#!/usr/bin/env python3
"""Checks what mcsfetch reads from a carried-over server, and what 'record' writes. Touches only a
temporary folder.

Safe to run anywhere:  python3 -I tests/carried-server.py
"""
import contextlib
import gzip
import io
import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True  # leave no __pycache__ in the source tree
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcs" / "lib" / "MCS"))
import mcsfetch as mf  # noqa: E402

failures = 0


def check(what: str, ok: bool) -> None:
    global failures
    failures += not ok
    print(("PASS  " if ok else "FAIL  ") + what)


def tag(kind: int, name: str, body: bytes) -> bytes:
    return bytes([kind]) + struct.pack(">H", len(name)) + name.encode() + body


def text(s: str) -> bytes:
    return struct.pack(">H", len(s)) + s.encode()


def level(version: str) -> bytes:
    """A small level.dat, with other things (and a decoy "Version") ahead of the one that counts."""
    decoy = tag(10, "Player", tag(10, "Version", tag(8, "Name", text("9.9")) + b"\0") + b"\0")
    other = tag(3, "n", b"\0\0\0\1") + tag(9, "l", b"\x08\0\0\0\2" + text("a") + text("b")) + tag(11, "a", b"\0\0\0\1\0\0\0\0")
    wanted = tag(10, "Version", tag(3, "Id", b"\0\0\0\1") + tag(8, "Name", text(version)) + b"\0")
    return gzip.compress(tag(10, "", tag(10, "Data", decoy + other + wanted + b"\0") + b"\0"))


def jar(path: Path, main_class: str, files: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n")
        for name, body in files.items():
            z.writestr(name, body)


with tempfile.TemporaryDirectory() as tmp:
    mf.INSTANCES = Path(tmp)
    d = Path(tmp) / "s"
    (d / "world").mkdir(parents=True)
    dat = d / "world" / "level.dat"

    check("nothing to go on: no guess", mf.detect(d) == {"type": "", "mc": "", "mc_from": "", "world_mc": "", "has_jar": False})
    dat.write_bytes(level("1.20.4"))
    got = mf.detect(d)
    check("the version comes from the world, past other tags and a decoy", got["mc"] == "1.20.4" and got["mc_from"] == "your world" and got["type"] == "")

    jar(d / "fab.jar", "net.fabricmc.installer.ServerLauncher", {"install.properties": "fabric-loader-version=0.1\ngame-version=1.21.11"})
    got = mf.detect(d)
    check("a Fabric jar gives type and version, and the world's is still reported",
          (got["type"], got["mc"], got["mc_from"], got["world_mc"]) == ("fabric", "1.21.11", "the server jar", "1.20.4"))
    jar(d / "van.jar", "net.minecraft.bundler.Main", {"version.json": '{"id": "1.21.1"}'})
    check("a vanilla jar is recognised", mf.jar_identity(d / "van.jar") == ("vanilla", "1.21.1"))
    check("two jars that disagree: no guess about the type", mf.detect(d)["type"] == "")
    check("the jar service.conf names settles it", mf.detect(d, "van.jar")["type"] == "vanilla")
    jar(d / "odd.jar", "example.Other", {"version.json": '{"id": "1.8"}'})
    check("an unknown jar is not called vanilla", mf.jar_identity(d / "odd.jar") == ("", ""))
    (d / "bad.jar").write_bytes(b"not a zip")
    check("a broken jar is ignored", mf.jar_identity(d / "bad.jar") == ("", ""))
    jar(d / "inj.jar", "net.minecraft.bundler.Main", {"version.json": json.dumps({"id": "1.0\n<script>"})})
    check("a version that is not a version is dropped", mf.jar_identity(d / "inj.jar") == ("vanilla", ""))

    for what, body in (("not gzip", b"junk"), ("cut short", level("1.20.4")[:20]),
                       ("a list claiming billions of nothing", gzip.compress(tag(10, "", tag(9, "x", b"\0\x7f\xff\xff\xff")))),
                       ("nested far too deep", gzip.compress(tag(10, "", tag(10, "x", b"") * 5000))),
                       ("an odd version string", level("../../etc"))):
        dat.write_bytes(body)
        check(f"level.dat {what}: no version, no crash", mf.world_version(d) == "")
    (Path(tmp) / "other" / "w").mkdir(parents=True)
    (Path(tmp) / "other" / "w" / "level.dat").write_bytes(level("1.2.3"))
    (d / "server.properties").write_text("level-name=../other/w\n")
    check("level-name cannot point outside the server folder", mf.world_version(d) == "")
    (d / "server.properties").unlink()

    def run(*argv: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mf.main(list(argv))

    before = sorted(p.name for p in d.iterdir())
    check("record refuses something that is not a version", run("record", "s", "--type", "fabric", "--mc", "latest") == 1 and not (d / "mcs.json").exists())
    check("record writes type, version and loader", run("record", "s", "--type", "fabric", "--mc", "1.21.11") == 0
          and mf.load_meta(d) == {"type": "fabric", "mc": "1.21.11", "loader": "fabric"})
    check("record adds mcs.json and nothing else", sorted(p.name for p in d.iterdir()) == sorted(before + ["mcs.json"]))
    check("record does not overwrite an existing record", run("record", "s", "--type", "vanilla", "--mc", "1.0") == 1 and mf.load_meta(d)["mc"] == "1.21.11")

print(f"\nfailures: {failures}")
sys.exit(1 if failures else 0)
