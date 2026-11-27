#!/bin/bash
# Backup and restore checks. Needs the full install (tests/wsl-full.sh install) and the
# MCSweb service running. Run as root:  sudo bash tests/wsl-backup.sh
# Uses a throwaway server 'bk1' (stopped, fake world) for the logic, and, if the 'smoke'
# server can start, one real safe backup that stops and restarts it (about 2 minutes).
set -u
B=http://localhost:8080
J="$(mktemp)"; trap 'rm -f "$J" "$J".*; /usr/local/bin/MCSkey revoke "bk$$" >/dev/null 2>&1; as_mc "rm -rf $I/bk1 $I/bk2 $I/bk3 $BK/bk1-* $BK/bk2-* $BK/bk3-*"; rm -f /opt/minecraft/.config/MCS/backup/bk[123].json' EXIT
LIBDIR=/opt/mcs/lib/MCS
I=/opt/minecraft/instance; BK=/opt/minecraft/backups/instance
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "        $2"; fail=1; }
as_mc () { su minecraft -s /bin/bash -c "$1"; }
mcb () { as_mc "/opt/mcs/bin/MCSbackup $*"; }
req () { local m="$1" p="$2" d="${3:-}"; local args=(-s -o "$J.body" -w '%{http_code}' -b "$J" -c "$J" -X "$m" -H 'X-MCS: 1' "$B$p"); [[ -n "$d" ]] && args+=(-H 'Content-Type: application/json' --data "$d"); CODE=$(curl "${args[@]}"); BODY=$(cat "$J.body"); }
want () { if [[ "$CODE" == "$2" ]] && { [[ -z "${3:-}" ]] || grep -q -- "$3" <<<"$BODY"; }; then pass "$1"; else bad "$1 (got $CODE, wanted $2 ${3:-})" "$BODY"; fi; }
waitjob () { local id; id=$(grep -o '"id": "[0-9a-f]*"' <<<"$BODY" | grep -o '[0-9a-f]\{12\}'); for _ in $(seq 1 150); do sleep 2; req GET /api/jobs/$id; grep -q '"state": "running"' <<<"$BODY" || break; done; }

as_mc "rm -rf $I/bk1 $BK/bk1-*"
as_mc "mkdir -p $I/bk1/world/region $I/bk1/mods && echo 'diamonds=64' > $I/bk1/world/level.dat && head -c 200000 /dev/urandom > $I/bk1/world/region/r.0.0.mca && echo 'eula=true' > $I/bk1/eula.txt && printf 'SERVER=NULL\n' > $I/bk1/service.conf && mkdir -p $I/bk1/libraries && echo big > $I/bk1/libraries/x.jar"

echo "-- schedule logic"
python3 -I - <<EOF
import sys, datetime as dt
sys.path.insert(0, "$LIBDIR")
import mcsbackup as mb
cfg = dict(mb.DEFAULTS)  # 04:00 daily, enabled
D = lambda s: dt.datetime.fromisoformat(s)
def t(desc, ok):
    print(("PASS  " if ok else "FAIL  ") + desc)
t("due at 04:05", mb.is_due(cfg, {}, D("2026-03-10T04:05:00"))[0])
t("not due at 03:55 (yesterday's slot already handled)", not mb.is_due(cfg, {"last_sched": D("2026-03-09T04:00:00").isoformat()}, D("2026-03-10T03:55:00"))[0])
t("same slot never runs twice", not mb.is_due(cfg, {"last_sched": D("2026-03-10T04:00:00").isoformat()}, D("2026-03-10T04:20:00"))[0])
t("missed after the grace period, not run at 15:00", mb.is_due(cfg, {}, D("2026-03-10T15:00:00")) == (False, "missed"))
t("off means off", mb.is_due(dict(cfg, enabled=False), {}, D("2026-03-10T04:05:00")) == (False, "off"))
t("weekday filter: Tue 04:05 is not due when only Mon+Wed are chosen", not mb.is_due(dict(cfg, days=[0, 2]), {"last_sched": D("2026-03-09T04:00:00").isoformat()}, D("2026-03-10T04:05:00"))[0])
t("next backup after 04:05 is tomorrow", mb.next_scheduled(cfg, D("2026-03-10T04:05:00")) == D("2026-03-11T04:00:00"))
t("next skips unchosen days", mb.next_scheduled(dict(cfg, days=[4]), D("2026-03-10T04:05:00")) == D("2026-03-13T04:00:00"))
for bad_cfg in ({"time": "25:00"}, {"time": "4am"}, {"days": [7]}, {"days": "all"}, {"keep": 0}, {"keep": True}, {"keep_weekly": -1}, {"keep_weekly": 99}, {"warn_minutes": 99}, {"enabled": "yes"}):
    try:
        mb.validate_config(bad_cfg); t(f"bad schedule refused {bad_cfg}", False)
    except mb.BackupError:
        t(f"bad schedule refused {bad_cfg}", True)
