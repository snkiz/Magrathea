#!/bin/bash
# Off-box copy checks. Needs the full install (tests/wsl-full.sh install, which installs MCSoffbox) and MCSweb running.
# Run as root:  sudo bash tests/wsl-offbox.sh
# WSL has one disk, so the destination here is on the SAME disk as the servers: the test checks that
# MCSoffbox refuses that unless told otherwise, then uses --same-disk-ok for the rest.
set -u
B=http://localhost:8080
J="$(mktemp)"
SRC=/opt/minecraft/backups/instance
DEST=/root/ob-dest
LIBDIR=/opt/minecraft/.local/lib/MCS
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
cleanup () {
  MCSoffbox off >/dev/null 2>&1; /usr/local/bin/MCSkey revoke "ob$$" >/dev/null 2>&1
  rm -rf "$DEST" /tmp/ob-w /tmp/ob-mc "$J" "$J".*; as_mc "rm -f $SRC/ob1-*"
}
trap cleanup EXIT
MCSoffbox () { /usr/local/bin/MCSoffbox "$@"; }
req () { CODE=$(curl -s -o "$J.body" -w '%{http_code}' -b "$J" -c "$J" -H 'X-MCS: 1' "$B$1"); BODY=$(cat "$J.body"); }

cleanup
mkdir -p "$DEST"; chmod 700 "$DEST"
as_mc "head -c 300000 /dev/urandom > $SRC/ob1-20260101-010101.tar.gz; head -c 310000 /dev/urandom > $SRC/ob1-20260102-010101.tar.gz; head -c 50000 /dev/urandom > $SRC/ob1-20260103-010101-prerestore.tar.gz; head -c 50000 /dev/urandom > $SRC/ob1-20260104-010101-live.tar.gz; ln -s /etc/passwd $SRC/ob1-20260105-010101.tar.gz; echo hi > $SRC/ob1-notes.txt"

echo "-- destinations that must be refused"
MCSoffbox set relative/path >/tmp/ob.out 2>&1 && bad "relative path accepted" || pass "relative path refused"
MCSoffbox set /root/does-not-exist >/tmp/ob.out 2>&1; grep -q "not a folder" /tmp/ob.out && pass "missing folder refused, with a hint about plugging the drive in" || bad "missing folder" "$(cat /tmp/ob.out)"
MCSoffbox set /opt/minecraft/backups >/tmp/ob.out 2>&1 && bad "a folder under the minecraft user accepted" || pass "a folder the minecraft user owns is refused"
mkdir -p /tmp/ob-w; chmod 777 /tmp/ob-w
MCSoffbox set /tmp/ob-w >/tmp/ob.out 2>&1 && bad "world-writable folder accepted" || pass "a world-writable folder is refused"
mkdir -p /tmp/ob-mc; chown minecraft:minecraft /tmp/ob-mc
MCSoffbox set /tmp/ob-mc >/tmp/ob.out 2>&1 && bad "minecraft-owned folder accepted" || pass "a folder owned by the minecraft user is refused"
MCSoffbox set "$DEST" >/tmp/ob.out 2>&1; grep -q "SAME disk" /tmp/ob.out && pass "same disk as the servers is refused without --same-disk-ok" || bad "same-disk not refused" "$(cat /tmp/ob.out)"
test ! -e /etc/MCS/offbox.json && pass "nothing was set up by the refusals" || bad "conf written by a refusal"

