# Design 0001 — Root-scoped governance sessions for LangChain callbacks

| | |
| :-- | :-- |
| **Status** | **Accepted and implemented** in v0.3.0.2. The body below is the contract the implementation was built and tested against, recorded as it was designed rather than rewritten afterwards. |
| **Release** | v0.3.0.2 |
| **Supersedes** | The single-slot `_root_run_id` guard sketched in the release plan, **withdrawn at CP0** |
| **Revision** | Amended 2026-08-18 after CP1 architecture review: all mutable execution state root-scoped, middleware binding resolved, active-root eviction removed. **Further amended after the parallel-branch probe: turn telemetry is branch-keyed, not one slot per root (§4.1.1)** |
| **Why a spec is required** | This changes *when* POL-001 fires, which triggers the standing `docs/design/` practice |
| **Grounded against** | `langchain-core==1.5.6`, `langgraph==1.2.11`, Python 3.14.6, `sentience-governor` @ `v0.3.0.1` (`e179008`) |

---

## 1. Problem

`SentienceCallbackHandler` keeps **one** session's state in instance attributes:
`_session_id`, `_intent_emitted`, `_builder`. `on_chain_start` overwrites all
three unconditionally, and `on_chain_end` tears them down unconditionally.

LangGraph fires chain-level callbacks **once per graph and once per node**, so a
single `.invoke()` produces several starts and several ends. The result is
measured, not argued.

## 2. What CP0 established

Empirical, reproduced against the versions above. Evidence is uncommitted CP0
scratch work; the findings are restated here because the design depends on them.

**2.1 Multi-fire reproduces.** One `.invoke()` of a two-node graph produced
**3 `on_chain_start` and 3 `on_chain_end`**.

**2.2 Nested ends precede the outer end.**

```
chain_start  run=…91af  parent=None    ← root
chain_start  run=…75f3  parent=…91af   ← node
chain_end    run=…75f3  parent=…91af   ← nested end, while the root is still open
...
chain_end    run=…91af  parent=None    ← root end, last
```

A start-only guard would therefore ship an incomplete fix: the first nested end
tears the session down mid-run.

**2.3 The false POL-001 is real, and isolated by control.**

| Case | sessions | POL-001 |
| :-- | --: | --: |
| Flat chain, tool **in** capabilities | 1 | **0** |
| **Nested** chain, tool **in** capabilities | 2 | **1** ← false |
| Flat chain, tool **not in** capabilities | 1 | 1 ← true positive |

Identical tool and capabilities; the only variable is the nested start.

**2.4 `run_id` and `parent_run_id` arrive reliably.** Present on every callback.
Across 1-node, 2-node and nested-subgraph shapes, **every nested start carried a
`parent_run_id`** and each shape had **exactly one** root.

**2.5 One handler can receive two overlapping legitimate roots.** Demonstrated
for threads and for `asyncio.gather` over `.ainvoke()`. Under async both roots
started before either node ran. Roots are distinct and each nested event points
at its own root.

**2.6 Ancestry is complete.** Under overlapping roots, **every** event walked to
its correct root via `parent_run_id`, with **zero unroutable events**.

**2.7 Tool events parent to the NODE, not the root.**

```
chain_start  LangGraph   run=d2ca…  parent=None
chain_start  n           run=6f2f…  parent=d2ca…
tool_start   probe_tool  run=6f35…  parent=6f2f…   ← parent is the node
```

**A one-hop check is therefore insufficient. Routing requires an ancestry walk.**

## 3. Why the plan's guard is withdrawn

The plan proposed, as belt and braces, `if self._root_run_id is not None: return`.
CP0 replayed the **real captured event stream** of two overlapping roots through
that guard:

```
legitimate root runs SILENTLY SWALLOWED: 1   → no session, no AGENT_REGISTERED
root teardowns that never matched:       1
```

A set `_root_run_id` means *either* nested *or* a second legitimate root, and the
callback cannot distinguish them. **`_root_run_id` must not be used as a nesting
test.**

**The good news from 2.4:** `parent_run_id is not None` is a reliable nesting
test on its own, so the guard gets simpler, not more complex.

## 4. Design — per-root state

### 4.1 State

Replace the three single-slot attributes with two maps and a lock.

