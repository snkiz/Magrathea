#!/bin/bash
# Regression: reinstalling the MCS files resets the template eula.txt to false
# while EULA.token survives. A new instance must still get eula=true.
# Run as root after tests/wsl-full.sh install:  SUDO_USER=$USER bash tests/wsl-eula-regression.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
(cd "$HERE" && echo y | ./MCSuser) >/dev/null 2>&1
echo "token:    $(cat /opt/minecraft/.local/lib/MCS/EULA.token)"
echo "template: $(grep -h '^eula=' /opt/minecraft/.local/lib/MCS/eula.txt)   (reset by reinstall, as expected)"
printf 'eulatest\n\nn\nn\nn\n' | su minecraft -s /bin/bash -c "~/toolbox/MCScreate" 2>&1 | grep -E "command not found|No desktop|ready"
echo "instance: $(grep -h '^eula=' /opt/minecraft/instance/eulatest/eula.txt)"
grep -q '^eula=true' /opt/minecraft/instance/eulatest/eula.txt && echo "PASS" || { echo "FAIL"; rc=1; }
rm -rf /opt/minecraft/instance/eulatest
exit "${rc:-0}"