t("defaults are 7 daily + 4 weekly", mb.DEFAULTS["keep"] == 7 and mb.DEFAULTS["keep_weekly"] == 4)
t("weekly can be switched off (0)", mb.validate_config({"keep_weekly": 0}) == {"keep_weekly": 0})
base = dt.date(2025, 2, 9)   # 40 daily backups, newest Sunday 9 Feb 2025
names = [f"x-{(base - dt.timedelta(days=i)).strftime('%Y%m%d')}-040000.tar.gz" for i in range(40)]
days = lambda ch: sorted(n[2:10] for n in ch)
t("7 daily + 4 weekly keeps 11: the last 7 days, then the newest of each of 4 older weeks",
  days(mb.choose_keep(names, 7, 4)) == ["20250112", "20250119", "20250126", "20250202"] + [f"202502{d:02d}" for d in range(3, 10)])
t("weekly 0 keeps just the newest 7", len(mb.choose_keep(names, 7, 0)) == 7)
t("a week already covered by the daily ones is not counted again",
  days(mb.choose_keep(names, 10, 4)) == ["20250105", "20250112", "20250119", "20250126"] + ["20250131"] + [f"202502{d:02d}" for d in range(1, 10)])
t("fewer backups than the limit: all kept", len(mb.choose_keep(names[:5], 7, 4)) == 5)
EOF

echo "-- a safe backup of a stopped server, checked"
mcb now bk1 >/tmp/bk-now.out 2>&1; grep -q "checked" /tmp/bk-now.out && pass "backup made and checked" || bad "backup failed" "$(cat /tmp/bk-now.out)"
F=$(ls -1 $BK | grep '^bk1-.*\.tar\.gz$' | head -1)
[[ "$F" =~ ^bk1-[0-9]{8}-[0-9]{6}\.tar\.gz$ ]] && pass "clean backup has the plain name ($F)" || bad "unexpected name: $F"
tar -tzf $BK/$F | grep -q 'bk1/world/level.dat' && pass "world is inside" || bad "level.dat missing"
tar -tzf $BK/$F | grep -q 'libraries' && bad "libraries should be left out" || pass "libraries left out (they re-download)"
mcb verify bk1 $F >/tmp/bk-v.out 2>&1 && pass "verify reads it back" || bad "verify failed" "$(cat /tmp/bk-v.out)"
sleep 1.2; mcb now bk1 >/dev/null 2>&1; test "$(ls $BK | grep -c '^bk1-')" = 2 && pass "second backup coexists" || bad "expected 2 backups"
sleep 1

echo "-- damaged and hostile archives are refused"
cp $BK/$F $BK/bk1-20200101-000000.tar.gz; truncate -s 3000 $BK/bk1-20200101-000000.tar.gz; chown minecraft:minecraft $BK/bk1-20200101-000000.tar.gz
mcb verify bk1 bk1-20200101-000000.tar.gz >/dev/null 2>&1 && bad "truncated archive passed verify" || pass "truncated archive refused"
python3 -I - <<EOF2
import tarfile, io
with tarfile.open("$BK/bk1-20200102-000000.tar.gz", "w:gz") as tf:
    for name in ("bk1/ok.txt", "bk1/../../evil.txt"):
        ti = tarfile.TarInfo(name); data = b"x"; ti.size = 1; tf.addfile(ti, io.BytesIO(data))
EOF2
chown minecraft:minecraft $BK/bk1-20200102-000000.tar.gz
mcb verify bk1 bk1-20200102-000000.tar.gz >/dev/null 2>&1 && bad "foreign path passed verify" || pass "archive with paths outside the server folder refused"
rm -f $BK/bk1-2020*

