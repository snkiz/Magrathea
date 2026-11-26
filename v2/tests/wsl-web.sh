#!/bin/bash
# API test for the MCS web interface. Needs the full install (tests/wsl-full.sh
# install create fetch) and the MCSweb service running. Run as a user in the
# minecraft group:  bash tests/wsl-web.sh
# Starts and stops the 'smoke' server (about 1-2 minutes).
set -u
B=http://localhost:8080
J="$(mktemp)"; trap 'rm -f "$J" "$J".*; $MCW revoke "$KEYNAME" >/dev/null 2>&1' EXIT
MCW="sudo /usr/local/bin/MCSkey"; KEYNAME="t$$"; TOKEN="$($MCW add "$KEYNAME" --quiet)"
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
# req METHOD PATH [json] -> sets CODE and BODY
req () {
  local m="$1" p="$2" d="${3:-}"; local args=(-s -o "$J.body" -w '%{http_code}' -b "$J" -c "$J" -X "$m" -H 'X-MCS: 1' "$B$p")
  [[ -n "$d" ]] && args+=(-H 'Content-Type: application/json' --data "$d")
  CODE=$(curl "${args[@]}"); BODY=$(cat "$J.body")
}
want () {  # want "desc" code [substring]
  if [[ "$CODE" == "$2" ]] && { [[ -z "${3:-}" ]] || grep -q -- "$3" <<<"$BODY"; }; then pass "$1"; else bad "$1 (got $CODE, wanted $2 ${3:-})" "$BODY"; fi
}

echo "-- auth & request hygiene"
CODE=$(curl -s -o /dev/null -w '%{http_code}' $B/api/instances); [[ $CODE == 401 ]] && pass "API closed without login" || bad "unauthenticated API call returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' $B/); [[ $CODE == 200 ]] && pass "login page is public" || bad "index returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example.com' $B/); [[ $CODE == 400 ]] && pass "foreign Host header rejected" || bad "Host check returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{}' $B/api/login); [[ $CODE == 403 ]] && pass "POST without X-MCS header blocked" || bad "CSRF header check returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-MCS: 1' -H 'Origin: http://evil.example.com' -H 'Content-Type: application/json' --data '{}' $B/api/login); [[ $CODE == 403 ]] && pass "foreign Origin blocked" || bad "Origin check returned $CODE"
req POST /api/login '{"key":"wrong"}'; want "wrong token refused" 401
req POST /api/login "{\"key\":\"$TOKEN\"}"; want "login with the real token" 200
grep -q 'HttpOnly' <(curl -s -i -X POST -H 'X-MCS: 1' -H 'Content-Type: application/json' --data "{\"key\":\"$TOKEN\"}" $B/api/login) && pass "cookie is HttpOnly" || bad "cookie flags"
curl -s -i -X POST -H 'X-MCS: 1' -H 'Content-Type: application/json' --data "{\"key\":\"$TOKEN\"}" $B/api/login | grep -qi 'SameSite=Strict' && pass "cookie is SameSite=Strict" || bad "SameSite flag"
curl -s -I $B/ | grep -qi 'Content-Security-Policy' && pass "CSP header present" || bad "no CSP"

