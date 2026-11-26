#!/bin/bash
# The key broker, installed for real: who can reach it, who can read the owner's key, and
# what gets logged. Uses a made-up key, so CurseForge answers with an error; that proves
# the request went out. Needs the full install and network. REMOVES any key set with MCScred.
# Run as root:  sudo bash tests/wsl-cred.sh
set -u
fail=0
ok ()  { echo "PASS  $*"; }
bad () { echo "FAIL  $*"; fail=$((fail+1)); }
chk () { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
KEY="made-up-test-key-0123456789"
since="$(date '+%Y-%m-%d %H:%M:%S')"

MCScred remove curseforge >/dev/null
chk "project key file is root only" test "$(stat -c '%U:%G %a' /etc/MCS/cred/curseforge-project)" = "root:root 600"
chk "minecraft cannot read the project key" bash -c '! su minecraft -s /bin/bash -c "cat /etc/MCS/cred/curseforge-project" >/dev/null 2>&1'
chk "no key file in the program folder" test -z "$(find /opt/mcs -name 'curseforge*')"
chk "socket is root:minecraft 660" test "$(stat -c '%U:%G %a' /run/MCScred.sock)" = "root:minecraft 660"
chk "other users cannot use it" bash -c '! su nobody -s /bin/bash -c "python3 -I -c \"import socket; socket.socket(socket.AF_UNIX).connect(\\\"/run/MCScred.sock\\\")\"" 2>/dev/null'
chk "no key: status says not available" bash -c "su minecraft -s /bin/bash -c 'MCScred status' | grep -q 'not available'"

chk "garbage key refused" bash -c '! echo "no spaces allowed" | MCScred set curseforge >/dev/null 2>&1'
chk "owner can set a key" bash -c "echo '$KEY' | MCScred set curseforge >/dev/null"
chk "key file is root only" test "$(stat -c '%U:%G %a' /etc/MCS/cred/curseforge)" = "root:root 600"
chk "status says available" bash -c "MCScred status | grep -q 'CurseForge is available'"
pid="$(systemctl show -p MainPID --value MCScred)"
chk "the broker is running" test "$pid" -gt 0
user="$(ps -o user= -p "$pid" | tr -d ' ')"
chk "as a user that is neither root nor minecraft (uid $(ps -o uid= -p "$pid" | tr -d ' '))" test "$user" != root -a "$user" != minecraft
chk "minecraft cannot read the key file" bash -c '! su minecraft -s /bin/bash -c "cat /etc/MCS/cred/curseforge" >/dev/null 2>&1'
chk "minecraft cannot read systemd's copy" bash -c '! su minecraft -s /bin/bash -c "cat /run/credentials/MCScred.service/keys_curseforge" >/dev/null 2>&1'
chk "minecraft cannot read the broker's memory or environment" bash -c "! su minecraft -s /bin/bash -c 'cat /proc/$pid/environ' >/dev/null 2>&1"

# a real request through MCSfetch's own path, as the minecraft user
as_mc "python3 -I -B -c 'import sys; sys.path.insert(0, \"/opt/mcs/lib/MCS\"); import mcsfetch as m; m.api_get(\"curseforge\", \"/mods/238222\")'" >/tmp/cred-req.out 2>&1
chk "CurseForge rejects the made-up key (so the request went out)" grep -Eq 'HTTP (401|403)' /tmp/cred-req.out
chk "the caller never sees the key" bash -c "! grep -qF '$KEY' /tmp/cred-req.out"
chk "audit shows the route and the caller" bash -c "MCScred audit -n 3 | grep -q '/mods/238222'"
chk "audit names a unit from the kernel" bash -c "MCScred audit -n 1 | grep -Eq '\.(service|scope) '"
chk "the key is not in the journal" bash -c "! journalctl --since '$since' -o cat --no-pager | grep -qF '$KEY'"

chk "owner can remove it" bash -c "MCScred remove curseforge >/dev/null"
chk "and CurseForge is off again" bash -c "MCScred status | grep -q 'not available'"

echo
echo "failures: $fail"; exit $fail
