#!/bin/bash
# Is everything shipshape for MCS? MCSuser runs this when it finishes. To run it again:
#   sudo bash /opt/mcs/lib/MCS/mcscheck.sh
#
# It only looks. pkcheck asks polkit "would this be allowed?" without doing it. Nothing is
# started, stopped or changed, and nobody else's rules are touched.
# It looks at a few things MCS relies on. It is NOT a security audit.
#
# Exit: 0 as expected, 1 something is worth a look, 2 part of it could not be checked.
mcuser=minecraft
prefix="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
owner="${1:-$SUDO_USER}"   # the person who runs the servers
[[ $(id -u) -eq 0 ]] || { echo "Run this with sudo." >&2; exit 2; }

notes=()
unsure=()
rules_note=0

# May <user> do <action>? 0 = yes without a password, 1 = no (or it would ask), 2 = cannot tell.
may () {
  local user="$1" pid rc; shift
  setpriv --reuid="$user" --regid="$(id -g "$user")" --init-groups sleep 30 >/dev/null 2>&1 &
  pid=$!
  # polkit judges whoever owns this process, so wait until it has become <user> (it is then "sleep").
  for _ in $(seq 1 50); do
    [[ "$(cat "/proc/$pid/comm" 2>/dev/null)" == sleep ]] && break
    sleep 0.1
  done
  if [[ "$(cat "/proc/$pid/comm" 2>/dev/null)" == sleep ]]; then
    pkcheck --process "$pid" --action-id "$@" >/dev/null 2>&1; rc=$?
  else
    rc=127
  fi
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  case $rc in 0) return 0 ;; 1|2|3) return 1 ;; *) return 2 ;; esac
}
units=org.freedesktop.systemd1.manage-units
files=org.freedesktop.systemd1.manage-unit-files   # enable, disable, link: what starts at boot
server () { may "$mcuser" $units --detail unit MCS@mcscheck.service --detail verb "$1"; }

if ! command -v pkcheck >/dev/null || ! command -v setpriv >/dev/null || ! id -u "$mcuser" >/dev/null 2>&1; then
  unsure+=("permissions: needs polkit (pkcheck), setpriv and the $mcuser user")
else
  # polkit rereads its rules a moment after they change, so a first refusal gets a second try.
  server start || { sleep 2; server start; }; start=$?
  server stop; stop=$?
  if [[ $start -eq 2 || $stop -eq 2 ]]; then
    unsure+=("permissions: polkit did not answer (systemctl status polkit)")
  else
    if [[ $start -ne 0 || $stop -ne 0 ]]; then
      notes+=("The $mcuser user is not allowed to start and stop MCS servers, so the web page's buttons and
    scheduled backups will not work."); rules_note=1
    fi
    if may "$mcuser" $units --detail unit mcscheck-not-a-server.service --detail verb start; then
      notes+=("The $mcuser user, which mods run as, may start and stop services that are not Minecraft servers."); rules_note=1
    fi
    if may "$mcuser" $files; then
      notes+=("The $mcuser user, which mods run as, may change what starts at boot."); rules_note=1
    fi
    if [[ -n "$owner" && "$owner" != root ]] && id -u "$owner" >/dev/null 2>&1; then
      if may "$owner" $units || may "$owner" $files; then
        notes+=("The account '$owner' can manage system services without a password. Some people set that up
    on purpose. If you did not, it is worth finding out why."); rules_note=1
      fi
    fi
  fi
fi

# The program, its units and its commands must belong to root and be writable by nobody else.
paths=()
for p in "$prefix" /etc/MCS /etc/systemd/system/MCS* /usr/local/bin/MCS* /etc/profile.d/mcs.sh /etc/polkit-1/rules.d/50-mcs.rules; do
  [[ -e "$p" ]] && paths+=("$p")
done
wrong="$(find "${paths[@]}" ! -type l \( ! -user root -o -perm /022 \) -print 2>/dev/null | head -n 5)"
if [[ -n "$wrong" ]]; then
  notes+=("These should belong to root and be writable only by root (up to five shown):
    ${wrong//$'\n'/$'\n'    }
    Running MCSuser again puts MCS's own files right.")
fi
if [[ -d /etc/polkit-1/rules.d ]] && ! cmp -s "$prefix/lib/systemd/MCS.rules" /etc/polkit-1/rules.d/50-mcs.rules; then
  notes+=("MCS's permission rule (/etc/polkit-1/rules.d/50-mcs.rules) is missing or has been changed.
    Running MCSuser again replaces it.")
fi

echo
echo "Checking that things are shipshape for MCS..."
if [[ ${#notes[@]} -gt 0 ]]; then
  echo "  Worth a look. Nothing has been changed."
  for n in "${notes[@]}"; do echo "  - $n"; done
  if [[ $rules_note -eq 1 ]]; then
    echo "  Permissions come from polkit. MCS adds only 50-mcs.rules to /etc/polkit-1/rules.d/; another rule"
    echo "  there can change the answer. Mistakes in a rule show up in: sudo journalctl -u polkit"
  fi
fi
for u in "${unsure[@]}"; do echo "  Could not check $u"; done
[[ ${#notes[@]} -eq 0 && ${#unsure[@]} -eq 0 ]] && echo "  All as expected."
echo "  (This looks at a few things MCS relies on. It is not a security audit.)"
[[ ${#notes[@]} -gt 0 ]] && exit 1
[[ ${#unsure[@]} -gt 0 ]] && exit 2
exit 0