echo "-- restore, and undo the restore"
as_mc "echo 'diamonds=0' > $I/bk1/world/level.dat; echo new > $I/bk1/world/newfile"
mcb restore bk1 $F >/tmp/bk-r.out 2>&1 && pass "restore ran" || bad "restore failed" "$(cat /tmp/bk-r.out)"
grep -q 'diamonds=64' $I/bk1/world/level.dat && pass "world is back as it was" || bad "level.dat not restored: $(cat $I/bk1/world/level.dat)"
test ! -e $I/bk1/world/newfile && pass "files added later are gone (a true rewind)" || bad "newfile survived"
PRE=$(ls -1 $BK | grep 'prerestore' | head -1)
[[ -n "$PRE" ]] && pass "current state saved first ($PRE)" || bad "no prerestore backup"
mcb restore bk1 "$PRE" >/dev/null 2>&1; grep -q 'diamonds=0' $I/bk1/world/level.dat && test -e $I/bk1/world/newfile && pass "restoring the prerestore copy undoes the restore" || bad "undo failed"
test "$(stat -c %U $I/bk1/world/level.dat)" = minecraft && pass "restored files belong to minecraft" || bad "wrong owner"
mcb restore bk1 ../../etc/passwd >/dev/null 2>&1 && bad "path in file name accepted" || pass "file name with path refused"

echo "-- retention keeps the newest N, never touches quick copies"
for n in 1 2 3 4 5 6 7 8 9; do cp $BK/$F $BK/bk1-2021010$n-000000.tar.gz; done
cp $BK/$F $BK/bk1-20210101-000001-live.tar.gz; chown minecraft:minecraft $BK/bk1-2021*
mcb schedule bk1 --keep 3 --weekly 0 >/dev/null; sleep 1.2; mcb now bk1 >/dev/null 2>&1
test "$(ls $BK | grep -cE '^bk1-[0-9]{8}-[0-9]{6}\.tar\.gz$')" = 3 && pass "only the newest 3 clean backups kept" || bad "clean count: $(ls $BK | grep -cE '^bk1-[0-9]{8}-[0-9]{6}\.tar\.gz$')"
test -f $BK/bk1-20210101-000001-live.tar.gz && pass "quick (live) copy left alone" || bad "live copy pruned"

echo "-- a server seen for the first time is not reported as missed"
as_mc "rm -f /opt/minecraft/.config/MCS/backup/bk1.json"
mcb schedule bk1 --on --time 00:00 >/dev/null; as_mc "rm -f /opt/minecraft/.config/MCS/backup/bk1.json"
systemctl start MCSbackup.service; sleep 1
mcb schedule bk1 | grep -q '"last_status"' && bad "first sight reported a status" || pass "first sight records the slot quietly, no false 'missed'"
echo "-- the timer path: due now, run through systemd"
NOW=$(date +%H:%M); EARLIER=$(date -d '-2 minutes' +%H:%M)
as_mc "rm -f /opt/minecraft/.config/MCS/backup/bk1.json"
mcb schedule bk1 --on --time "$EARLIER" --days 0,1,2,3,4,5,6 --keep 20 >/dev/null
su minecraft -s /bin/bash -c "python3 -I -c 'import sys; sys.path.insert(0, \"$LIBDIR\"); import mcsbackup as mb; mb.set_state(\"bk1\", last_sched=\"2000-01-01T00:00:00\")'"  # as if this server had been seen long ago
BEFORE=$(ls $BK | grep -c '^bk1-')
as_mc "touch $I/bk1/world/level.dat"
systemctl start MCSbackup.service; sleep 1
systemctl is-failed --quiet MCSbackup.service && bad "MCSbackup.service failed" "$(journalctl -u MCSbackup -n 8 --no-pager -o cat)" || pass "systemd ran MCSbackup.service"
test "$(ls $BK | grep -c '^bk1-')" -gt "$BEFORE" && pass "a scheduled backup was made" || bad "no new backup" "$(journalctl -u MCSbackup -n 8 --no-pager -o cat)"
mcb schedule bk1 | grep -q '"last_status": "ok"' && pass "result recorded for the web page" || bad "state not recorded"
AFTER=$(ls $BK | grep -c '^bk1-'); systemctl start MCSbackup.service; sleep 1
test "$(ls $BK | grep -c '^bk1-')" = "$AFTER" && pass "the same slot does not run twice" || bad "ran twice"
systemctl is-enabled --quiet MCSbackup.timer && pass "timer is enabled" || bad "timer not enabled"

