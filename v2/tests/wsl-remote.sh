#!/bin/bash
# Remote control (RCON, management server): the owner's switch, a real server start, and the
# web page refusing to touch it. Needs the full install and a "smoke" server with a jar
# (wsl-full.sh install, create, fetch). Starts and stops that server.
# Run as root:  sudo bash tests/wsl-remote.sh
set -u
fail=0
ok ()  { echo "PASS  $*"; }
bad () { echo "FAIL  $*"; fail=$((fail+1)); }
chk () { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
P=/opt/minecraft/instance/smoke/server.properties
prop () { grep "^$1=" "$P" | tail -1 | cut -d= -f2-; }
listening () { ss -Hltn "sport = :$1" | grep -q .; }
start () { since="$(date '+%Y-%m-%d %H:%M:%S')"; systemctl start MCS@smoke; }
log () { journalctl -u MCS@smoke --since "$since" -o cat --no-pager; }
set_prop () { as_mc "sed -i '/^$1=/d' $P; echo '$1=$2' >> $P"; }

systemctl stop MCS@smoke 2>/dev/null
rm -f /etc/MCS/remote.json

echo "-- switched on by hand, not allowed"
set_prop enable-rcon true
set_prop rcon.password hunter2
set_prop management-server-enabled true
start
chk "server starts" systemctl is-active --quiet MCS@smoke
chk "RCON switched off" test "$(prop enable-rcon)" = false
chk "password cleared" test -z "$(prop rcon.password)"
chk "management server switched off" test "$(prop management-server-enabled)" = false
chk "nothing listens on the RCON port" bash -c '! ss -Hltn "sport = :25575" | grep -q .'
chk "the log says why" bash -c "journalctl -u MCS@smoke --since '$since' -o cat | grep -q 'switched RCON off'"
systemctl stop MCS@smoke

echo "-- only the owner can allow it"
chk "minecraft user cannot write the permission folder" bash -c '! su minecraft -s /bin/bash -c "touch /etc/MCS/remote.json" 2>/dev/null'
chk "enable works with sudo/root" MCSrcon enable smoke >/tmp/rcon-enable.out
chk "enable warns it is unencrypted" grep -q UNENCRYPTED /tmp/rcon-enable.out
chk "permission file is root:minecraft 640" test "$(stat -c '%U:%G %a' /etc/MCS/remote.json)" = "root:minecraft 640"
chk "unknown server refused" bash -c '! MCSrcon enable no-such-server >/dev/null 2>&1'
chk "list shows it" bash -c 'MCSrcon list | grep -q "^smoke: rcon"'
start
chk "server starts with RCON allowed" systemctl is-active --quiet MCS@smoke
chk "RCON switched on" test "$(prop enable-rcon)" = true
chk "RCON port is listening" listening 25575
chk "the log warns RCON is ON" bash -c "journalctl -u MCS@smoke --since '$since' -o cat | grep -q 'RCON is ON'"
pw="$(prop rcon.password)"
chk "a password was generated" test "${#pw}" -ge 20
chk "show prints it" bash -c "MCSrcon show smoke | grep -qF 'password: $pw'"
chk "the password is not in the log" bash -c "! journalctl -u MCS@smoke --since '$since' -o cat | grep -qF '$pw'"
systemctl stop MCS@smoke

echo "-- off again"
chk "disable works" MCSrcon disable smoke >/dev/null
start
chk "RCON off after disable" test "$(prop enable-rcon)" = false
chk "password cleared after disable" test -z "$(prop rcon.password)"
chk "port closed" bash -c '! ss -Hltn "sport = :25575" | grep -q .'
systemctl stop MCS@smoke

echo
echo "failures: $fail"; exit $fail