```python
@dataclass
class _RootState:
    session_id: str
    builder: EventBuilder
    intent_emitted: bool = False
    # v0.2.3 Track 2 per-LLM-turn state. Keyed BY BRANCH, not one slot
    # per root — see §4.1.1. `None` key is the root's default scope.
    turn_scopes: Dict[Optional[UUID], _TurnState] = field(default_factory=dict)


@dataclass
class _TurnState:
    usage: Dict[str, Optional[int]] = field(
        default_factory=lambda: {f: None for f in CANONICAL_TOKEN_FIELDS})
    model: Optional[str] = None
    provider: Optional[str] = None
    turn_id: Optional[str] = None

self._roots: Dict[Key, _RootState]   # root key -> that root's governance state
self._ancestry: Dict[UUID, Optional[UUID]]  # run_id -> parent_run_id
self._lock: threading.RLock
```

**All four `_pending_llm_*` attributes move into `_RootState`.** They are
per-execution mutable state, not just session state, and the existing source
already says so:

> *"Concurrency assumption (per plan §3.0.1): one handler instance per agent run
> / session. Sharing a single instance across concurrent runs would cause
> cross-run leakage of pending state."* — `langchain_adapter.py:87-92`

Leaving them on the instance would fix session attribution while leaving **token
usage, model, provider and `llm_turn_id` free to cross between overlapping
roots**, which is the same defect wearing different clothes. The source suggests
`contextvars`; per-root keying is preferred here because we already have an
authoritative root identity from `run_id`, and it works identically for threads
and asyncio without depending on context propagation.

**No instance-level mutable execution state survives.** After this change the
handler's instance attributes are configuration only (`_agent_id`, `_sm`,
`_cache`, `_sink`, `_deployment_mode`, `_agent_version`, `_vendor_id`,
`_declared_capabilities`, `_owner_claim`) plus the two maps and the lock.

`Key` is the root's `run_id`, or the sentinel `_LEGACY_ROOT` (§4.5).

**Every mutation of either map is taken under `self._lock`.** `RLock` covers
threads; asyncio callbacks on one loop cannot interleave mid-method, and the lock
is uncontended there.

### 4.1.1 Why turn state is branch-keyed, not one slot per root

**Measured 2026-08-18.** An earlier revision of this spec put one
`pending_llm_*` slot on `_RootState`. **That is insufficient**, and the probe is
recorded here because the conclusion is not obvious.

**Two parallel nodes inside ONE root have overlapping LLM turns**, in both
`.invoke()` and `.ainvoke()`. Fan-out (`START → A`, `START → B`) executes
branches concurrently; sync uses threads, async uses the event loop. Captured:

```
  ms  kind         run_id        parent
   1  chain_start  A             ROOT
   1  LLM_START    A-turn        A          ← A's turn opens
   1  chain_start  B             ROOT
 106  LLM_START    B-turn        B          ← B's turn opens INSIDE A's
 162  LLM_END      B-turn        B          ← B ends first
 162  tool_start   B-tool        B
 457  LLM_END      A-turn        A
 457  tool_start   A-tool        A
```

**Max simultaneously-open LLM turns: 2.** Overlap `True` for sync and async.

Replaying that exact stream through the two candidate models:

| Model | B-tool | A-tool |
| :-- | :-- | :-- |
| **One slot per root** | correct | **CONTAMINATED**: carries **B's `turn_id`** with A's usage and A's model |
| **Branch-keyed scopes** | correct | correct |

The contamination is subtle and therefore worse than a crash: A's event is
**internally inconsistent but well-formed**. It happens precisely because
`on_llm_end` deliberately does not regenerate the turn id (the existing
persistence contract), so A's end writes usage into a slot B had already re-keyed.

**Key choice.** An LLM turn and the tool calls of the same branch **share a
`parent_run_id`** — the node's `run_id`. They are siblings, not
ancestor/descendant, so the branch is the correct scope key, not the LLM
`run_id`.

**Resolution for a callback carrying `parent_run_id = p`:**

```
scope_key(p):
    node = p
    hops = 0
    while node is not None and node not in root.turn_scopes and hops < MAX_HOPS:
        node = self._ancestry.get(node)
        hops += 1
    return node if node in root.turn_scopes else None    # None = root default scope
```

Walking up handles a tool nested deeper than its node (tool inside a sub-chain).
Falling back to the `None` key preserves flat-chain and legacy behaviour exactly:
one root, one branch-less scope, identical to today.

**Lifetime.** A branch's scope is dropped when that branch's `on_chain_end`
fires, and everything is dropped with the root. No new eviction policy (§4.6).

**Architecture unchanged.** Sessions, roots, ancestry and teardown are exactly as
accepted in the preceding revision of this spec. This amends only where turn
telemetry is stored.

### 4.2 Root resolution