echo "-- personal keys and the audit log"
test "$(sudo -u minecraft grep -c "$TOKEN" /etc/MCS/keys.json)" = 0 && pass "key is not stored in plain text" || bad "plain key found in keys.json"
sudo -u minecraft test ! -e /opt/minecraft/.config/MCS/web.token && pass "old shared web.token is gone" || bad "web.token still exists"
sudo -u minecraft sh -c "echo x >> /etc/MCS/keys.json" 2>/dev/null && bad "minecraft user can write the keys file" || pass "minecraft user cannot write the keys file"
sudo -u minecraft sh -c "rm -f /etc/MCS/keys.json" 2>/dev/null; test -s /etc/MCS/keys.json && pass "minecraft user cannot delete the keys file" || bad "keys file deleted by minecraft"
sudo -u minecraft python3 -I /opt/mcs/lib/MCS/mcskey.py revoke "$KEYNAME" </dev/null >/dev/null 2>&1; sudo -u minecraft grep -q "$KEYNAME" /etc/MCS/keys.json && pass "minecraft user cannot revoke a key" || bad "minecraft user revoked a key"
req GET /api/auth; want "auth reports who is signed in" 200 "\"who\": \"$KEYNAME\""
$MCW add "$KEYNAME" >/dev/null 2>&1 && bad "duplicate key name accepted" || pass "duplicate key name refused"
OTHER="$($MCW add "$KEYNAME-2" --quiet)"
req POST /api/login "{\"key\":\"$OTHER\"}"; want "second person's key works" 200
$MCW list | grep -q "$KEYNAME-2" && pass "key list shows it" || bad "key list"
$MCW audit -n 50 | grep -q "$KEYNAME-2 .*login" && pass "audit log records the login under the key's name" || bad "no audit entry for login"
req POST /api/instances/nope/command '{"command":"list"}'  # unknown server: refused, but still audited (a real command would start the stopped smoke server through its socket)
$MCW audit -n 50 | grep -q "/nope/command -> 404.*list" && pass "audit log records the console command and its result" || bad "console command not audited"
$MCW revoke "$KEYNAME-2" >/dev/null
req GET /api/instances; want "revoked key's session ends at once" 401
req POST /api/login "{\"key\":\"$OTHER\"}"; want "revoked key cannot sign in" 401
req POST /api/login "{\"key\":\"$TOKEN\"}"; want "first key still works" 200
req POST /api/login '{"key":"abc123.notarealsecretvalue"}'; want "malformed key refused" 401

echo "-- read endpoints"
req GET /api/instances; want "list servers" 200 '"name": "smoke"'
req GET /api/instances/smoke; want "server detail" 200 '"properties"'
req GET /api/instances/..%2Fetc; want "path traversal in name rejected" 400
req GET /api/instances/nope; want "unknown server 404" 404
req GET /api/versions?type=fabric; want "version list" 200 '"1.21'
req GET /api/eula; want "eula status" 200 '"agreed": true'

echo "-- settings"
req PUT /api/instances/smoke/properties '{"values":{"motd":"hello from test","max-players":"7"}}'; want "save properties" 200
grep -q '^motd=hello from test' /opt/minecraft/instance/smoke/server.properties && grep -q '^max-players=7' /opt/minecraft/instance/smoke/server.properties && pass "properties written to disk" || bad "properties not on disk"
req PUT /api/instances/smoke/properties '{"values":{"bad key":"x"}}'; want "bad property key rejected" 400
req PUT /api/instances/smoke/properties '{"values":{"motd":"a\nb"}}'; want "newline in value rejected" 400
req PUT /api/instances/smoke/service '{"values":{"TIMER":"3","RAM":"-Xmx1G -Xms1G","SIGNOFF":"going down in @TIMER@s"}}'; want "save service.conf" 200
req PUT /api/instances/smoke/service '{"values":{"RAM":"lots"}}'; want "bad RAM rejected" 400
req PUT /api/instances/smoke/service '{"values":{"SERVER":"../../etc/passwd"}}'; want "SERVER path traversal rejected" 400
echo "-- bad request bodies get a clean 400 (security review F-2, F-3, I-1, F-4)"
req PUT /api/instances/smoke/properties '{"values":"notanobject"}'; want "properties: values as a string is a 400" 400 "values must be an object"
req PUT /api/instances/smoke/properties '{"values":["a"]}'; want "properties: values as a list is a 400" 400 "values must be an object"
req PUT /api/instances/smoke/service '{"values":"notanobject"}'; want "service: values as a string is a 400" 400 "values must be an object"
req PUT /api/instances/smoke/service '{"values":[1,2]}'; want "service: values as a list is a 400" 400 "values must be an object"
req PUT /api/instances/smoke/properties '{"values":{"motd":NaN}}'; want "NaN is not JSON: refused" 400
python3 -c 'print("["*100000+"]"*100000)' > "$J.deep"
CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -c "$J" -X PUT -H 'X-MCS: 1' -H 'Content-Type: application/json' --data-binary @"$J.deep" "$B/api/instances/smoke/properties"); BODY=$(cat "$J.body")
want "very deeply nested JSON gets a 400, not a dropped connection" 400
req GET /api/instances; want "server still answers after the nested body" 200
req PUT /api/instances/smoke/properties '{"values":{"motd":"AUDITMARK-9f3c"}}'
$MCW audit -n 80 | grep -q "AUDITMARK-9f3c" && bad "a changed setting's value appeared in the audit log" || pass "settings values are not written to the audit log"
req PUT /api/instances/smoke/service '{"values":{"JAVA_HOME":"/tmp"}}'; want "JAVA_HOME without java rejected" 400
req PUT /api/instances/smoke/service '{"values":{"EVIL":"x"}}'; want "unknown service key rejected" 400
grep -q '^TIMER=3' /opt/minecraft/instance/smoke/service.conf && pass "service.conf written" || bad "service.conf not updated"

