# Principles and guiding rules

## Principles
1. Your server, your call. The person who runs a server decides what it does.
2. Magrathea installs and manages. It does not decide for you. It tells you what is risky and lets you choose.
3. Safety is a choice you make with the facts in front of you: whitelist on, backups kept somewhere
   safe, and plain words about the risks, because some players are kids.
4. Nothing hidden: what Magrathea downloads, from where, and what it changes on your machine is visible.
5. No lock-in: your worlds, mods and settings are ordinary files you can take with you.
6. Simple on purpose. One server user, ordinary files, no Docker-style images: you can see how it works.
   That has limits, and the docs say what they are.
7. Meant to be learned from. Magrathea lets you see how Linux, permissions, web pages and small programs
   work, so you can fix it. The teaching is the point, and there is no charge.

## Guiding rules
- Magrathea is a PACKAGE MANAGER, not a loader/launcher. It installs and manages; it does not interfere
  with game rules or other people's choices. Where something is risky, it warns you. It does not block or "fix" it.

  Example: `online-mode=false` gets a warning, not a refusal.

  **We do not support piracy.** There are honest reasons to turn `online-mode` off; getting the game without
  paying isn't one. Read the Mojang, Modrinth and CurseForge terms before you use their services.
- Scope: Linux + systemd only (see `INSTALL.md` for what has been tested). No Windows, no Bedrock, no Docker-style images.

  **Warning:** a cloud or VPS server has no home network to hide behind.
- Magrathea does not encrypt your files for you (too easy to get wrong); that is the operator's job. Logs
  (`log-ips`) and backups contain player names, IPs and player data. Any cloud upload would be off by default.
- Players or operators may be children: safe defaults matter.