```
resolve_root(run_id, parent_run_id):
    node = parent_run_id if parent_run_id is not None else run_id
    hops = 0
    while node is not None and node not in self._roots and hops < MAX_HOPS:
        node = self._ancestry.get(node)
        hops += 1
    return self._roots.get(node)          # None if unresolvable
```

`MAX_HOPS` (proposed 64) bounds a malformed or cyclic chain. Walking terminates
at a **known root**, which is what makes 2.7 safe.

### 4.3 Callback behaviour

| Callback | Rule |
| :-- | :-- |
| `on_chain_start`, `parent_run_id is None` | **New root.** Record ancestry, create `_RootState` (new session id, `init_session`, new `EventBuilder`), emit registration/intent exactly as today |
| `on_chain_start`, `parent_run_id is not None` | **Nested.** Record ancestry **only**. No session, no builder, no teardown |
| `on_chain_end`, `run_id in self._roots` | **Root end.** Tear down **that root only**: `session_closed`, `session_end`, `clear_session`, then drop its `_RootState` and prune its ancestry subtree |
| `on_chain_end`, otherwise | **Nested end.** No teardown. This is defect 2.2 |
| `on_tool_*`, `on_llm_*` | `resolve_root(...)`; operate on **that root's** state |

### 4.3.1 Per-callback operation on resolved root state

Every one of these resolves a root **first** and then operates only on that
root's `_RootState`. None of them reads or writes instance-level mutable state.

| Callback | Operation |
| :-- | :-- |
| `on_llm_start` | Resolve root, then `scope_key(parent_run_id)`. Create/reset **that branch's** `_TurnState` with a **new** `turn_id`. Another branch's turn is untouched, in this root or any other |
| `on_llm_end` | Resolve root, then `scope_key(parent_run_id)`. Populate **that branch's** `usage`, `model`, `provider`. **Does not regenerate `turn_id`** — the persistence contract is preserved, now per branch. This is exactly the step that contaminated the single-slot model |
| `on_tool_start` | Resolve root, then `scope_key(parent_run_id)`. Emit through **that root's** builder, attaching **that branch's** token kwargs. Back-fill intent via `_emit_intent(root, None)` if that root has not emitted one |
| `on_tool_end` | Resolve root. Operate on that root's builder only |
| `_token_kwargs` | **Signature changes to take the resolved turn state**: `_token_kwargs(turn)`. Reads `turn.usage / model / provider / turn_id`. It must never reach for `self.` state or guess a branch |
| `_emit_intent` | **Signature changes to take the resolved root**: `_emit_intent(root, stated_objective)`. The early return tests `root.intent_emitted`, not an instance flag, and sets it on that root |

**The `_emit_intent` change is what actually fixes the reported bug.** Today the
early return reads instance state that survives the session reset, so a
newly-created session can never receive a baseline. Per-root, a new root starts
with `intent_emitted = False` and gets its own baseline.

**Ordering contract preserved.** If `on_tool_start` fires before `on_llm_end`
has populated usage, the emitted event carries that root's turn id (when
`on_llm_start` ran) and no token fields. Already-emitted events are never
mutated. This is unchanged, only root-scoped.

**Requirement 4 is satisfied structurally:** teardown is keyed on the ending
root, so it can only remove that root's entry.

### 4.4 Unresolvable events

If `resolve_root` returns `None` for a tool event (no matching root, e.g. a tool
invoked outside any chain), the handler **must not invent a session**. It
no-ops for that event. Silently attaching to an arbitrary root would produce
false attribution, which is the class of defect this release exists to remove.

### 4.4.1 `SentienceMiddleware` — binding, and what is scoped out

**Grounded read of the current implementation** (`langchain_adapter.py:435-581`):

1. It keeps its own per-step state (`_current_step_usage / model / provider /
   turn_id`), with the **same** documented caveat: *"one middleware instance per
   agent run. Sharing across concurrent runs would clobber this state."* (`:461-463`)
2. `awrap_tool_call` **snapshots the handler's `_pending_llm_*`, overwrites them
   from step state, calls the handler, then restores** (`:492-501`, `:581`).
3. It calls **`self._handler.on_tool_start(serialized, input_str)` and
   `on_tool_end(...)` with no `run_id` and no `parent_run_id`** (`:503`, `:506`).

Point 3 is the hazard: under §4.3 those calls would resolve to `_LEGACY_ROOT`
**by accident**, so middleware-generated tool events would land in a root that
has no session while the real root ran beside it. Point 2 breaks outright once
`_pending_llm_*` live on `_RootState` rather than the handler.

