---
name: sentience-status
description: |
  Sentience capture status — is the Claude Code hook wired and
  capturing governance events? Quick health check.
disable-model-invocation: true
allowed-tools: Bash(sentience status *)
---

!`sentience status`

Render the command output above verbatim in a code block. Do not
summarize, reinterpret, reformat, read trace files, or compute
substitute results. If the output says no_signal, no_turns, or
no_token_data, show it as-is and stop. Do not use Sentience report
headings unless they appear in the command output. Add no prose of
your own after the code block. If the user explicitly asks you to
explain it, reply starting with
`Interpretation (not Sentience output):` and use only the rendered
report.
