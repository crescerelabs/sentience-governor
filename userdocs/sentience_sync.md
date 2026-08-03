# Sync cloud telemetry has been sunset

Sentience Governor is local-first. The experimental cloud telemetry CLI
(`sentience-sync` — `register` / `run` / `update-check`) was **removed from the
supported product surface in v0.2.8.3**. Local pulse, policy evaluation, slash
commands, and Claude Code capture are unchanged — everything runs on your machine.

The `sentience-sync` command remains only as a thin stub that prints a local-first
notice and exits 0, so existing scripts get a clear message rather than a broken
command.

Optional cloud / control-plane capabilities may return later as part of the MCP
roadmap.

> **Not the same thing:** the **"Sentience Sync" email list**
> (`getsentience.ai/sentience-sync`) — for product updates and future release
> notifications — is a separate, still-active list and is unaffected by this sunset.
