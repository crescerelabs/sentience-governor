# v0.3.0.2 — root-scoped governance session tests

**Contract:** `docs/design/0001-langgraph-root-scoped-governance-sessions.md`
and `docs/design/0002-sessionmanager-concurrent-sessions-per-agent.md`.

These were written before the fix and committed red, so the fix is measured
rather than asserted. They cover:

- root-scoped sessions — a nested chain start must not open a second session,
  and a nested chain end must not tear one down;
- ancestry-based routing, since tool events parent to the node rather than to
  the root;
- overlapping root invocations through one handler, on threads and on one
  event loop;
- branch-scoped LLM turn telemetry, because parallel graph nodes have
  overlapping turns;
- middleware binding by active-root count;
- the legacy path, where no callback run ids are supplied;
- root isolation in the session registry.

## Running

```bash
python -m pytest tests/v0_3_0_2_cp2 -q
```

## Expected state

| Against | Result |
| :-- | :-- |
| v0.3.0.1, before the fix | **13 failed, 10 passed** |
| v0.3.0.2, after the fix | **23 passed** |

The 10 that pass before the fix are regression and compatibility tests that
must stay green through it — including the true-positive POL-001 on a tool the
caller never declared, which the fix must not suppress.

The 32 pre-existing adapter tests in `tests/test_langchain_handler.py` are part
of the same contract and are **unmodified** by this release.