**Binding rule for middleware-generated tool events.** The handler resolves them
by *active-root count*, never by silent fallback:

| Active roots at the time of the middleware call | Binding |
| --: | :-- |
| **Exactly 1** | Bind to that root. This is the supported single-run case and reproduces today's behaviour |
| **0** | `_LEGACY_ROOT` (§4.5). Preserves direct-call and existing-test behaviour |
| **2 or more** | **Ambiguous. Do not guess.** Emit `GOVERNANCE_ERROR`, no-op the governance event, let the tool call proceed. Attributing to an arbitrary root is the defect class this release removes |

**State mirroring is re-specified.** The middleware must stop writing
`self._handler._pending_llm_*`, which will no longer exist. It supplies its step
state to the resolved root through a narrow internal entry point on the handler
rather than reaching into attributes, keeping snapshot/restore semantics
identical so a turn id cannot outlive the step.

**Middleware concurrency is explicitly SCOPED OUT of v0.3.0.2.** The middleware
has no callback ids at all and its own per-step state has the same single-run
assumption, so making it concurrency-safe is a separate redesign (the plausible
shape is `contextvars`, or threading run ids through `awrap_step` /
`awrap_tool_call`, both larger than this patch). **v0.3.0.2 preserves existing
supported middleware behaviour and no more.**

**Requirement:** regression tests prove v0.3.0.2 does not break the middleware
path that works today (§6, tests 12-14).

### 4.5 Legacy path — behaviour-preserving

The 32 existing adapter tests call the callbacks **without** `run_id` or
`parent_run_id`. Under §4.3 those would have `run_id is None`.

**Rule:** when `run_id is None`, the key is the sentinel `_LEGACY_ROOT`, and
`on_chain_start` replaces that entry unconditionally, exactly as today. All
lookups fall back to `_LEGACY_ROOT` when no run ids are supplied.

**This makes today's behaviour the explicit legacy branch rather than an
accident**, and is what lets the 32 tests pass unmodified.

### 4.6 Lifetime

`_ancestry` is pruned when its root tears down, and the root's `_RootState` is
dropped at the same time. Normal completion therefore reclaims everything.

**No eviction policy in v0.3.0.2.** An earlier draft proposed `MAX_TRACKED_ROOTS`
with oldest-first eviction. **Removed.**

- **The reproduced bug does not require it.** Nothing in §2 involves an abandoned
  root, and eviction cannot fix a false POL-001.
- **Evicting an active root would reintroduce the defect class this patch
  removes**: it force-closes a session that is still legitimately running, which
  is precisely what the unguarded `on_chain_end` does today.
- A bound whose failure mode is "silently discard live governance state" is worse
  than an unbounded map in a process that normally tears roots down correctly.

**Recorded as follow-up hardening, not this release:** abandoned-root cleanup for
a long-lived process whose graphs crash without firing `on_chain_end`. That needs
its own design (idle timeout? explicit reset API? weak references?) and its own
evidence that it happens in practice. **Do not add it here.**

## 5. Requirements coverage

| # | Requirement | How |
| --: | :-- | :-- |
| 1 | One session per root invocation | Sessions are created only on `parent_run_id is None` (§4.3) |
| 2 | Nested starts/ends never create or tear down | Nested start records ancestry only; nested end is a no-op (§4.3) |
| 3 | Tool events resolve correctly under overlap | Ancestry walk (§4.2), grounded by 2.6 and 2.7 |
| 4 | Ending one root cannot alter another | Teardown keyed on the ending root (§4.3) |
| 5 | Sequential reuse correct | Root A's entry is removed at its end; root B creates its own |
| 6 | Sync and async overlap | Same map, `RLock`; both shapes grounded in 2.5 |
| 7 | Flat undeclared-tool POL-001 stays a true positive | `_eval_scope` untouched; a correctly-baselined session still fires POL-001 on an out-of-scope tool |
| 8 | No policy/cache/schema change | Confined to `wrapper/langchain_adapter.py`. **No change to `_eval_scope`, policy semantics, cache schema, or event schema** |
| + | **No cross-root leakage of execution state** | Turn telemetry lives in branch-keyed `_TurnState` under `_RootState` (§4.1, §4.1.1, §4.3.1); tests 12-16 |
| + | **No cross-BRANCH leakage inside one root** | Branch-keyed scopes (§4.1.1), measured; tests 21-23 |
| + | **Middleware binds deliberately** | Active-root-count rule (§4.4.1); never a silent `_LEGACY_ROOT` fallback; tests 17-20 |