echo "-- uploads"
printf 'PK\x03\x04fake-jar-body' > "$J.jar"
CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -X PUT -H 'X-MCS: 1' --data-binary @"$J.jar" "$B/api/instances/smoke/upload?dir=mods&name=test-upload.jar"); BODY=$(cat "$J.body"); want "upload a jar" 200
test -f /opt/minecraft/instance/smoke/mods/test-upload.jar && pass "jar landed in mods/" || bad "jar missing"
printf 'not a jar' > "$J.txt"
CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -X PUT -H 'X-MCS: 1' --data-binary @"$J.txt" "$B/api/instances/smoke/upload?dir=mods&name=fake.jar"); BODY=$(cat "$J.body"); want "non-jar content refused" 400 'does not look like a jar'
CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -X PUT -H 'X-MCS: 1' --data-binary @"$J.txt" "$B/api/instances/smoke/upload?dir=mods&name=..%2F..%2Fevil.jar"); BODY=$(cat "$J.body"); want "traversal in upload name refused" 400
CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -X PUT -H 'X-MCS: 1' --data-binary @"$J.txt" "$B/api/instances/smoke/upload?dir=mods&name=run.sh"); BODY=$(cat "$J.body"); want "wrong extension refused" 400
req GET /api/instances/smoke/files; want "file listing shows upload" 200 'test-upload.jar'
req DELETE "/api/instances/smoke/files?dir=mods&name=test-upload.jar"; want "delete upload" 200
test ! -f /opt/minecraft/instance/smoke/mods/test-upload.jar && pass "upload removed from disk" || bad "file still there"

echo "-- start through polkit, console, stop"
req POST /api/instances/smoke/command '{"command":"list"}'; want "console refused while stopped" 409
req POST /api/instances/smoke/action '{"action":"explode"}'; want "unknown action refused" 400
req POST /api/instances/smoke/action '{"action":"start"}'; want "start accepted (polkit allows it)" 200
for i in $(seq 1 90); do sleep 2; req GET /api/instances/smoke; grep -q '"state": "active"' <<<"$BODY" && break; done
grep -q '"state": "active"' <<<"$BODY" && pass "server became active after ~$((i*2))s" || bad "server never became active" "$(journalctl -u MCS@smoke -n 5 --no-pager -o cat 2>&1)"
( curl -s -N -m 4 -b "$J" "$B/api/instances/smoke/console?n=20" > "$J.sse" ) ; grep -q '^data: ' "$J.sse" && pass "console stream delivers log lines (SSE)" || bad "no SSE data" "$(head -c 300 "$J.sse")"
req POST /api/instances/smoke/command '{"command":"say api-test-marker"}'; want "send console command" 200
sleep 2; ( curl -s -N -m 3 -b "$J" "$B/api/instances/smoke/console?n=10" > "$J.sse" ); grep -q 'api-test-marker' "$J.sse" && pass "command reached the server console" || bad "marker not in console"
req POST /api/instances/smoke/players '{"name":"Notch","op":false}'; want "adding a player to a RUNNING server goes through its console" 200 '"live": true'
req POST /api/instances/smoke/players '{"name":"bad name!"}'; want "a bad player name is refused while running" 400
sleep 3; ( curl -s -N -m 3 -b "$J" "$B/api/instances/smoke/console?n=15" > "$J.sse" ); grep -qiE "whitelist" "$J.sse" && pass "the server itself handled the whitelist command" || bad "no whitelist reply in the console" "$(tail -3 $J.sse)"
req POST /api/instances/smoke/command '{"command":"whitelist remove Notch"}'; want "test player removed again" 200
req POST /api/instances/smoke/command '{"command":"a\nb"}'; want "multi-line command refused" 400
req POST /api/instances/smoke/command "{\"command\":\"$(printf 'x%.0s' $(seq 1 1100))\"}"; want "overlong command refused" 400

