#!/bin/bash
# Does mcscheck.sh notice what it should? Needs MCS installed. Run as root on a throwaway machine only:
# for a few seconds it puts deliberately dangerous polkit rules in place, and it makes a user in the
# sudo (or wheel) group. Both are removed when it ends.
set -u
[[ $(id -u) -eq 0 ]] || { echo "run as root"; exit 1; }
CHECK=/opt/mcs/lib/MCS/mcscheck.sh
RULE=/etc/polkit-1/rules.d/48-mcs-test.rules
WHO=mcschecktest
fail=0
pass () { echo "PASS  $1"; }
bad () { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/        /'; fail=1; }
cleanup () { rm -f "$RULE"; chmod go-w "$CHECK" 2>/dev/null; id $WHO >/dev/null 2>&1 && userdel -r $WHO >/dev/null 2>&1; }
trap cleanup EXIT

group=sudo; getent group sudo >/dev/null || group=wheel
useradd -m -G $group $WHO || exit 1

run () { OUT="$(bash $CHECK $WHO 2>&1)"; RC=$?; }
says () { grep -q -- "$1" <<<"$OUT"; }
rule () { printf '%s\n' "$1" > "$RULE"; sleep 3; run; }

run
[[ $RC -eq 0 ]] && says "All as expected" && pass "a normal install passes" || bad "a normal install should pass" "$OUT"
says "not a security audit" && pass "it says what it is not" || bad "the limit is not stated" "$OUT"

# The mistake that started this: '=' where '==' was meant.
rule 'polkit.addRule(function(action, subject) {
if (action.id = "org.freedesktop.Flatpak.modify-repo" &&
subject.isInGroup("'$group'")) { return polkit.Result.YES; }
});'
[[ $RC -eq 1 ]] && says "not allowed to start and stop" && pass "a broken rule that blocks minecraft is noticed" || bad "blocked minecraft not noticed" "$OUT"
says "can manage system services without a password" && pass "the same rule's gift to the admin group is noticed" || bad "passwordless admin not noticed" "$OUT"

rule 'polkit.addRule(function(action, subject) {
if (subject.user === "minecraft") { return polkit.Result.YES; }
});'
says "not Minecraft servers" && says "starts at boot" && pass "too much power for minecraft is noticed" || bad "over-broad rule for minecraft not noticed" "$OUT"
says "without a password" && bad "warned about an account the rule does not touch" "$OUT" || pass "no warning about an account the rule does not touch"

rm -f "$RULE"; sleep 3
chmod g+w "$CHECK"; run
[[ $RC -eq 1 ]] && says "writable only by root" && says "$CHECK" && pass "a program file others could write is noticed" || bad "bad permissions not noticed" "$OUT"
chmod go-w "$CHECK"

before="$(ls -A /etc/polkit-1/rules.d)"; run
[[ $RC -eq 0 && "$(ls -A /etc/polkit-1/rules.d)" == "$before" ]] && pass "passes again, and changed no rules" || bad "not back to normal" "$OUT"
exit $fail
