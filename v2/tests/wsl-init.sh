#!/bin/bash
# server.properties: MCS writes its own choices, then the server jar completes the file before
# the world is made. Needs the full install, Java 21 and network. Creates and removes its own
# servers (init1, init2).
# Run as root:  sudo bash tests/wsl-init.sh
set -u
fail=0
ok ()  { echo "PASS  $*"; }
bad () { echo "FAIL  $*"; fail=$((fail+1)); }
chk () { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
I=/opt/minecraft/instance
clean () { rm -rf $I/init1 $I/init2; }
trap clean EXIT
clean
create () { printf '%s\n\nn\nn\n%s\nNotch\nn\n' "$1" "$2" | timeout 90 su minecraft -s /bin/bash -c /opt/mcs/bin/MCScreate >/tmp/$1.out 2>&1; }
has () { grep -qx "$2" "$I/$1/server.properties"; }
count () { grep -vc '^#' "$I/$1/server.properties"; }

echo "-- before there is a jar: only MCS's choices"
create init1 25591
chk "short file" test "$(count init1)" -lt 20
chk "whitelist on" has init1 white-list=true

echo "-- downloading the jar completes the file"
as_mc "MCSFETCH=1 /opt/mcs/bin/MCSfetch jar init1 --type fabric --mc 1.21.11" >/tmp/init1.jar.out 2>&1
chk "jar downloaded" grep -q "saved" /tmp/init1.jar.out
chk "the jar filled in the rest" test "$(count init1)" -gt 50
for line in white-list=true enforce-whitelist=true accepts-transfers=false difficulty=normal view-distance=12 \
            simulation-distance=6 spawn-protection=1 server-port=25591 level-name=init1; do
  chk "kept: $line" has init1 "$line"
done
chk "the game's own default for chunk writes" has init1 sync-chunk-writes=true
chk "no dead settings" bash -c "! grep -qE '^(pvp|spawn-animals|spawn-npcs|allow-nether)=' $I/init1/server.properties"
chk "no world made" test ! -e $I/init1/init1
chk "RCON still off" has init1 enable-rcon=false
chk "files stay in the minecraft group" test "$(stat -c '%U:%G' $I/init1/server.properties)" = "minecraft:minecraft"

echo "-- never on a server with a world, or a version that would just start"
mkdir -p $I/init1/init1; before="$(md5sum $I/init1/server.properties)"
as_mc "/opt/mcs/bin/MCSfetch settings init1" >/tmp/init1.again.out 2>&1
chk "existing world: left alone" grep -q "world already exists" /tmp/init1.again.out
chk "and the file is unchanged" test "$before" = "$(md5sum $I/init1/server.properties)"
create init2 25592
as_mc "/opt/mcs/bin/MCSfetch jar init2 --type vanilla --mc 1.12.2" >/tmp/init2.out 2>&1
chk "1.12.2: not asked" test "$(count init2)" -lt 20
chk "1.12.2: no world made" test ! -e $I/init2/init2

echo "-- the sandbox"
u=/etc/systemd/system/MCSinit@.service
for l in User=minecraft ProtectSystem=strict NoNewPrivileges=true 'ReadWritePaths=/opt/minecraft/instance/%i'; do
  chk "unit has $l" grep -qx "$l" $u
done
chk "polkit does not allow other units" bash -c '! su minecraft -s /bin/bash -c "systemctl start MCSbackup.service" >/dev/null 2>&1'

echo
echo "failures: $fail"; exit $fail