echo "-- backup while running"
req POST /api/instances/smoke/backups '{}'; want "start backup job" 200 '"id"'
JOB=$(grep -o '"id": "[0-9a-f]*"' <<<"$BODY" | head -1 | cut -d'"' -f4)
for i in $(seq 1 60); do sleep 2; req GET /api/jobs/$JOB; grep -q '"state": "running"' <<<"$BODY" || break; done
grep -q '"state": "done"' <<<"$BODY" && pass "backup job finished" || bad "backup job" "$BODY"
req GET /api/instances/smoke/backups; want "backup listed" 200 'smoke-'
BK=$(grep -o 'smoke-[0-9-]*\(-live\)\?\.tar\.gz' <<<"$BODY" | head -1)  # a backup taken while running ends in -live
CODE=$(curl -s -o "$J.tgz" -w '%{http_code}' -b "$J" "$B/api/instances/smoke/backups/$BK"); [[ $CODE == 200 ]] && tar -tzf "$J.tgz" | grep -q 'smoke/server.properties' && pass "backup downloads and contains the server" || bad "backup download ($CODE)"
req DELETE /api/instances/smoke/backups/..%2F..%2Fetc%2Fpasswd; want "backup name traversal refused" 400
req DELETE "/api/instances/smoke/backups/$BK"; want "delete backup" 200

echo "-- stop"
req POST /api/instances/smoke/action '{"action":"stop"}'; want "stop accepted" 200
for i in $(seq 1 60); do sleep 2; req GET /api/instances/smoke; grep -q '"state": "inactive"' <<<"$BODY" && break; done
grep -q '"state": "inactive"' <<<"$BODY" && pass "server stopped after ~$((i*2))s" || bad "server did not stop"

echo "-- mods through the UI backend (Modrinth)"
req GET "/api/instances/smoke/search?q=lithium"; want "search Modrinth" 200 '"source": "modrinth"'
grep -q '"url": "https://modrinth.com/project/' <<<"$BODY" && pass "results link back to the Modrinth page" || bad "no page url in results"
req GET "/api/instances/smoke/search?q=lithium&source=curseforge"; want "CurseForge without a project key is a clear error" 502 'not available in this build'
req POST /api/instances/smoke/mods '{"specs":["modrinth:ferrite-core"]}'; want "install job starts" 200
JOB=$(grep -o '"id": "[0-9a-f]*"' <<<"$BODY" | head -1 | cut -d'"' -f4)
for i in $(seq 1 30); do sleep 1; req GET /api/jobs/$JOB; grep -q '"state": "running"' <<<"$BODY" || break; done
grep -q '"state": "done"' <<<"$BODY" && pass "mod installed via job" || bad "install job" "$BODY"
req POST /api/instances/smoke/mods '{"specs":["x; rm -rf /"]}'; want "shell metacharacters in mod spec refused" 400

echo "-- remote control settings are the owner's (MCSrcon)"
RP=/opt/minecraft/instance/smoke/server.properties
su minecraft -s /bin/bash -c "sed -i '/^rcon.password=/d' $RP; echo 'rcon.password=s3cretvalue' >> $RP"
req GET /api/instances/smoke; want "instance details load" 200
grep -q 's3cretvalue' <<<"$BODY" && bad "RCON password reaches the web page" || pass "RCON password is hidden"
req PUT /api/instances/smoke/properties '{"values":{"enable-rcon":"true"}}'; want "web cannot switch RCON on" 400 'MCSrcon'
req PUT /api/instances/smoke/properties '{"values":{"rcon.password":"x"}}'; want "web cannot set the RCON password" 400
req PUT /api/instances/smoke/properties '{"values":{"management-server-enabled":"true"}}'; want "web cannot switch the management server on" 400
req PUT /api/instances/smoke/properties '{"values":{"motd":"a enable-rcon=true"}}'; want "a hidden line break in a value is refused" 400
su minecraft -s /bin/bash -c "sed -i 's/^rcon.password=.*/rcon.password=/' $RP"