echo "-- awkward moments (systemd calls replaced by stand-ins so each one can be forced)"
as_mc "mkdir -p $I/bk2/world && echo x > $I/bk2/world/level.dat && printf 'SERVER=NULL\n' > $I/bk2/service.conf"
cat > /tmp/bkx.py <<'EOF'
import sys, os, time, datetime as dt
LIBDIR, F = sys.argv[1], sys.argv[2]
sys.path.insert(0, LIBDIR)
import mcsbackup as mb
def t(d, ok): print(("PASS  " if ok else "FAIL  ") + d)
real_sleep = time.sleep
mb.time.sleep = lambda s: None          # no real waiting
st = {"up": True}
mb.unit_state = lambda n: "active" if st["up"] else "inactive"
mb.stop_server = lambda n: st.update(up=False)
mb.start_server = lambda n: st.update(up=True)
mb.countdown = lambda n, m: None
real = mb.make_archive

# the process is killed in the middle of a backup (web page restarted, SIGTERM): the server must come back
mb.make_archive = lambda *a, **k: (_ for _ in ()).throw(SystemExit(143))
try: mb.backup_clean("bk1", 0)
except SystemExit: pass
t("killed mid-backup: the server is started again", st["up"] and not mb.get_state("bk1").get("stopped_for_backup"))

# ...and if even that never happened (power cut), the timer notices the note and starts it
st["up"] = False
mb.set_state("bk1", stopped_for_backup=123, recover_tries=0)
mb.recover("bk1", mb.get_state("bk1"))
t("a server left stopped by an interrupted backup is started by the timer", st["up"] and not mb.get_state("bk1")["stopped_for_backup"])
mb.set_state("bk1", stopped_for_backup=123)
mb.recover("bk1", mb.get_state("bk1"))
t("a server that is already running is left alone", st["up"] and not mb.get_state("bk1")["stopped_for_backup"])
def boom(n): raise mb.BackupError("nope")
st["up"] = False; keep_start = mb.start_server; mb.start_server = boom
mb.set_state("bk1", stopped_for_backup=123, recover_tries=5)
mb.recover("bk1", mb.get_state("bk1"))
s = mb.get_state("bk1")
t("after repeated failures the web page is told to start it by hand", s.get("last_status") == "failed" and "by hand" in s.get("last_message", ""))
mb.start_server = keep_start; mb.set_state("bk1", stopped_for_backup=0)

# someone starts the server while it is being copied: that copy is not clean, so it is thrown away
st["up"] = False
def sneaky(n, k="clean", sk=None):
    p = real(n, k, sk); st["up"] = True; return p
mb.make_archive = sneaky
before = set(os.listdir(mb.BACKUPS))
try: mb.backup_clean("bk1", 0); ok = False
except mb.BackupError as e: ok = "thrown away" in str(e)
t("server started during the copy: the copy is discarded", ok and set(os.listdir(mb.BACKUPS)) == before)
mb.make_archive = real

# a server that is still starting up is stopped first, never copied half-started
st["up"] = True; stops = []
mb.unit_state = lambda n: "activating" if st["up"] else "inactive"
mb.stop_server = lambda n: (stops.append(n), st.update(up=False))
mb.backup_clean("bk1", 0)
t("a server still starting up is stopped before copying", stops == ["bk1"])
mb.unit_state = lambda n: "active" if st["up"] else "inactive"; mb.stop_server = lambda n: st.update(up=False)

# another backup running: the scheduled slot is not used up, it is tried again
now = dt.datetime.now()
mb.set_config("bk1", {"enabled": True, "time": (now - dt.timedelta(minutes=1)).strftime("%H:%M"), "days": [0, 1, 2, 3, 4, 5, 6]})
mb.set_state("bk1", last_sched="2000-01-01T00:00:00", stopped_for_backup=0)
with mb.exclusive():
    r = mb._run_one("bk1", now)
t("a busy lock does not use up the slot", r is False and mb.get_state("bk1")["last_sched"] == "2000-01-01T00:00:00")

# one server's trouble does not stop the next server's backup
seen = []
def fake(name, now):
    seen.append(name)
    if name == "bk1": raise RuntimeError("boom")
    return False
