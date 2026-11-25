#!/bin/bash
# Mod dependency / Java / version checks. Needs the full install and MCSweb running. Run as root:
#   sudo bash tests/wsl-modcheck.sh
# Uses made-up jars for each kind of problem, and (if present) the real mods in the 'smoke' server.
set -u
B=http://localhost:8080
J="$(mktemp)"
LIBDIR=/opt/mcs/lib/MCS
T=$(mktemp -d)
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
cleanup () { /usr/local/bin/MCSkey revoke "mc$$" >/dev/null 2>&1; rm -rf "$T" "$J" "$J".*; }
trap cleanup EXIT

echo "-- version ranges and each kind of problem"
cat > $T/t.py <<'EOF'
import sys, json, zipfile, io, os
sys.path.insert(0, sys.argv[1])
import mcsmodcheck as c
from pathlib import Path
def t(d, ok): print(("PASS  " if ok else "FAIL  ") + d)

S = c.satisfies
t("exact version", S("1.21.11", "1.21.11") and not S("1.21.10", "1.21.11"))
t(">= and <", S("1.21.11", ">=1.21.11 <1.22") and not S("1.22", ">=1.21.11 <1.22"))
t("a pre-release lower bound is met by the release", S("1.21.11", ">=1.21.6-rc.1"))
t("tilde", S("1.21.11", "~1.21.4") and not S("1.22.0", "~1.21.4"))
t("caret", S("1.9.0", "^1.2.0") and not S("2.0.0", "^1.2.0"))
t("x ranges", S("1.21.5", "1.21.x") and not S("1.22.0", "1.21.x"))
t("a list means any of", S("1.20.1", [">=1.21", "1.20.1"]))
t("star matches anything", S("0.0.1", "*"))
t("a build suffix is ignored", S("0.3.6.0.0+1.21.11", ">=0.3"))
t("a version it cannot read is not called a problem", S("weird", ">=1.0"))
t("java 21 meets >=21 but not >=22", S("21.0.8", ">=21") and not S("21.0.8", ">=22"))

root = Path(sys.argv[2])
def jar(folder, name, files):
    p = root / folder; p.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p / name, "w") as z:
        for k, v in files.items(): z.writestr(k, v if isinstance(v, (str, bytes)) else json.dumps(v))
def fmj(id, ver="1.0", depends=None, **kw): return {"fabric.mod.json": {"id": id, "name": id.title(), "version": ver, "depends": depends or {}, **kw}}
# a made-up Fabric API: provides "fabric", carries a nested mod
inner = io.BytesIO()
with zipfile.ZipFile(inner, "w") as z: z.writestr("fabric.mod.json", json.dumps({"id": "fabric-resource-loader-v0", "version": "3.0"}))
jar("mods", "fabric-api.jar", {**fmj("fabric-api", "0.1", provides=["fabric"], jars=[{"file": "META-INF/jars/frl.jar"}]), "META-INF/jars/frl.jar": inner.getvalue()})
open(root / "java-home-release", "w")
(root / "jdk").mkdir(); (root / "jdk" / "release").write_text('JAVA_VERSION="21.0.8"\n')
jar("mods", "needs-api.jar", fmj("needsapi", depends={"fabric": "*", "fabricloader": ">=0.15", "fabric-resource-loader-v0": ">=3.0"}))
jar("mods", "needs-missing.jar", fmj("needsmissing", depends={"cloth-config": "*"}))
jar("mods", "new-mc.jar", fmj("newmc", depends={"minecraft": ">=1.22"}))
jar("mods", "new-java.jar", fmj("newjava", depends={"java": ">=22"}))
jar("mods", "libx.jar", fmj("libx", "1.0"))
jar("mods", "wants-libx2.jar", fmj("wantslibx2", depends={"libx": ">=2.0"}))
jar("mods", "breaker.jar", fmj("breaker", breaks={"libx": "*"}))
jar("mods", "forge-mod.jar", {"META-INF/mods.toml": "modLoader='javafml'"})
(root / "mods" / "broken.jar").write_bytes(b"not a zip at all")
jar("mods", "dup-a.jar", fmj("dupmod")); jar("mods", "dup-b.jar", fmj("dupmod"))
jar("mods", "clientside.jar", fmj("clientside", environment="client"))
jar("mods", "plain-lib.jar", {"readme.txt": "no metadata"})

