# Sentience Pulse — session `demo-v02...`

- Status: `ok`
- Total events: 8
- Total turns: 3
- Duration: 0s
- Profile: `5c3ce82958dd` (schema v1, loaded)

## Undeclared-intent spend

| Metric | Tokens | Share |
|---|---:|---:|
| Total compute | 3,720 | 100.0% |
| Undeclared | 0 | 0.0% |
| Declared | 3,720 | 100.0% |
| Cached read | 0 | 0.0% |
| Cached write | 0 | 0.0% |
| Prompt | 3,100 | 83.3% |
| Completion | 620 | 16.7% |

_Per-turn usage is deduped by requestId._

_Why it matters: every turn was attributable to your declared session intent._

### Tool calls

| Operation | Calls |
|---|---:|
| Execute | 1 |
| Read | 1 |
| Write | 1 |
| Delete | 0 |
| **Total** | **3** |

_Top tools by call count: Bash (1), fs.read (1), fs.write (1)._

### Tool-token attribution

**Tokens on turns that fired ≥1 tool call:** 0 (0.0% of total)


## Policy-violation burn rate

No policy violations recorded.

_Why it matters: your profile rules did not fire in this session._

## Advisory flags

| Flag | Count |
|---|---:|
| `HIGH_CONSEQUENCE_DETECTED` | 1 |
| `TASK_BOUNDARY_CROSSED` | 1 |

_Why it matters: the agent crossed into new tasks 1 time and triggered 1 high-consequence operation. Review the trace for context._

## Interpretation

Your session was observable, your profile was loaded, and no policy
violations were recorded against the rules active in this session.
That is still value. Pulse will surface more signal as your profile
tightens or agent behavior shifts. Run `sentience pulse` after each
session to track changes over time.

---

_Want this pulse delivered weekly via email?_  
_Join the list: [getsentience.ai/sentience-sync](https://getsentience.ai/sentience-sync)_
