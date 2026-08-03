---
name: sentience-profile
description: |
  View the active Sentience governance profile — the rules currently
  gating this run. View-only; profile editing stays in the CLI.
disable-model-invocation: true
allowed-tools: Bash(sentience profile view *)
---

!`sentience profile view`

Render the command output above verbatim in a code block. Do not
summarize, reinterpret, reformat, read trace files, or compute
substitute results. If the output says no_signal, no_turns, or
no_token_data, show it as-is and stop. Do not use Sentience report
headings unless they appear in the command output. Add no prose of
your own after the code block. If the user explicitly asks you to
explain it, reply starting with
`Interpretation (not Sentience output):` and use only the rendered
report.