echo "-- settings & create"
req PUT /api/settings '{"curseforge_key":"abcdefghij1234"}'; want "the web page cannot store a CurseForge key" 405
req POST /api/instances '{"name":"../x","agree":true}'; want "bad new-server name refused" 400
req POST /api/instances '{"name":"smoke","agree":true}'; want "duplicate name refused" 409
req POST /api/instances '{"name":"webmade","agree":true,"op_name":"Notch","port":25590}'; want "create server" 200
grep -q '^eula=true' /opt/minecraft/instance/webmade/eula.txt && pass "new server has eula=true" || bad "eula not set"
grep -q "^level-seed=$" /opt/minecraft/instance/webmade/server.properties && pass "no seed given: left blank (random world)" || bad "level-seed not blank by default"
W=/opt/minecraft/instance/webmade/server.properties
req POST /api/instances/webmade/jar '{"type":"fabric","mc":"1.21.11"}'; want "jar download job starts" 200
JOB=$(grep -o '"id": "[0-9a-f]*"' <<<"$BODY" | head -1 | cut -d'"' -f4)
for i in $(seq 1 120); do sleep 2; req GET /api/jobs/$JOB; grep -q '"state": "running"' <<<"$BODY" || break; done
grep -q '"state": "done"' <<<"$BODY" && pass "jar download job finished" || bad "jar job" "$BODY"
[[ $(grep -vc '^#' $W) -gt 50 ]] && grep -qx white-list=true $W && grep -qx server-port=25590 $W \
  && pass "the web download completed server.properties and kept MCS's choices" || bad "server.properties not completed: $(grep -vc '^#' $W) settings"
test ! -e /opt/minecraft/instance/webmade/webmade && pass "and made no world" || bad "a world was made"
rm -rf /opt/minecraft/instance/webmade
req POST /api/instances '{"name":"webseed","agree":true,"op_name":"Notch","port":25591,"seed":"diamond sword 42"}'; want "create server with a seed" 200
grep -q "^level-seed=diamond sword 42$" /opt/minecraft/instance/webseed/server.properties && pass "the seed is written to server.properties" || bad "seed missing: $(grep level-seed /opt/minecraft/instance/webseed/server.properties)"
rm -rf /opt/minecraft/instance/webseed
req POST /api/instances '{"name":"webseed2","agree":true,"op_name":"Notch","port":25592,"seed":"line\nbreak"}'; want "a seed with a line break is refused" 400
req POST /api/instances "{\"name\":\"webseed3\",\"agree\":true,\"op_name\":\"Notch\",\"port\":25593,\"seed\":\"$(printf 'x%.0s' $(seq 1 200))\"}"; want "an over-long seed is refused" 400
test ! -d /opt/minecraft/instance/webseed2 && test ! -d /opt/minecraft/instance/webseed3 && pass "nothing was created for the refused seeds" || bad "refused seed still created a folder"
sudo -n rm -rf /opt/minecraft/instance/webmade 2>/dev/null || rm -rf /opt/minecraft/instance/webmade

echo "-- font and footer"
H=$(curl -sI $B/fonts/ShantellSans.ttf); grep -qi "content-type: font/ttf" <<<"$H" && grep -qi "max-age=604800" <<<"$H" && pass "the font is served from this machine, cacheable" || bad "font not served right" "$H"
grep -q "not affiliated" /opt/mcs/lib/MCS/web/app.js && pass "the not-affiliated footer is in the page" || bad "footer missing"

echo; [[ $fail -eq 0 ]] && echo "ALL PASSED" || echo "SOME FAILED"
exit $fail
