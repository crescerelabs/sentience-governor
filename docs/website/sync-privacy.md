# Cloud telemetry sunset

The experimental Sync cloud telemetry CLI was removed from the supported product in v0.2.8.3.

Sentience Governor is local-first. Local pulse reports, policy evaluation, slash commands, Claude Code capture, and trace files run on your machine.

The `sentience-sync` command remains only as a compatibility stub. Running it prints a local-first notice and exits 0.

## What changed

Removed:

- Sync cloud telemetry registration
- Sync cloud telemetry upload
- Sync cloud telemetry update-check flows

Unchanged:

- Local traces
- `sentience pulse`
- `sentience status`
- Governance profiles
- Claude Code hook capture
- Claude Code slash commands
- The Sentience Sync email list for product updates

## Network behavior

Sentience Governor does not make automatic telemetry uploads.

The optional Sentience Sync email list is separate. It is only used when you choose to submit an email for product updates.
