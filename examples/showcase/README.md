# v0.2.4 — Undeclared-Intent Showcase

Three deliberate scenarios rendered as Markdown reports, illustrating
what `sentience analyze undeclared-intent --save` produces in
representative cases. The scenarios are encoded inline in
`regenerate.py` (single source of truth — no JSONL fixture files
checked in separately).

| Scenario | File | Status | Undeclared share | Footer branch |
|---|---|---|---:|---|
| Agent mostly on-task — one drift turn | `sample_report_low_undeclared.md` | `ok` | 10.0% | agent-bound |
| Agent drifts heavily into unrelated systems | `sample_report_high_undeclared.md` | `ok` | 51.1% | agent-bound |
| Surface lacks an intent primitive (Claude Code today) | `sample_report_no_intent.md` | `ok` | 100.0% | surface-bound |

The third scenario is the most operationally important to
understand: when no `INTENT_DECLARED` event fires anywhere in the
session, every attributed turn is undeclared, and the analyzer uses
the *surface-bound* footer copy (frames the result as a surface
limitation rather than agent drift). This is the metric correctly
diagnosing an ecosystem-level absence of an intent primitive — see
plan v3 §"Differentiated footer copy" and the strategy doc
the declaration-recipes guidance.

## Regenerating

```bash
python examples/showcase/regenerate.py
```

Re-running must produce byte-identical Markdown (the analyzer +
renderers are pure functions, fixtures are deterministic, and
rendered Markdown contains no timestamps or random IDs). To verify:

```bash
python examples/showcase/regenerate.py
git diff --exit-code examples/showcase/sample_report_*.md
```

## Related

* Live runnable demo: `examples/v024_undeclared_intent_demo.py`
* User guide: §9 in `docs/guide/sentience_governor.md` (added in v0.2.4)
