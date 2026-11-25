#!/bin/bash
# After a crash, does a stale 'stop' left in the console FIFO shut the restarted server down?
# Run as root after tests/wsl-full.sh fetch.  Takes about 3 minutes.
set -u
S="MCS@smoke"
systemctl reset-failed "$S" "$S.socket" 2>/dev/null
timeout 250 systemctl start "$S" || { echo "could not start"; exit 2; }
since="$(date '+%Y-%m-%d %H:%M:%S')"
kill -9 "$(pgrep -f 'mcs-smoke' | head -n 1)"
echo "killed java, waiting for the automatic restart..."
for i in $(seq 1 60); do sleep 2; [[ $(systemctl is-active "$S") == active ]] && break; done
echo "restarted: $(systemctl is-active "$S") after ~$((i*2))s; waiting 45s to see whether it stays up"
sleep 45
state=$(systemctl is-active "$S")
echo "state now: $state"
journalctl -u "$S" --since "$since" --no-pager -o cat | grep -E "Stopping the server|SHUTTING|Server is up|Done \(" | sed 's/^/   /'
if [[ $state == active ]] && ! journalctl -u "$S" --since "$since" -o cat | grep -q "Stopping the server"; then
  echo "PASS: restarted server stayed up"; rc=0
else
  echo "FAIL: restarted server was told to stop"; rc=1
fi
timeout 200 systemctl stop "$S"
exit $rc
