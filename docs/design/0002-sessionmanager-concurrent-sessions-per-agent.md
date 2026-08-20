# Design 0002 — Concurrent sessions per `agent_id` in `SessionManager`

| | |
| :-- | :-- |
| **Status** | **Accepted and implemented** in v0.3.0.2, as the final extension to the design 0001 work. Grounding recorded as it was gathered, before any code was written. |
| **Release** | v0.3.0.2 |
| **Why** | CP3 shipped root-scoped governance sessions in the LangChain adapter. `SessionManager` still assumes one active session per `agent_id`, so opening root B force-closes root A. The adapter trace stays correct, but root isolation does not hold at the registry. |
| **Does not amend** | Design 0001, which is frozen at `765ab57`. This is a separate surface. |
| **Grounded against** | `sentience_governor` @ `e179008` (v0.3.0.1) plus the CP3 adapter patch, read-only, 2026-08-20 |

---

## 1. The responsible state

Exactly one structure enforces the invariant:

```python
self._agent_to_session: Dict[str, str] = {}   # manager.py:85 — agent_id → active session_id
```

It is read or written in **three places, all inside `manager.py`**:

| Line | Site | What it does |
| --: | :-- | :-- |
| 133 | `session_start` | Looks up the agent's prior session to detect a collision |
| 158 | `session_start` | Registers the new session as *the* session for that agent |
| 231 | `_close_entry` | `pop(entry.agent_id)` — removes the agent's mapping on any close |

**No consumer outside `manager.py`.** Zero references in the rest of
`sentience_governor/`, zero in the test suite. The value is never returned by
any public method.

## 2. What everything else is keyed by

`session_end`, `get_state`, `get_profile`, `acquire_sequence`, `touch`, the
`_sessions` registry, the per-session sequencing lock and `_SessionEntry`
itself are **already keyed by `session_id`**. So is the whole cache:
`init_session`, `clear_session` and every read/write take `session_id`
(`cache/cache.py:109-117`), and the cache holds no agent-level state at all.

`resumption.py` contains **no agent-level assumption whatsoever** — no
reference to `agent_id`, `session_start`, or the agent index.

The reaper (`manager.py:233-252`) iterates entries and closes them
individually. It is already per-session.

## 3. Teardown keyed by `session_id`: yes, with one fix

Ending A already cannot clear B's *session state* — `session_end(session_id)`
resolves one entry. The only agent-keyed side effect is line 231:

```python
self._agent_to_session.pop(entry.agent_id, None)      # pops regardless of WHICH session
```

Under today's semantics this is harmless: the only path that closes a session
while another is registered for the same agent is `session_start`, which
overwrites the mapping two lines later.

**It stops being harmless the moment two sessions are genuinely live.** With
force-close removed, `session_end(A)` would pop the agent's mapping while it
points at B, orphaning B from the index. This is latent in v0.3.0.1 and is
activated by the CP3 adapter. It must be fixed in the same change.

## 4. What `SESSION_FORCE_CLOSED` actually triggers

1. `_force_close(prior)` → `_close_entry` → state `CLOSED`, mapping popped.
2. A `logger.warning`.
3. The optional `on_session_force_closed(session_id, agent_id)` callback.

**Nothing in the product passes that callback.** All three construction sites
(`claude_code_hook.py:517`, `:570`, `demos/declare_intent_flip.py:70`) use
defaults. Its only user is one test.

Force-close is still doing real work for the single-run runtimes, and must be
preserved: the Claude Code hook and the MCP wrapper run one agent per process,
so a crashed run leaves a stale `ACTIVE` entry and the next run reclaims it.
Removing force-close outright would leak sessions in exactly those runtimes.

**Observed consequence today, measured:** with root A force-closed, A keeps
emitting correctly — right `session_id`, right sequence, right policy
evaluation — and `get_profile`, `acquire_sequence` and `touch` all still work
on a `CLOSED` entry, because none of them checks state. Teardown of both roots
is clean. So the defect is an invariant mismatch visible to anything that
reads session state, not a corruption of the trace.

## 5. Minimal design delta

**Opt in per call, not per manager.** The adapter receives a `SessionManager`
it did not construct, so a constructor flag would land on the integrator.

```python
# state
self._agent_sessions: Dict[str, Set[str]] = {}      # agent_id → live session_ids

# session_start(..., allow_concurrent: bool = False)
#   allow_concurrent=False (default): force-close every live session for this
#       agent, then register. At most one exists today, so behaviour is
#       byte-identical for every current caller.
#   allow_concurrent=True: register alongside; no force-close, no callback.

# _close_entry
#   discard THIS session_id from the agent's set, instead of popping the agent.
```

`SentienceCallbackHandler._open_root` passes `allow_concurrent=True`. Nothing
else changes.

### Impact

| Surface | Impact |
| :-- | :-- |
| Public API | One keyword parameter with a default, added to `session_start`. Additive; no signature break |
| Package exports | None. `__init__.py` still exports `SessionManager`, `SessionState` |
| Event schema | **None.** Events carry `session_id`; nothing is agent-scoped |
| Cache schema | **None.** Already fully `session_id`-keyed |
| Session semantics | Unchanged for every existing caller, because the default preserves force-close |
| Other callers | `claude_code_hook.py:521`, `:581`, `mcp.py:523`, `async_session`, the demo — all keep today's behaviour untouched |

**Verdict: narrow internal-state change. No public API break, no schema
impact.**

### Affected tests

| Test | Effect |
| :-- | :-- |
| `tests/test_session_manager.py::TestSessionCollision::test_force_close_prior_session` | **The only existing test that exercises the invariant.** It calls `session_start` twice with one `agent_id` and no `allow_concurrent`, so it passes unchanged under the default. Worth extending with a second case asserting that `allow_concurrent=True` keeps both live and that ending one leaves the other registered |
| `tests/test_session_manager.py::TestConcurrentSessions` | Uses distinct `agent_id`s; unaffected |
| CP2 suite | Should gain a test asserting both overlapping roots remain `ACTIVE` in the manager, which is the assertion CP2 currently lacks and the reason this slipped past it |
| Everything else | No test in the suite references `_agent_to_session`, `on_session_force_closed` (beyond the one above), or `SESSION_FORCE_CLOSED` |

## 6. Open

- Whether the CP2 suite gains the missing registry-state assertion as part of
  this change or as a separate correction.
- Whether force-close should also be reachable explicitly (an
  `abandon_sessions(agent_id)` call) once it is no longer implicit for
  concurrent callers. **Not proposed here**; no evidence it is needed.
