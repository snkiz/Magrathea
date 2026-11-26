#!/bin/bash
# Smoke test for MCSfetch. Uses a throwaway MCS_HOME, touches nothing else.
# Needs network access. Run from anywhere:  bash tests/wsl-smoke.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
M="python3 -I $HERE/mcs/lib/MCS/mcsfetch.py"
export MCS_HOME="$(mktemp -d)"
trap 'rm -rf "$MCS_HOME"' EXIT
mkdir -p "$MCS_HOME"/instance/{fab,pap}
fail=0
check () {  # check "description" expected_rc command...
  local d="$1" want="$2"; shift 2
  "$@" >"$MCS_HOME/out" 2>&1; local rc=$?
  if [[ $rc -eq $want ]]; then echo "PASS  $d"; else echo "FAIL  $d (rc=$rc, wanted $want)"; sed 's/^/        /' "$MCS_HOME/out"; fail=1; fi
}
has () { grep -q -- "$1" "$MCS_HOME/out" && echo "PASS    ...says '$1'" || { echo "FAIL    ...missing '$1'"; sed 's/^/        /' "$MCS_HOME/out"; fail=1; }; }

check "versions list"                     0 $M versions vanilla --limit 2
check "fabric jar downloads"              0 $M jar fab --type fabric --mc 1.21.11
check "service.conf points at the jar"    0 grep -q '^SERVER=fabric-server-mc.1.21.11' "$MCS_HOME/instance/fab/service.conf"
check "bad instance name rejected"        1 $M jar '../x' --type paper
check "missing instance rejected"         1 $M jar nope --type paper
check "install mod + required dep"        0 $M add fab owo-lib
check "dep tracked as dependency"         0 grep -q '"explicit": false' "$MCS_HOME/instance/fab/mcs.json"
check "removing a needed dep is refused"  1 $M remove fab fabric-api
$M remove fab fabric-api >"$MCS_HOME/out" 2>&1; has "required by: oωo"
check "client-only mod refused"           1 $M add fab sodium
check "unknown mod fails"                 1 $M add fab this-mod-does-not-exist-xyz
check "update when current"               0 $M update fab
check "remove --prune takes dep too"      0 $M remove fab owo-lib --prune
check "mods dir is empty again"           0 test -z "$(ls -A "$MCS_HOME/instance/fab/mods")"
check "paper jar (dry run)"               0 $M jar pap --type paper --mc 1.21.11 -n
check "paper plugin from Modrinth"        0 $M add pap luckperms --mc 1.21.11 --loader paper
check "plugins dir is setgid group-rwx"   0 test "$(stat -c %a "$MCS_HOME/instance/pap/plugins")" = 2770
check "curseforge refused for paper"      1 $M add pap curseforge:1
check "curseforge without a project key"  1 $M add fab curseforge:1

echo "-- download safety"
python3 -I - "$HERE/mcs/lib/MCS" <<'PY'
import sys, pathlib, tempfile, os
sys.path.insert(0, sys.argv[1]); import mcsfetch as m
d = pathlib.Path(tempfile.mkdtemp()); bad = 0
cases = [("x.jar", "https://cdn.modrinth.com/data/gvQqBUqZ/versions/Ow7wA0kG/lithium-fabric-0.21.4%2Bmc1.21.11.jar", {"sha1": "0"*40}, "checksum mismatch"),
         ("x.jar", "http://cdn.modrinth.com/x", {}, "allowed host"),
         ("x.jar", "https://evil.example.com/x.jar", {}, "allowed host"),
         ("x.exe", "https://cdn.modrinth.com/x", {}, "unsafe file name")]
for name, url, h, why in cases:
    try: m.download(url, d, name, h); print("FAIL  not rejected:", name, url); bad = 1
    except m.McsError as e:
        ok = why in str(e); print(("PASS  " if ok else "FAIL  ") + f"rejected ({why})"); bad |= not ok
left = os.listdir(d); print(("PASS  " if not left else "FAIL  ") + f"no leftover files {left}"); bad |= bool(left)
sys.exit(bad)
PY
[[ $? -eq 0 ]] || fail=1
echo; [[ $fail -eq 0 ]] && echo "ALL PASSED" || echo "SOME FAILED"
exit $fail
