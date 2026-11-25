# Principles and guiding rules

Status: the guiding rules are decided. The five principles for the README are a draft; the wording is not final.

## Guiding rules (DECIDED)
- MCS is a PACKAGE MANAGER, not a loader/launcher. It installs and manages; it does not interfere
  with game rules or other people's choices. Where something is risky, WARN ("caution tape"),
  do not block or "fix" it. Example: `online-mode=false` gets a warning, not a refusal.
  Not encouraging piracy; read Mojang/Modrinth/CurseForge terms to stay clear of trouble.
- Scope: Linux + systemd (Debian tested) only. No Windows, no Bedrock, no containers.
  A cloud/VPS install gets a clear WARNING only (no LAN to hide behind). Not ready to be containerized.
- No encryption done for the operator (too easy to get wrong). Operator's responsibility: warn that
  logs (`log-ips`) and backups contain player names/IPs/player data, and keep cloud upload off by default.
- Players may be children: safe defaults matter, and the project must not be blamed for a kid getting hacked.

## Principles (DRAFT wording, owner to approve; goes at the top of the README and docs)
Short, calm, no soapbox, no naming companies; the meaning is in the lines themselves.
1. Your server, your call. The person who runs a server decides what it does.
2. MCS installs and manages. It does not decide for you. It tells you what is risky and lets you choose.
3. Safety is a choice you make with the facts in front of you: whitelist on, backups kept somewhere
   safe, and plain words about the risks, because some players are kids.
4. Nothing hidden: what MCS downloads, from where, and what it changes on your machine is visible.
5. No lock-in: your worlds, mods and settings are ordinary files you can take with you.
