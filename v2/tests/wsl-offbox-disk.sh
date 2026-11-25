#!/bin/bash
# Off-box copy on a REAL second filesystem: a loop-mounted disk image, so MCSoffbox sees a different device,
# and pulling the "drive" out (umount) leaves an empty folder on the main disk, like an unplugged USB drive.
# Run as root:  sudo bash tests/wsl-offbox-disk.sh      (skips itself where loop mounts are not possible)
set -u
SRC=/opt/minecraft/backups/instance
IMG=/root/mcs-offbox-test.img
MNT=/mnt/mcs-offbox-test
DEST=$MNT/backups
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
cleanup () {
  /usr/local/bin/MCSoffbox off >/dev/null 2>&1
  mountpoint -q $MNT && umount $MNT 2>/dev/null; rm -rf $MNT $IMG; as_mc "rm -f $SRC/od1-*"
}
trap cleanup EXIT

command -v mkfs.ext4 >/dev/null && command -v mount >/dev/null || { echo "SKIP  mkfs.ext4 is not installed"; exit 0; }
rm -rf $MNT; mkdir -p $MNT
truncate -s 300M $IMG && mkfs.ext4 -q -F $IMG || { echo "SKIP  cannot make a filesystem image here"; exit 0; }
mount -o loop $IMG $MNT 2>/dev/null || { echo "SKIP  loop mounts are not available here"; exit 0; }
mkdir -p $DEST; chmod 755 $DEST   # root-owned, like a drive prepared by the owner
as_mc "head -c 400000 /dev/urandom > $SRC/od1-20260301-010101.tar.gz; head -c 300000 /dev/urandom > $SRC/od1-20260302-010101.tar.gz"

echo "-- a real second disk"
[[ "$(stat -c %d $DEST)" != "$(stat -c %d $SRC)" ]] && pass "the test drive really is a different device" || bad "same device"
/usr/local/bin/MCSoffbox set $DEST >/tmp/od.out 2>&1 && pass "set works WITHOUT --same-disk-ok on another disk" || bad "set refused a real second disk" "$(cat /tmp/od.out)"
grep -q "WARNING" /tmp/od.out && bad "a separate disk got the same-disk warning" || pass "no same-disk warning"
ls $DEST | grep -c '^od1-' | grep -q '^2$' && pass "both backups copied to the second disk" || bad "copy missing: $(ls $DEST)"
python3 -c "import json; s=json.load(open('/etc/MCS/offbox-status.json')); print('same_disk', s.get('same_disk'))" | grep -q "same_disk False" && pass "status says it is NOT on the same disk" || bad "status same_disk wrong"
python3 -c "import json; c=json.load(open('/etc/MCS/offbox.json')); print(c.get('separate'))" | grep -q True && pass "recorded as a separate disk (for the unplug check)" || bad "separate flag not set"

echo "-- the drive is pulled out"
umount $MNT
as_mc "head -c 100000 /dev/urandom > $SRC/od1-20260303-010101.tar.gz"
mkdir -p $DEST    # what is left behind: an empty folder on the main disk
/usr/local/bin/MCSoffbox run >/tmp/od.out 2>&1; rc=$?
[[ $rc != 0 ]] && grep -q "unplugged" /tmp/od.out && pass "copying pauses with a plain explanation" || bad "no pause" "$(cat /tmp/od.out)"
ls $DEST | grep -q '^od1-' && bad "backups were copied onto the MAIN disk" || pass "nothing was copied onto the main disk"
python3 -c "import json; s=json.load(open('/etc/MCS/offbox-status.json')); print(bool(s.get('error')))" | grep -q True && pass "the error is recorded for the web page" || bad "no error recorded"
rm -rf $DEST

echo "-- the drive is plugged back in"
mount -o loop $IMG $MNT
/usr/local/bin/MCSoffbox run >/tmp/od.out 2>&1 && pass "copying resumes" || bad "did not resume" "$(cat /tmp/od.out)"
ls $DEST | grep -q '^od1-20260303' && pass "the backup made while it was unplugged is copied now" || bad "missed backup not copied"
ls $DEST | grep -c '^od1-' | grep -q '^3$' && pass "nothing duplicated or lost" || bad "count: $(ls $DEST)"

echo; [[ $fail == 0 ]] && echo "all passed" || echo "FAILURES"; exit $fail