mb._run_one = fake
mb.run_due()
t("an error in one server does not skip the others", "bk1" in seen and "bk2" in seen)

# restoring onto a nearly full disk is refused before anything is touched
class U: free = 1
orig = mb.shutil.disk_usage; mb.shutil.disk_usage = lambda p: U
try: mb.restore("bk1", F); ok = False
except mb.BackupError as e: ok = "not enough disk space" in str(e)
mb.shutil.disk_usage = orig
t("restore refuses when there is no room, and says nothing was changed", ok)
t("restore unpacks beside the servers (one disk, so the swap cannot fail)", mb.STAGING.parent == mb.INSTANCES)

# deleting a file counts as a change (so 'unchanged' backups are not wrongly skipped)
d = mb.INSTANCES / "bk1"
(d / "gone.txt").write_text("x"); real_sleep(1.1); mark = time.time(); real_sleep(1.1); (d / "gone.txt").unlink()
t("deleting a file counts as a change", mb.newest_change(d) > mark)
EOF
chmod 644 /tmp/bkx.py
as_mc "python3 -I /tmp/bkx.py $LIBDIR $F" >/tmp/bkx.out 2>&1; cat /tmp/bkx.out; grep -q Traceback /tmp/bkx.out && bad "awkward-moments script crashed"; rm -f /tmp/bkx.py /tmp/bkx.out
as_mc "rm -rf $I/bk2 /opt/minecraft/.config/MCS/backup/bk2.json"
as_mc "rm -f $BK/bk2-*"

echo "-- players, links, backup tests and notices"
cat > /tmp/bkx2.py <<'EOF'
import sys, os, time, json, gzip, zipfile, socket, threading
sys.path.insert(0, sys.argv[1])
import mcsbackup as mb
def t(d, ok): print(("PASS  " if ok else "FAIL  ") + d)
d = mb.INSTANCES / "bk3"
(d / "world" / "region").mkdir(parents=True, exist_ok=True); (d / "mods").mkdir(exist_ok=True)
with gzip.open(d / "world" / "level.dat", "wb") as g: g.write(b"\x0a\x00\x00\x00")
(d / "world" / "region" / "r.0.0.mca").write_bytes(os.urandom(12288))
with zipfile.ZipFile(d / "mods" / "ok.jar", "w") as z: z.writestr("fabric.mod.json", "{}")
def props(port): (d / "server.properties").write_text(f"level-name=world\nserver-port={port}\n")

def fake_server(online):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    def run():
        c, _ = srv.accept(); c.recv(1024)
        body = json.dumps({"players": {"online": online, "max": 20}}).encode()
        pkt = b"\x00" + mb._varint(len(body)) + body
        c.sendall(mb._varint(len(pkt)) + pkt); c.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]

# who is online (asked the way the game's own server list asks)
props(fake_server(3)); t("players online read from the server (3)", mb.players_online("bk3") == 3)
props(fake_server(0)); t("an empty server reads as 0", mb.players_online("bk3") == 0)
props(1); t("a server that cannot be asked reads as unknown, not zero", mb.players_online("bk3") is None)
sent = []; mb.console = lambda n, x: (sent.append(x), True)[1]; mb.time.sleep = lambda s: None
props(fake_server(0)); mb.countdown("bk3", 2)
t("nobody online: no warning, no waiting", sent == [])
props(1); mb.countdown("bk3", 2)
t("cannot tell: players are warned, to be safe", any(x.startswith("say ") for x in sent))

# a link inside the server folder: skipped, reported, and nagged about until fixed
os.symlink("/etc/passwd", d / "link")
r = mb.backup_clean("bk3", 0)
names = []
import tarfile
with tarfile.open(mb.BACKUPS / r["file"]) as tf: names = tf.getnames()
t("a link is skipped and the backup still succeeds", r["result"] == "ok" and r["skipped"] == ["link"] and not any(n.endswith("/link") for n in names))
n = {x["id"]: x for x in mb.notices_all()}
t("the banner nags about it", n.get("bk3:skipped", {}).get("level") == "warn" and "link" in n["bk3:skipped"]["text"])
mb.backup_clean("bk3", 0, only_if_changed=True)
t("an 'unchanged' run does not clear the nag", "bk3:skipped" in {x["id"] for x in mb.notices_all()})
os.unlink(d / "link"); mb.backup_clean("bk3", 0)
t("fixed: the next backup clears it", "bk3:skipped" not in {x["id"] for x in mb.notices_all()})

