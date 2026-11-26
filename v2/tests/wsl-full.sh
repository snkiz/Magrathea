#!/bin/bash
# Full dry run of MCS v2 on a disposable systemd machine (a WSL distro, a VM).
# Run as root:   sudo SUDO_USER=$USER bash tests/wsl-full.sh [install|create|fetch|run|all|teardown]
# It creates the real 'minecraft' user, /opt/minecraft and the systemd units.
# 'teardown' removes all of that again.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INST=smoke
as_mc () { su minecraft -s /bin/bash -c "$1"; }
hr () { echo; echo "=== $*"; }

install_stage () {
  hr "MCSuser (fresh install)"
  (cd "$HERE" && ./MCSuser) 2>&1 | tail -n 25
  hr "result"
  id minecraft; ls -la /opt/minecraft | head -20
  ls -l /etc/systemd/system/MCS@.service /etc/systemd/system/MCS@.socket
  ls -l /opt/mcs/bin
  id "$SUDO_USER"
  hr "MCSuser again (should be idempotent: replaces the program, keeps the data)"
  (cd "$HERE" && ./MCSuser) 2>&1 | tail -n 8
}

create_stage () {
  hr "MCScreate, driven non-interactively"
  printf 't\n%s\n\nn\nn\n\nNotch\nn\n' "$INST" | as_mc "/opt/mcs/bin/MCScreate" 2>&1 | tail -n 30
  hr "result"
  as_mc "ls -la ~/instance/$INST; echo; cat ~/instance/$INST/eula.txt; echo; grep -E '^(SERVER|JAVA_HOME|RAM)=' ~/instance/$INST/service.conf; grep -E '^(level-name|motd)=' ~/instance/$INST/server.properties"
  hr "MCScreate again with the same name must refuse, then accept a new one"
  printf '%s\nbad name!\n%s2\n\nn\nn\n\nNotch\nn\n' "$INST" "$INST" | as_mc "/opt/mcs/bin/MCScreate" 2>&1 | grep -E "already exists|may only|ready"
  as_mc "rm -rf ~/instance/${INST}2"
}

fetch_stage () {
  hr "MCSfetch jar + mod"
  as_mc "/opt/mcs/bin/MCSfetch jar $INST --type fabric --mc 1.21.11" 2>&1
  as_mc "/opt/mcs/bin/MCSfetch add $INST lithium" 2>&1
  # keep memory modest for the test machine
  as_mc "sed -i 's/^RAM=.*/RAM=-Xmx1G -Xms1G/' ~/instance/$INST/service.conf"
  as_mc "grep -E '^(SERVER|JAVA_HOME|RAM)=' ~/instance/$INST/service.conf; ls -la ~/instance/$INST ~/instance/$INST/mods"
}

run_stage () {
  hr "systemctl start MCS@$INST   (waits for 'Done', Type=notify)"
  local t0=$SECONDS
  timeout 280 systemctl start "MCS@$INST"; echo "start rc=$? after $((SECONDS-t0))s"
  systemctl is-active "MCS@$INST" "MCS@$INST.socket"
  systemctl status "MCS@$INST" --no-pager -n 0 | head -n 14
  hr "process name"
  ps -eo pid,user,comm,args | grep -E "mcs-$INST|MCS-run" | grep -v grep | cut -c1-150
  hr "console: send 'list' through the FIFO as root (journal tail follows)"
  ls -l "/run/MCS@$INST.stdin"
  if [[ -p "/run/MCS@$INST.stdin" ]]; then echo "list" > "/run/MCS@$INST.stdin"; sleep 2
  else echo "NOT A FIFO: /run/MCS@$INST.stdin"; fi
  journalctl -u "MCS@$INST" -n 6 --no-pager -o cat
  hr "console as an unrelated user must be denied"
  su nobody -s /bin/sh -c "echo 'op nobody' > /run/MCS@$INST.stdin" 2>&1 | head -n 2
  hr "files written (confined to the instance dir?)"
  as_mc "ls ~/instance/$INST; ls ~/instance/$INST/mods"
  hr "systemctl stop (graceful: warn, save-all, stop)"
  local t1=$SECONDS
  timeout 200 systemctl stop "MCS@$INST"; echo "stop rc=$? after $((SECONDS-t1))s"
  systemctl is-active "MCS@$INST"; systemctl is-failed "MCS@$INST"
  journalctl -u "MCS@$INST" -n 25 --no-pager -o cat | grep -E "SHUTTING|GOODBYE|Stopping|Saving|ThreadedAnvil|All dimensions|Done|Stopped|exit|Failed" | head -n 15
  ls -d /run/MCS@* 2>&1 | head -n 3
  hr "start again, then enable/disable"
  timeout 280 systemctl start "MCS@$INST"; systemctl is-active "MCS@$INST"
  systemctl enable "MCS@$INST" 2>&1 | tail -n 2; systemctl disable "MCS@$INST" 2>&1 | tail -n 1
  timeout 200 systemctl stop "MCS@$INST"; systemctl is-active "MCS@$INST"
}

teardown_stage () {
  hr "teardown"
  systemctl stop "MCS@$INST" 2>/dev/null
  systemctl disable "MCS@$INST" 2>/dev/null
  systemctl disable --now MCSweb 2>/dev/null
  systemctl disable --now MCSbackup.timer MCSoffbox.timer MCScred.socket 2>/dev/null; systemctl stop MCScred.service 2>/dev/null
  rm -f /etc/systemd/system/MCSinit@.service /etc/systemd/system/MCScred.socket /etc/systemd/system/MCScred.service /etc/systemd/system/MCS@.service /etc/systemd/system/MCS@.socket /etc/systemd/system/MCSweb.service /etc/systemd/system/MCSbackup.service /etc/systemd/system/MCSbackup.timer /etc/systemd/system/MCSoffbox.service /etc/systemd/system/MCSoffbox.timer /etc/polkit-1/rules.d/50-mcs.rules /etc/profile.d/mcs.sh
  for t in MCSimport MCScreate MCSbrowse MCSconf MCSfetch MCSweb MCSkey MCSrcon MCScred MCSoffbox MCSbackup MCShelp; do rm -f "/usr/local/bin/$t"; done
  systemctl daemon-reload
  [[ -n "${SUDO_USER:-}" ]] && gpasswd -d "$SUDO_USER" minecraft 2>&1 | tail -n 1
  userdel -r minecraft 2>&1 | tail -n 2; getent group minecraft >/dev/null && groupdel minecraft
  rm -rf /opt/mcs /opt/minecraft /etc/MCS /usr/local/lib/MCS
  id minecraft 2>&1 | head -n 1
}

case "${1:-all}" in
  install) install_stage ;; create) create_stage ;; fetch) fetch_stage ;; run) run_stage ;;
  teardown) teardown_stage ;;
  all) install_stage; create_stage; fetch_stage; run_stage ;;
  *) echo "usage: $0 install|create|fetch|run|all|teardown"; exit 2 ;;
esac