probs = c.check(root, "1.21.11", str(root / "jdk"), "fabric")
by = {(p["file"], p["kind"]): p for p in probs}
t("bundled mods count as installed (Fabric API provides 'fabric' and its nested mods)", ("needs-api.jar", "missing-dep") not in by and ("needs-api.jar", "dep-version") not in by)
t("a missing mod is named plainly with where to find it", "Cloth Config" in by.get(("needs-missing.jar", "missing-dep"), {}).get("text", "") and "Add mods" in by[("needs-missing.jar", "missing-dep")]["fix"])
t("wrong Minecraft version", ("new-mc.jar", "minecraft") in by and "1.21.11" in by[("new-mc.jar", "minecraft")]["text"])
t("Java too old for a mod", ("new-java.jar", "java") in by and "Java 21.0.8" in by[("new-java.jar", "java")]["text"])
t("an installed mod that is too old", ("wants-libx2.jar", "dep-version") in by)
t("two mods that cannot be used together", ("breaker.jar", "conflict") in by)
t("a Forge mod on a Fabric server", ("forge-mod.jar", "wrong-loader") in by)
t("a damaged jar", ("broken.jar", "broken") in by)
t("the same mod twice", any(p["kind"] == "duplicate" for p in probs))
t("a client-only mod is a gentle warning, not an error", by.get(("clientside.jar", "client-only"), {}).get("level") == "warn")
t("a jar with no metadata is left alone", not any(p["file"] == "plain-lib.jar" for p in probs))
t("healthy mods raise nothing", not any(p["file"] in ("fabric-api.jar", "libx.jar") for p in probs))
t("no check without a loader", c.check(root, "1.21.11", str(root / "jdk"), "") == [])

# Paper plugins
jar("plugins", "econ.jar", {"plugin.yml": "name: Econ\nversion: 1\ndepend: [Vault, Other]\n"})
jar("plugins", "vault.jar", {"plugin.yml": "name: Vault\nversion: 1\n"})
pp = c.check(root, "1.21.11", str(root / "jdk"), "paper")
t("a Paper plugin that needs a plugin that is not there", any(p["kind"] == "missing-dep" and "Other" in p["text"] for p in pp) and not any("Vault" in p["text"] for p in pp))
EOF
python3 -I $T/t.py $LIBDIR $T/srv 2>&1 | sed -n '1,60p' > $T/out.txt; cat $T/out.txt
grep -q Traceback $T/out.txt && bad "check script crashed"; grep -q '^FAIL' $T/out.txt && fail=1

echo "-- the real mods in the smoke server"
if [[ -d /opt/minecraft/instance/smoke/mods ]] && ls /opt/minecraft/instance/smoke/mods | grep -qi travel; then
  JH=$(grep '^JAVA_HOME=' /opt/minecraft/instance/smoke/service.conf | cut -d= -f2-)
  cat > $T/real.py <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import mcsmodcheck as c
from pathlib import Path
p = c.check(Path("/opt/minecraft/instance/smoke"), "1.21.11", sys.argv[2], "fabric")
for x in p: print("   found:", x["file"], "|", x["text"])
ok = [x["file"] for x in p if x["level"] == "bad"] == ["TrustedTravelFabric-1.1+1.21.11.jar"]
print(("PASS  " if ok else "FAIL  ") + "only Trusted Travel is flagged (it needs Fabric API); C2ME, Lithium and FerriteCore are fine")
EOF
  python3 -I $T/real.py $LIBDIR "$JH" | tee $T/real.out; grep -q '^FAIL' $T/real.out && fail=1
else
  echo "SKIP  the smoke server no longer has the Trusted Travel jar (the problem was fixed)"
fi

echo "-- web API"
KEY="$(/usr/local/bin/MCSkey add "mc$$" --quiet)"
curl -s -o /dev/null -b "$J" -c "$J" -H 'X-MCS: 1' -H 'Content-Type: application/json' --data "{\"key\":\"$KEY\"}" $B/api/login
CODE=$(curl -s -o "$J.b" -w '%{http_code}' -b "$J" -H 'X-MCS: 1' $B/api/instances/smoke/modcheck); [[ $CODE == 200 ]] && pass "modcheck endpoint answers" || bad "modcheck $CODE" "$(cat $J.b)"
CODE=$(curl -s -o "$J.b" -w '%{http_code}' -b "$J" -H 'X-MCS: 1' $B/api/instances/nope/modcheck); [[ $CODE == 404 ]] && pass "unknown server is a 404" || bad "unknown server gave $CODE"
if ls /opt/minecraft/instance/smoke/mods | grep -qi travel; then
  curl -s -b "$J" -H 'X-MCS: 1' $B/api/notices | grep -q '"id": "mods:smoke:missing-dep' && pass "the problem reaches the Notices list" || bad "no mod notice" "$(curl -s -b $J -H 'X-MCS: 1' $B/api/notices | head -c 400)"
fi

echo; [[ $fail == 0 ]] && echo "all passed" || echo "FAILURES"; exit $fail