# testing a backup
good = mb.list_backups("bk3")[0]["name"]
r = mb.test_backup("bk3", good)
t("a good backup passes its test", r["ok"] is True and "1 region" in r["message"] and "1 mod" in r["message"])
t("the scratch folder is cleaned up", not any(p.name.startswith("test-bk3") for p in mb.STAGING.iterdir()) if mb.STAGING.exists() else True)
def region(keep):  # one 500-byte chunk in the third block, with only `keep` bytes of it written
    table = (2 << 8 | 1).to_bytes(4, "big").ljust(8192, b"\0")
    (d / "world" / "region" / "r.0.0.mca").write_bytes(table + ((500).to_bytes(4, "big") + b"x" * 500)[:keep])
region(504)
r = mb.test_backup("bk3", mb.backup_clean("bk3", 0)["file"])
t("a region file not rounded up to a full block passes", r["ok"] is True)
region(200)
bad = mb.backup_clean("bk3", 0)["file"]; r = mb.test_backup("bk3", bad)
t("a cut-off region file is caught", r["ok"] is False and "damaged" in r["message"])
t("the banner reports the failed test", "bk3:test" in {x["id"] for x in mb.notices_all()})
(d / "world" / "region" / "r.0.0.mca").write_bytes(b"x" * 100)
r = mb.test_backup("bk3", mb.backup_clean("bk3", 0)["file"])
t("a region file too short for its tables is caught", r["ok"] is False and "damaged" in r["message"])
(d / "world" / "region" / "r.0.0.mca").write_bytes(os.urandom(12288))
(d / "mods" / "broken.jar").write_bytes(b"not a zip")
r = mb.test_backup("bk3", mb.backup_clean("bk3", 0)["file"])
t("a broken mod jar is caught", r["ok"] is False and "broken.jar" in r["message"])
os.unlink(d / "mods" / "broken.jar")
class U: free = 1
orig = mb.shutil.disk_usage; mb.shutil.disk_usage = lambda p: U
r = mb.test_backup("bk3", good); mb.shutil.disk_usage = orig
t("no room to unpack: says so instead of failing the backup", r["ok"] is None and "not enough free space" in r["message"])
fresh = mb.backup_clean("bk3", 0)["file"]
t("a fixed server tests clean again and the banner clears", mb.test_backup("bk3", fresh)["ok"] is True and "bk3:test" not in {x["id"] for x in mb.notices_all()})

# more banner cases
mb.set_state("bk3", last_status="failed", last_message="disk full")
t("a failed backup shows in the banner", any(x["id"] == "bk3:failed" and x["level"] == "bad" and "disk full" in x["text"] for x in mb.notices_all()))
mb.set_state("bk3", last_status="ok")
old = time.time() - 3 * 86400
for b in mb.list_backups("bk3"): os.utime(mb.BACKUPS / b["name"], (old, old))
t("no good backup for days shows in the banner", "bk3:old" in {x["id"] for x in mb.notices_all()})
mb.set_config("bk3", {"enabled": False})
t("backups switched off is a gentle info notice", [x["level"] for x in mb.notices("bk3")] == ["info"])
mb.set_config("bk3", {"enabled": True})
mb.backup_clean("bk3", 0)   # leave one fresh good backup for the web checks
mb.set_state("bk3", last_test={"ok": True, "time": int(time.time()), "file": "x", "message": "ok"})
EOF
chmod 644 /tmp/bkx2.py
as_mc "mkdir -p $I/bk3 && python3 -I /tmp/bkx2.py $LIBDIR" >/tmp/bkx2.out 2>&1; grep -Ev '^copying|^saved|^testing|^Backup tested|^NOT copied|^nobody' /tmp/bkx2.out; grep -q Traceback /tmp/bkx2.out && bad "players/links/tests script crashed"; rm -f /tmp/bkx2.py /tmp/bkx2.out
F3=$(ls -1 $BK | grep '^bk3-[0-9]\{8\}-[0-9]\{6\}\.tar\.gz$' | tail -1)

