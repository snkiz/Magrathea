#!/usr/bin/env python3
"""Checks the API boundary in mcsfetch without touching the network or the machine.

Safe to run anywhere:  python3 -I tests/api-boundary.py
"""
import email.message
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True  # leave no __pycache__ in the source tree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcs" / "lib" / "MCS"))
import mcsfetch as m  # noqa: E402

failures = 0


def check(name, ok):
    global failures
    print(("PASS  " if ok else "FAIL  ") + name)
    failures += not ok


def headers(**kw):
    h = email.message.Message()
    for k, v in kw.items():
        h[k.replace("_", "-")] = v
    return h


class Reply(io.BytesIO):
    def __init__(self, body=b'{"n": 1}', **kw):
        super().__init__(body)
        self.headers = headers(**kw)


class FakeNet:
    """Stands in for the network: records each request, hands back the queued replies."""

    def __init__(self):
        self.seen, self.queue = [], []

    def open(self, req, timeout=0):
        self.seen.append(req)
        item = self.queue.pop(0) if self.queue else Reply()
        if isinstance(item, Exception):
            raise item
        return item


net = m._opener = FakeNet()
slept = []
m.time.sleep = slept.append


def refused(fn, text):
    try:
        fn()
    except m.McsError as e:
        return text in str(e)
    return False


m.api_get("modrinth", "/search", {"query": "x"})
req = net.seen[-1]
check("User-Agent names the project and its address",
      req.get_header("User-agent") == m.UA and "github.com/snkiz/Magrathea" in m.UA)
check("address is the platform base plus the route", req.full_url == "https://api.modrinth.com/v2/search?query=x")
check("no API key sent to Modrinth", not req.has_header("X-api-key"))
check("a route cannot change the host", refused(lambda: m.api_get("modrinth", ".evil.example/x"), "bad API route"))

asked = []
m.broker_ask = lambda request: asked.append(request) or {"ok": True, "body": '{"data": []}'}
before = len(net.seen)
m.api_get("curseforge", "/mods/1")
m.api_get("curseforge", "/mods/1")
check("CurseForge requests go to the key broker", asked[0] == {"route": "/mods/1", "params": {}})
check("and this program sends nothing to CurseForge itself", len(net.seen) == before)
check("CurseForge answers are never cached", len(asked) == 2)

before = len(net.seen)
first = m.api_get("paper")
first["n"] = 99
second = m.api_get("paper")
check("Paper answers are reused for a while", len(net.seen) - before == 1)
check("changing an answer does not change the cached copy", second["n"] == 1)

m._cache.clear()
net.queue = [urllib.error.HTTPError("u", 429, "slow down", headers(Retry_After="7"), None), Reply()]
slept.clear()
check("a 429 is retried", m.api_get("modrinth", "/a") == {"n": 1})
check("and waits as long as the service asked", slept == [7.0])

net.queue = [urllib.error.HTTPError("u", 429, "slow down", headers(Retry_After="99999"), None), Reply()]
slept.clear()
m.api_get("modrinth", "/b")
check("but never longer than the limit", slept == [m.MAX_WAIT])

net.queue = [Reply(X_Ratelimit_Remaining="0", X_Ratelimit_Reset="5"), Reply()]
slept.clear()
m.api_get("modrinth", "/c")
m.api_get("modrinth", "/d")
check("no requests left: the next one waits", len(slept) == 1 and 4 < slept[0] <= 5)

handler = m._SafeRedirect()
start = urllib.request.Request("https://api.curseforge.com/v1/x", headers={"x-api-key": "test-key-not-real"})
other = handler.redirect_request(start, None, 302, "moved", headers(), "https://edge.forgecdn.net/y")
same = handler.redirect_request(start, None, 302, "moved", headers(), "https://api.curseforge.com/v1/y")
check("a redirect to another host drops the key", not other.has_header("X-api-key"))
check("a redirect on the same host keeps it", same.has_header("X-api-key"))
check("a redirect off the allowed list is refused", refused(
    lambda: handler.redirect_request(start, None, 302, "moved", headers(), "https://evil.example/y"), "allowed host"))

check("a Mojang list entry on another host is refused",
      refused(lambda: m.mojang_entry("https://evil.example/v1.json"), "unexpected address"))

print(f"\nfailures: {failures}")
sys.exit(1 if failures else 0)
