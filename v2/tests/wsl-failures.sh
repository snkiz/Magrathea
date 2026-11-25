#!/bin/bash
# Failure-path checks for MCS@ units. Run as root after tests/wsl-full.sh fetch.
set -u
INST=smoke
S="MCS@$INST"
hr () { echo; echo "=== $*"; }
hr "state after a normal stop (service, socket, FIFO)"
systemctl is-active "$S" "$S.socket"; ls -l "/run/$S.stdin" 2>&1 | head -n 1

hr "crash: start, kill -9 java, expect an automatic restart"
timeout 200 systemctl start "$S"; systemctl is-active "$S"
pid=$(pgrep -f "mcs-$INST" | head -n 1); echo "java pid $pid"
kill -9 "$pid"
for i in $(seq 1 25); do sleep 2; st=$(systemctl is-active "$S"); echo "  +$((i*2))s $st"; [[ $st == active ]] && break; done
systemctl show "$S" -p NRestarts -p Result --value | tr '\n' ' '; echo
journalctl -u "$S" -n 12 --no-pager -o cat | grep -E "SIGKILL|killed|Main process|Scheduled restart|Failed with|Started|Stopping|SHUTTING" | tail -n 8
timeout 200 systemctl stop "$S"

hr "misconfigured: SERVER=NULL must fail fast with a clear message and not loop forever"
cp /opt/minecraft/instance/$INST/service.conf /tmp/service.conf.bak
sed -i 's/^SERVER=.*/SERVER=NULL/' /opt/minecraft/instance/$INST/service.conf
systemctl reset-failed "$S" 2>/dev/null
timeout 60 systemctl start "$S" 2>&1 | head -n 2; echo "start rc=${PIPESTATUS[0]}"
sleep 25
systemctl is-active "$S"; systemctl show "$S" -p NRestarts -p Result --value | tr '\n' ' '; echo
journalctl -u "$S" -n 20 --no-pager -o cat | grep -E "MCS:|start request repeated|Failed|Scheduled" | tail -n 5
systemctl stop "$S" 2>/dev/null; systemctl reset-failed "$S" 2>/dev/null
cp /tmp/service.conf.bak /opt/minecraft/instance/$INST/service.conf; chown minecraft:minecraft /opt/minecraft/instance/$INST/service.conf
systemctl is-active "$S" "$S.socket"
