#!/bin/bash
# Safe-defaults checks: whitelist on, first operator, per-server ports, PATH shims,
# online-mode warning. Needs the full install (wsl-full.sh install) and network.
# Run as root:  sudo bash tests/wsl-safe-defaults.sh
set -u
fail=0
ok ()  { echo "PASS  $*"; }
bad () { echo "FAIL  $*"; fail=$((fail+1)); }
chk () { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
I=/opt/minecraft/instance
as_mc "rm -rf $I/sd1 $I/sd2 $I/sd3 $I/t"

# MCScreate (EULA already accepted): name, Enter(import), n n (edit prompts), port, op name, n (browse)
printf 'sd1\n\nn\nn\n\nNotch\nn\n' | timeout 90 su minecraft -s /bin/bash -c "~/toolbox/MCScreate" >/tmp/sd1.out 2>&1
chk "MCScreate completes" grep -q "ready" /tmp/sd1.out
chk "whitelist on by default" grep -q '^white-list=true' $I/sd1/server.properties
chk "enforce-whitelist on" grep -q '^enforce-whitelist=true' $I/sd1/server.properties
chk "next free port suggested (25565 is taken by smoke)" grep -q '^server-port=25566' $I/sd1/server.properties
chk "whitelist.json has Notch" grep -q '"name": "Notch"' $I/sd1/whitelist.json
chk "ops.json has Notch at level 4" grep -q '"level": 4' $I/sd1/ops.json
chk "whitelist uuid is dashed" grep -Eq '"uuid": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' $I/sd1/whitelist.json

# second server must be offered the next free port, and a duplicate must be refused
printf 'sd2\n\nn\nn\n25565\n25577\nNotch\nn\n' | timeout 90 su minecraft -s /bin/bash -c "~/toolbox/MCScreate" >/tmp/sd2.out 2>&1
chk "duplicate port refused" grep -q "already used by" /tmp/sd2.out
chk "second server took the replacement port" grep -q '^server-port=25577' $I/sd2/server.properties

# a name Mojang does not have is rejected, then a good one accepted
printf 'sd3\n\nn\nn\n\nxx\nNotch\nn\n' | timeout 90 su minecraft -s /bin/bash -c "~/toolbox/MCScreate" >/tmp/sd3.out 2>&1
chk "bad player name rejected" grep -q "3-16 letters" /tmp/sd3.out
chk "good name then accepted" grep -q '"name": "Notch"' $I/sd3/whitelist.json

# idempotent: adding the same player twice does not duplicate
as_mc "~/toolbox/MCSfetch player sd1 Notch --op" >/dev/null 2>&1
chk "no duplicate whitelist entry" test "$(grep -c '"uuid"' $I/sd1/whitelist.json)" = 1

# online-mode=false: offline UUID + warning
as_mc "sed -i 's/^online-mode=.*/online-mode=false/' $I/sd1/server.properties; ~/toolbox/MCSfetch player sd1 Steve" >/tmp/off.out 2>&1
chk "online-mode=false warns" grep -q "online-mode=false" /tmp/off.out
chk "offline uuid is a v3 uuid" grep -Eq '"uuid": "[0-9a-f]{8}-[0-9a-f]{4}-3[0-9a-f]{3}-[89ab][0-9a-f]{3}-' $I/sd1/whitelist.json
python3 - <<'EOF'
import hashlib, sys
b = bytearray(hashlib.md5(b"OfflinePlayer:Steve").digest()); b[6] = b[6] & 0x0F | 0x30; b[8] = b[8] & 0x3F | 0x80
h = b.hex(); print("PASS  offline uuid matches the known algorithm" if f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" in open("/opt/minecraft/instance/sd1/whitelist.json").read() else "FAIL  offline uuid")
EOF

# PATH: minecraft sees the toolbox; the normal user gets a root-owned shim
chk "minecraft login shell finds MCScreate" su - minecraft -c 'command -v MCScreate' >/dev/null
chk "shim is root owned" test "$(stat -c %U /usr/local/bin/MCScreate)" = root
chk "shim runs the tool as minecraft" grep -q "sudo -u minecraft" /usr/local/bin/MCScreate

as_mc "rm -rf $I/sd1 $I/sd2 $I/sd3"
echo; echo "failures: $fail"; exit $fail