echo "-- a restore that dies partway (power cut, crash): tidied up by the timer"
cat > /tmp/bkx3.py <<'EOF'
import sys, os, json
sys.path.insert(0, sys.argv[1])
import mcsbackup as mb
def t(d, ok): print(("PASS  " if ok else "FAIL  ") + d)
d = mb.INSTANCES / "bk4"
def left(): return sorted(p.name for p in mb.STAGING.iterdir()) if mb.STAGING.exists() else []
def fresh():
    mb.shutil.rmtree(d, ignore_errors=True); mb.shutil.rmtree(mb.STAGING, ignore_errors=True)
    (d / "world").mkdir(parents=True); (d / "world" / "level.dat").write_text("old")
    (d / "logs").mkdir(); (d / "logs" / "latest.log").write_text("log")
    f = mb.make_archive("bk4", "clean").name
    (d / "world" / "level.dat").write_text("current")
    return f
def die_at(f, module, attr, when):
    """Run a restore in a child that is killed outright (no cleanup runs) at the chosen step."""
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        real = getattr(module, attr)
        def hook(*a, **kw):
            if when(*a): os._exit(9)
            return real(*a, **kw)
        setattr(module, attr, hook)
        try: mb.restore("bk4", f)
        finally: os._exit(0)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 9
def world(): return (d / "world" / "level.dat").read_text()
def logs(): return (d / "logs" / "latest.log").is_file()
def noticed(): return any(x["id"].startswith("bk4:interrupted") for x in mb.notices("bk4"))

mb.set_state("bk4", restore_interrupted=0)
f = fresh(); ok = die_at(f, mb.os, "rename", lambda a, b: str(a).startswith(str(mb.STAGING)) and b == d)
t("killed between the two halves of the swap: the server folder is gone", ok and not d.exists())
mb.clean_leftovers()
t("it is put back, with its logs, and the scratch space is empty", d.is_dir() and world() == "current" and logs() and left() == [])
t("the banner says the restore did not happen", noticed())
mb.set_config("bk4", {"enabled": False}); t("it still says so with automatic backups off", noticed()); mb.set_config("bk4", {"enabled": True})
mb.set_state("bk4", restore_interrupted=1); t("the warning goes away after a week", not noticed())

mb.set_state("bk4", restore_interrupted=0)
f = fresh(); ok = die_at(f, mb.os, "rename", lambda a, b: a == d)
t("killed just before the swap: logs are stranded in the scratch copy", ok and world() == "current" and not logs())
mb.clean_leftovers()
t("logs are given back and the scratch space is empty", world() == "current" and logs() and left() == [] and noticed())

mb.set_state("bk4", restore_interrupted=0)
f = fresh(); ok = die_at(f, mb.shutil, "rmtree", lambda p, *a: "old~" in str(p))
t("killed after the swap: the old folder is left over", ok and world() == "old" and any(n.startswith("old~") for n in left()))
mb.clean_leftovers()
t("the restored world stays, the old folder goes, no false warning", world() == "old" and logs() and left() == [] and not noticed())

f = fresh(); die_at(f, mb.os, "rename", lambda a, b: str(a).startswith(str(mb.STAGING)) and b == d)
d.mkdir(); (d / "new-server").write_text("x")   # someone made a server with the same name in the meantime
mb.clean_leftovers()
t("a name reused after the crash: the old copy is kept, the new server untouched", any(n.startswith("old~") for n in left()) and (d / "new-server").is_file() and not logs())

f = fresh(); die_at(f, mb.os, "rename", lambda a, b: a == d)
with mb.exclusive():
    before = left(); mb.clean_leftovers()
    t("nothing is touched while a restore or backup holds the lock", before and left() == before)
(mb.STAGING / "test~bk4~1").mkdir(); (mb.STAGING / "something-else").mkdir()
mb.clean_leftovers()
t("an abandoned test folder goes; an unrecognised folder is left alone", left() == ["something-else"])
mb.shutil.rmtree(d, ignore_errors=True); mb.shutil.rmtree(mb.STAGING / "something-else")
EOF
chmod 644 /tmp/bkx3.py
as_mc "python3 -I /tmp/bkx3.py $LIBDIR" >/tmp/bkx3.out 2>&1; grep -E '^(PASS|FAIL)|Traceback|Error' /tmp/bkx3.out; grep -q Traceback /tmp/bkx3.out && bad "interrupted-restore script crashed"; grep -q '^FAIL' /tmp/bkx3.out && fail=1; rm -f /tmp/bkx3.py /tmp/bkx3.out
as_mc "rm -f $BK/bk4-* /opt/minecraft/.config/MCS/backup/bk4.json"