## 6. Test obligations for CP2

Written before implementation, must fail first.

1. Nested start does **not** create a second session; tool call fires **no**
   POL-001 (the 2.3 false positive).
2. Nested end does **not** tear down; a later tool call is still governed (2.2).
3. **Two overlapping roots**, threaded: two sessions, each tool attributed to its
   own root, neither swallowed.
4. **Two overlapping roots**, `asyncio.gather` over `.ainvoke()` (2.5).
5. Ending root A leaves root B fully live.
6. Sequential reuse: A completes, B roots cleanly with a new session.
7. Deep ancestry: tool under a nested subgraph resolves to the true root (2.7).
8. Unresolvable tool event no-ops rather than attaching (§4.4).
9. Flat chain, undeclared tool: POL-001 **still fires**.
10. No `run_id`/`parent_run_id` supplied: byte-identical to today (§4.5).
11. All **32** existing adapter tests green and **unmodified**.

**Per-root execution state (correction 1):**

12. **Two overlapping roots cannot exchange token usage.** Root A's
    `on_llm_end` populates A's usage; B's tool event must carry **B's** usage (or
    all-None), never A's.
13. **Model and provider do not cross roots** under the same interleaving.
14. **`llm_turn_id` does not cross roots.** A's turn id must never appear on an
    event attributed to B.
15. `on_llm_start` on root A does **not** reset root B's pending state.
16. Turn-id persistence still holds **within** a root: `on_llm_start` then two
    tool events then `on_llm_end` share one turn id.

**Middleware (correction 2):**

17. **Single active root:** middleware tool events bind to that root, and the
    emitted events match today's output. Regression, must not change.
18. **Zero active roots:** middleware tool events use `_LEGACY_ROOT`; behaviour
    identical to today.
19. **Two or more active roots:** middleware tool event emits `GOVERNANCE_ERROR`
    and no governance event, and **never** silently lands in `_LEGACY_ROOT` or an
    arbitrary root.
20. Middleware snapshot/restore still prevents a step turn id outliving its step.

**Parallel branches within one root (§4.1.1):**

21. **Two parallel nodes with overlapping LLM turns**: each branch's tool event
    carries **its own** `turn_id`, usage, model and provider. Reproduces the
    captured interleaving where B opens inside A and closes first.
22. Same, under `.ainvoke()`.
23. **Tool nested deeper than its node** resolves to the correct branch scope via
    the ancestry walk, not the root default.

## 7. Docstring correction

The class docstring reads as fully general ("Attach as a callback handler to a
LangChain agent or chain"). It must state that concurrent reuse of one handler
across overlapping invocations **is** supported, and that per-root state is why.

## 8. Rejected alternatives

| Alternative | Why rejected |
| :-- | :-- |
| Single `_root_run_id` guard (the plan's sketch) | Silently swallows a second legitimate root. Measured, §3 |
| Declare concurrent reuse unsupported | The framework permits it and does not warn; the failure would be silent misattribution |
| One-hop `parent_run_id` check for tools | Tool events parent to the **node** (2.7), so one hop resolves to a node, not a root |
| Handler-per-run, documented | Cannot be enforced from inside the handler; existing integrations already share one |
| Root-scope only session state, leave `_pending_llm_*` on the instance | Fixes attribution while token usage, model, provider and turn id still cross roots. Same defect, different field |
| `contextvars` for pending LLM state | Suggested by the existing source comment. Rejected for this patch: we already have an authoritative root identity from `run_id`, and per-root keying behaves identically for threads and asyncio without depending on context propagation through LangChain's dispatch |
| Let middleware tool events fall into `_LEGACY_ROOT` | Silent misattribution while a real root is live. This is the defect class the release removes |
| Make middleware concurrency-safe in v0.3.0.2 | It has no callback ids and its own per-step state carries the same assumption. A separate redesign; scoped out (§4.4.1) |

## 9. Open, deliberately

- **`MAX_HOPS`** (proposed 64) is a malformed-chain bound only. CP2/CP3 may tune
  it; it does not affect correctness of the model.
- **Abandoned-root cleanup** is deliberately **not** in this release (§4.6).
  Follow-up hardening, needs its own design and its own evidence that it occurs.
- **Middleware concurrency** is deliberately **not** in this release (§4.4.1).
- **Other LangChain runtimes** (AgentExecutor, LCEL chains) were not exercised at
  CP0. The design does not depend on LangGraph specifics, only on
  `run_id`/`parent_run_id`, but that generality is **untested** and must not be
  claimed in release notes.
