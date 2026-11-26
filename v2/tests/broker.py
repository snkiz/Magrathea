#!/usr/bin/env python3
"""Checks what the key broker allows, limits and logs. No network, no service, no real key.

Safe to run anywhere:  python3 -I tests/broker.py
"""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # leave no __pycache__ in the source tree
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcs" / "lib" / "MCS"))
import mcscred as c  # noqa: E402

failures = 0
PROJECT, OWN = "project-key-not-real", "own-key-not-real-123"
CALLER = {"unit": "MCS@smoke.service", "pid": 123, "uid": 999}
sent = []


def fake_fetch(platform, url, headers):
    sent.append((url, headers))
    return b'{"data": []}'


c.mf._fetch = fake_fetch
os.environ.pop("CREDENTIALS_DIRECTORY", None)


def check(name, ok):
    global failures
    print(("PASS  " if ok else "FAIL  ") + name)
    failures += not ok


def ask(request, caller=CALLER):
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        reply = c.handle(request, caller)
    return reply, log.getvalue()


keys = tempfile.TemporaryDirectory()  # stands in for what systemd hands the service
os.environ["CREDENTIALS_DIRECTORY"] = keys.name
Path(keys.name, "keys_curseforge-project").write_text("# only comments\n")
reply, _ = ask({"status": True})
check("no key: status says unavailable", reply == {"ok": True, "available": False})
reply, _ = ask({"route": "/mods/1"})
check("no key: a request is refused and nothing sent", not reply["ok"] and "not available" in reply["error"] and not sent)

Path(keys.name, "keys_curseforge-project").write_text(f"# the project key\n{PROJECT}\n")
reply, log = ask({"route": "/mods/search", "params": {"gameId": 432, "searchFilter": "secretsearch"}, "claimed": "mcsweb.py"})
check("search is made with the project key", reply["ok"] and sent[-1][1] == {"x-api-key": PROJECT})
check("to CurseForge's address", sent[-1][0].startswith("https://api.curseforge.com/v1/mods/search?"))
check("the log names the unit from the kernel and the claim", "MCS@smoke.service" in log and '"claimed": "mcsweb.py"' in log)
check("the log has no key, search words or answer", PROJECT not in log + str(reply) and "secretsearch" not in log and "data" not in log)

for route in ("/games", "/mods/1/../../games", "/mods/search?x=1", "https://evil.example/", "/mods/1/files/2/download-url", "", None, 5):
    before = len(sent)
    reply, _ = ask({"route": route})
    check(f"address refused: {route!r}", not reply["ok"] and len(sent) == before)
for params in ({"apiKey": "x"}, {"slug": ["a"]}, {"slug": "x" * 500}, "slug=a"):
    before = len(sent)
    reply, _ = ask({"route": "/mods/search", "params": params})
    check(f"options refused: {str(params)[:40]}", not reply["ok"] and len(sent) == before)
check("junk is refused", not ask(None)[0]["ok"] and not ask([1])[0]["ok"])
_, log = ask({"route": "/x\n[MCScred audit] {}", "claimed": "a\nb"})
check("a request cannot add its own log lines", log.count("\n") == 1)

Path(keys.name, "keys_curseforge").write_text(OWN + "\n")
_, log = ask({"route": "/mods/1"})
check("the owner's own key wins", sent[-1][1] == {"x-api-key": OWN} and '"key": "own"' in log and OWN not in log)
check("the program itself holds no key", not hasattr(c.mf, "CURSEFORGE_KEY"))
keys.cleanup()

limit = c.Limiter()
check("one caller is limited", sum(limit.allow("a", 0) for _ in range(c.PER_CALLER + 5)) == c.PER_CALLER)
check("another caller still gets through", limit.allow("b", 0))
check("everyone together is limited", sum(limit.allow(f"u{i}", 0) for i in range(c.TOTAL)) == c.TOTAL - c.PER_CALLER - 1)
check("the limit lifts after the window", limit.allow("a", c.WINDOW + 1))

print(f"\nfailures: {failures}")
sys.exit(1 if failures else 0)