echo "-- web page API"
KEY="$(/usr/local/bin/MCSkey add "bk$$" --quiet)"
req POST /api/login "{\"key\":\"$KEY\"}"; want "login" 200
req GET /api/instances/bk1/backup-schedule; want "schedule readable" 200 '"config"'
req PUT /api/instances/bk1/backup-schedule '{"time":"05:30","keep":30,"warn_minutes":1,"days":[0,2,4]}'; want "schedule saved" 200 '"05:30"'
req PUT /api/instances/bk1/backup-schedule '{"time":"99:99"}'; want "bad time refused" 400
req PUT /api/instances/bk1/backup-schedule '{"keep":0}'; want "bad keep refused" 400
req GET /api/instances/bk1/backups; want "list has kinds" 200 '"kind": "clean"'
req POST /api/instances/bk1/backups/$F/restore '{}'; want "restore needs confirm" 400
req POST /api/instances/bk1/backups/..%2Fx.tar.gz/restore '{"confirm":true}'; want "restore name traversal refused" 400
req POST /api/instances/bk1/backups '{"mode":"sideways"}'; want "unknown backup mode refused" 400
req POST /api/instances/bk1/backups '{"mode":"clean"}'; want "safe backup job starts" 200 '"id"'; waitjob
grep -q '"state": "done"' <<<"$BODY" && pass "safe backup job finished" || bad "backup job" "$BODY"
as_mc "echo 'diamonds=1' > $I/bk1/world/level.dat"
req POST /api/instances/bk1/backups/$F/restore '{"confirm":true}'; want "restore job starts" 200 '"id"'; waitjob
grep -q '"state": "done"' <<<"$BODY" && grep -q 'diamonds=64' $I/bk1/world/level.dat && pass "restore from the web page works" || bad "web restore" "$BODY"

req GET /api/notices; want "notices readable" 200 '^\['
req POST /api/instances/bk3/backups/$F3/test '{}'; want "backup test job starts" 200 '"id"'; waitjob
grep -q '"state": "done"' <<<"$BODY" && pass "backup test job finished" || bad "test job" "$BODY"
req GET /api/instances/bk3/backup-schedule; want "test result is recorded for the page" 200 '"last_test"'
req POST /api/instances/bk3/backups/..%2Fx.tar.gz/test '{}'; want "test name traversal refused" 400

echo "-- real server: warn, stop, copy, start again (the smoke server)"
if [[ -d $I/smoke ]] && as_mc "grep -q '^SERVER=.*jar' $I/smoke/service.conf"; then
  systemctl reset-failed "MCS@smoke*" 2>/dev/null; systemctl start MCS@smoke.service 2>/dev/null
  for _ in $(seq 1 60); do systemctl is-active --quiet MCS@smoke && break; sleep 2; done
  if systemctl is-active --quiet MCS@smoke; then
    T0=$(date +%s); mcb now smoke --warn 0 >/tmp/bk-smoke.out 2>&1; RC=$?
    [[ $RC == 0 ]] && grep -q "checked" /tmp/bk-smoke.out && pass "backup of a running server succeeded" || bad "smoke backup failed" "$(tail -5 /tmp/bk-smoke.out)"
    systemctl is-active --quiet MCS@smoke && pass "server was started again afterwards ($(( $(date +%s) - T0 ))s total)" || bad "server not running after backup"
    SF=$(ls -1t $BK | grep '^smoke-[0-9]\{8\}-[0-9]\{6\}\.tar\.gz$' | head -1)
    LV=$(grep '^level-name=' $I/smoke/server.properties | cut -d= -f2); tar -tzf $BK/$SF | grep -q "smoke/$LV/level.dat" && pass "the world ('$LV') is in the smoke backup" || bad "no world in $SF"
    systemctl stop MCS@smoke 2>/dev/null
  else
    echo "SKIP  smoke server would not start here (check its mods/); the stop/start path was not exercised"
  fi
else
  echo "SKIP  no smoke server"
fi

echo; [[ $fail == 0 ]] && echo "all passed" || echo "FAILURES"; exit $fail
