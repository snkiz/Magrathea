#!/usr/bin/env python3
"""Checks how mcsrcon reads and rewrites server.properties. Touches only a temporary folder.

Safe to run anywhere:  python3 -I tests/remote-control.py
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # leave no __pycache__ in the source tree
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcs" / "lib" / "MCS"))
import mcsrcon as r  # noqa: E402

failures = 0


def check(name, ok):
    global failures
    print(("PASS  " if ok else "FAIL  ") + name)
    failures += not ok


tmp = Path(tempfile.mkdtemp())
prop = tmp / "server.properties"
r.STATE_FILE = str(tmp / "remote.json")  # not there: nothing is allowed


def start(text, name="s"):
    """What a server start does to this server.properties -> (settings afterwards, messages)."""
    prop.write_text(text, encoding="utf-8", newline="\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        r.apply(name, prop)
    return r.values(prop.read_text(encoding="utf-8")), out.getvalue()


# every way Java would read "RCON is on"
for label, line in {
    "plain": "enable-rcon=true", "spaces": "  enable-rcon = true", "colon": "enable-rcon:true",
    "blank separator": "enable-rcon true", "capitals": "enable-rcon=TRUE", "trailing space": "enable-rcon=true ",
    "escaped key": r"enable\-rcon=true", "unicode escape": r"enable-rcon=true",
    "escaped value": r"enable-rcon=true", "continued line": "enable-\\\n   rcon=true",
    "repeated, last wins": "enable-rcon=false\nenable-rcon=true",
}.items():
    after, said = start(line + "\nmotd=hi\n")
    check(f"not allowed, switched off: {label}", after["enable-rcon"] == "false" and "switched RCON off" in said)

after, _ = start("enable-rcon=true\nrcon.password=hunter2\nmanagement-server-enabled=true\nmotd=hi\n")
check("the old password is cleared", after["rcon.password"] == "")
check("the management server is switched off too", after["management-server-enabled"] == "false")
check("other settings are left alone", after["motd"] == "hi")

untouched = "#Minecraft server properties\nenable-rcon=false\nrcon.password=\nmotd=a \\\n  long one\n"
start(untouched)
check("a file with nothing to fix is not rewritten", prop.read_text(encoding="utf-8") == untouched)

after, _ = start("# enable-rcon=true\nmotd=hi\n")
check("a commented-out line is not a setting", "enable-rcon" not in after)

(tmp / "remote.json").write_text('{"servers": {"s": ["rcon"]}}')
after, said = start("enable-rcon=false\nrcon.password=\nmanagement-server-enabled=true\n")
check("allowed: RCON is switched on", after["enable-rcon"] == "true")
check("allowed: a password is made", len(after["rcon.password"]) >= 20)
check("allowed: the log warns", "RCON is ON" in said and after["rcon.password"] not in said)
check("allowing RCON does not allow the management server", after["management-server-enabled"] == "false")
after, _ = start("enable-rcon=true\nrcon.password=my-own\n")
check("allowed: an owner's own password is kept", after["rcon.password"] == "my-own")
after, _ = start("enable-rcon=true\n", name="other")
check("permission is per server", after["enable-rcon"] == "false")

(tmp / "remote.json").write_text("not json")
after, said = start("enable-rcon=true\n")
check("a damaged permission file allows nothing", after["enable-rcon"] == "false" and "cannot read" in said)

print(f"\nfailures: {failures}")
sys.exit(1 if failures else 0)
