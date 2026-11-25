#!/bin/bash
# Web API checks for the safe defaults (whitelist, first operator, ports, players).
# Needs the full install and MCSweb running, plus network (Mojang name lookup).
# Run as a user in the minecraft group:  bash tests/wsl-web-defaults.sh
set -u
B=http://localhost:8080
J="$(mktemp)"; trap 'rm -f "$J" "$J".*; $MCW revoke "$KEYNAME" >/dev/null 2>&1' EXIT
MCW="sudo /usr/local/bin/MCSkey"; KEYNAME="t$$"; TOKEN="$($MCW add "$KEYNAME" --quiet)"
I=/opt/minecraft/instance
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
req () {
  local m="$1" p="$2" d="${3:-}"; local args=(-s -o "$J.body" -w '%{http_code}' -b "$J" -c "$J" -X "$m" -H 'X-MCS: 1' "$B$p")
  [[ -n "$d" ]] && args+=(-H 'Content-Type: application/json' --data "$d")
  CODE=$(curl "${args[@]}"); BODY=$(cat "$J.body")
}
want () { if [[ "$CODE" == "$2" ]] && { [[ -z "${3:-}" ]] || grep -q -- "$3" <<<"$BODY"; }; then pass "$1"; else bad "$1 (got $CODE, wanted $2 ${3:-})" "$BODY"; fi; }

req POST /api/login "{\"key\":\"$TOKEN\"}"; want "login" 200
rm -rf $I/wd1 $I/wd2 2>/dev/null

req GET /api/newserver; want "newserver suggests a port" 200 '"port"'
PORT=$(grep -o '"port": [0-9]*' <<<"$BODY" | grep -o '[0-9]*')

req POST /api/instances '{"name":"wd1","agree":true,"op_name":"","port":'$PORT'}'; want "empty operator name refused" 400
[[ ! -d $I/wd1 ]] && pass "nothing created on refusal" || bad "wd1 left behind"
req POST /api/instances '{"name":"wd1","agree":true,"op_name":"x","port":'$PORT'}'; want "bad operator name refused" 400 "3-16"
req POST /api/instances '{"name":"wd1","agree":true,"op_name":"Notch","port":80}'; want "privileged port refused" 400 "1024"
req POST /api/instances '{"name":"wd1","agree":true,"op_name":"Notch","port":"abc"}'; want "non-numeric port refused" 400
req POST /api/instances '{"name":"wd1","agree":true,"op_name":"Notch","port":25565}'; want "port used by another server refused" 400 "already used"
[[ ! -d $I/wd1 ]] && pass "still nothing created" || bad "wd1 left behind after refusals"

req POST /api/instances '{"name":"wd1","agree":true,"op_name":"Notch","port":'$PORT'}'; want "create with port + operator" 200
grep -q "^server-port=$PORT" $I/wd1/server.properties && pass "port written" || bad "port not written"
grep -q '^white-list=true' $I/wd1/server.properties && pass "whitelist on" || bad "whitelist not on"
grep -q '"name": "Notch"' $I/wd1/whitelist.json && grep -q '"level": 4' $I/wd1/ops.json && pass "operator whitelisted" || bad "whitelist/ops missing"

req POST /api/instances/wd1/players '{"name":"Notch"}'; want "re-adding is harmless" 200
test "$(grep -c '"uuid"' $I/wd1/whitelist.json)" = 1 && pass "no duplicate entry" || bad "duplicate whitelist entry"
req POST /api/instances/wd1/players '{"name":"jeb_"}'; want "add a second player" 200 '"name": "jeb_"'
req POST /api/instances/wd1/players '{"name":"../../etc"}'; want "junk name refused" 400
req POST /api/instances/nope/players '{"name":"Notch"}'; want "unknown server 404" 404

req PUT /api/instances/wd1/properties '{"values":{"server-port":"25565"}}'; want "port clash on edit refused" 400 "already used"
req PUT /api/instances/wd1/properties '{"values":{"server-port":"'$PORT'"}}'; want "own port is fine" 200
req PUT /api/instances/wd1/properties '{"values":{"online-mode":"false"}}'; want "online-mode=false allowed, with a warning" 200 "does not check players"
req PUT /api/instances/wd1/properties '{"values":{"online-mode":"true"}}'; want "online-mode=true no warning" 200 '"warnings": \[\]'

rm -rf $I/wd1 2>/dev/null
echo; [[ $fail == 0 ]] && echo "all passed" || echo "FAILURES"; exit $fail