echo "-- set up and first copy"
MCSoffbox set "$DEST" --same-disk-ok >/tmp/ob.out 2>&1; grep -q "WARNING: same disk" /tmp/ob.out && pass "same-disk trial works, with a warning" || bad "set failed" "$(cat /tmp/ob.out)"
ls -1 "$DEST" | grep ^ob1- | tr '\n' ' ' | grep -q '^ob1-20260101-010101.tar.gz ob1-20260102-010101.tar.gz ob1-20260103-010101-prerestore.tar.gz $' && pass "clean and pre-restore backups copied, nothing else" || bad "wrong files copied: $(ls -1 $DEST | grep ^ob1- | tr '\n' ' ')"
ls "$DEST" | grep -q -- '-live' && bad "a quick (live) copy was copied" || pass "quick copies are not copied"
[[ "$(sha256sum < $SRC/ob1-20260101-010101.tar.gz)" == "$(sha256sum < $DEST/ob1-20260101-010101.tar.gz)" ]] && pass "copy is byte for byte the same" || bad "copy differs"
test "$(stat -c '%U %a' $DEST/ob1-20260101-010101.tar.gz)" = "root 600" && pass "copies are root-owned and private" || bad "mode: $(stat -c '%U %a' $DEST/ob1-20260101-010101.tar.gz)"
grep -q "passwd" <(cat "$DEST"/* 2>/dev/null | head -c 2000) && bad "a symlink to /etc/passwd was followed" || pass "a link in the backup folder is not followed"
systemctl is-enabled --quiet MCSoffbox.timer && pass "hourly timer enabled" || bad "timer not enabled"

echo "-- the minecraft user cannot touch the copy or its settings"
as_mc "rm -f $DEST/ob1-20260101-010101.tar.gz" >/dev/null 2>&1; test -f $DEST/ob1-20260101-010101.tar.gz && pass "minecraft cannot delete a copy" || bad "copy deleted by minecraft"
as_mc "echo '{}' > /etc/MCS/offbox.json" >/dev/null 2>&1; grep -q '"dest"' /etc/MCS/offbox.json && pass "minecraft cannot change the settings" || bad "settings overwritten"
as_mc "cat /etc/MCS/offbox-status.json" >/dev/null 2>&1 && pass "minecraft can read the status (for the web page)" || bad "status unreadable"
as_mc "sudo -n /usr/local/bin/MCSoffbox run" >/dev/null 2>&1 && bad "the minecraft user can run MCSoffbox as root" || pass "the minecraft user cannot run MCSoffbox (it needs root)"

echo "-- it never deletes or overwrites"
OUT=$(MCSoffbox run 2>&1); grep -q copied <<<"$OUT" && bad "second run copied again" "$OUT" || pass "a second run copies nothing"
as_mc "rm -f $SRC/ob1-20260101-010101.tar.gz"; MCSoffbox run >/dev/null 2>&1
test -f $DEST/ob1-20260101-010101.tar.gz && pass "deleting a backup at the source does not delete the separate copy" || bad "dest copy removed"
echo "different" > $DEST/ob1-20260102-010101.tar.gz; MCSoffbox run >/dev/null 2>&1
grep -q different $DEST/ob1-20260102-010101.tar.gz && pass "an existing copy is never overwritten" || bad "overwrote an existing copy"

echo "-- a new backup is picked up by the timer's service"
as_mc "head -c 100000 /dev/urandom > $SRC/ob1-20260107-010101.tar.gz"
systemctl start MCSoffbox.service; sleep 1
test -f $DEST/ob1-20260107-010101.tar.gz && pass "the systemd service copied the new backup" || bad "service did not copy" "$(journalctl -u MCSoffbox -n 5 --no-pager -o cat)"

echo "-- an unplugged drive does not fill the main disk"
python3 - <<'EOF'
import json
p = "/etc/MCS/offbox.json"; c = json.load(open(p)); c["separate"] = True; json.dump(c, open(p, "w"))
EOF
as_mc "head -c 100000 /dev/urandom > $SRC/ob1-20260108-010101.tar.gz"
MCSoffbox run >/tmp/ob.out 2>&1; rc=$?
[[ $rc != 0 ]] && grep -q "unplugged" /tmp/ob.out && pass "a destination that turned into the main disk pauses copying" || bad "no pause" "$(cat /tmp/ob.out)"
test ! -e $DEST/ob1-20260108-010101.tar.gz && pass "nothing was copied while paused" || bad "copied while paused"
python3 - <<'EOF'
import json
p = "/etc/MCS/offbox.json"; c = json.load(open(p)); c["separate"] = False; json.dump(c, open(p, "w"))
EOF
MCSoffbox run >/dev/null 2>&1; test -f $DEST/ob1-20260108-010101.tar.gz && pass "copying resumes when it is fine again" || bad "did not resume"

echo "-- notices and the web page"
as_mc "python3 -I - $LIBDIR" <<'EOF'
import sys, json, time
sys.path.insert(0, sys.argv[1])
import mcsbackup as mb
def t(d, ok): print(("PASS  " if ok else "FAIL  ") + d)
ids = lambda: {n["id"]: n for n in mb.offbox_notices()}
n = ids()
t("same-disk copy gets a warning", "offbox:samedisk" in n and n["offbox:samedisk"]["level"] == "warn")
t("a working copy has no 'set it up' nudge", "offbox:none" not in n)
t("status reports the files copied", mb.offbox_status()["configured"] and mb.offbox_status()["copied_files"] >= 3)
EOF
python3 - <<'EOF'
import json, time
p = "/etc/MCS/offbox-status.json"; s = json.load(open(p)); s["last_ok"] = int(time.time()) - 5 * 86400; json.dump(s, open(p, "w"))
EOF
as_mc "python3 -I -c \"import sys; sys.path.insert(0,'$LIBDIR'); import mcsbackup as mb; print('PASS  a copy that stopped working days ago is a red notice' if any(n['id']=='offbox:stale' and n['level']=='bad' for n in mb.offbox_notices()) else 'FAIL  stale not reported')\""
KEY="$(/usr/local/bin/MCSkey add "ob$$" --quiet)"
curl -s -o /dev/null -b "$J" -c "$J" -H 'X-MCS: 1' -H 'Content-Type: application/json' --data "{\"key\":\"$KEY\"}" $B/api/login
req /api/offbox; [[ $CODE == 200 ]] && grep -q '"configured": true' <<<"$BODY" && pass "web API reports the copy" || bad "/api/offbox" "$BODY"

echo "-- switching it off leaves the copies alone, and the nudge returns"
MCSoffbox off >/dev/null 2>&1
test ! -e /etc/MCS/offbox.json && ! systemctl is-enabled --quiet MCSoffbox.timer && pass "off removes the settings and the timer" || bad "off incomplete"
test -f $DEST/ob1-20260107-010101.tar.gz && pass "copies already made are left alone" || bad "off deleted copies"
req /api/offbox; grep -q '"configured": false' <<<"$BODY" && grep -q 'MCSoffbox set' <<<"$BODY" && pass "with nothing set up the page gets the step-by-step help" || bad "no howto" "$BODY"

echo; [[ $fail == 0 ]] && echo "all passed" || echo "FAILURES"; exit $fail
