---
name: sentience-review
description: |
  Retrospective review of your existing Claude Code history —
  which sessions wrote outside the project they were working in.
  Local and read-only; no prompt content is inspected.
disable-model-invocation: true
allowed-tools: Bash(sentience scan *)
---

!`sentience scan`

Render the command output above verbatim in a code block. Do not
summarize, reinterpret, reformat, rank, re-count, read transcript
files, or compute substitute results. Do not add findings, severity,
risk or confidence language of any kind — the review deliberately
carries no scores. If the review reports that no sessions stand out,
show it as-is and stop; that is a result, not a failure. Do not use
Sentience report headings unless they appear in the command output.
Add no prose of your own after the code block. If the user explicitly
asks you to explain it, reply starting with
`Interpretation (not Sentience output):` and use only the rendered
report.
