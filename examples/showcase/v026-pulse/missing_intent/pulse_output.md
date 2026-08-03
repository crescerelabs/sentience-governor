# Sentience Pulse — session `demo-v02...`

- Status: `ok`
- Total events: 7
- Total turns: 3
- Duration: 0s
- Profile: `0a36e5c9f31b` (schema v1, loaded)

## Undeclared-intent spend

| Metric | Tokens | Share |
|---|---:|---:|
| Total compute | 3,960 | 100.0% |
| Undeclared | 3,960 | 100.0% |
| Declared | 0 | 0.0% |
| Cached read | 0 | 0.0% |
| Cached write | 0 | 0.0% |
| Prompt | 3,300 | 83.3% |
| Completion | 660 | 16.7% |

_Per-turn usage is deduped by requestId._

_Why it matters: every attributed turn is classified as undeclared. Often a surface-bound limitation (e.g. Claude Code today), not agent drift._

### Tool calls

| Operation | Calls |
|---|---:|
| Execute | 0 |
| Read | 0 |
| Write | 3 |
| Delete | 0 |
| **Total** | **3** |

_Top tools by call count: fs.write (3)._

### Tool-token attribution

**Tokens on turns that fired ≥1 tool call:** 0 (0.0% of total)


## Policy-violation burn rate

Compute associated with turns where policy rules fired.

- 3 violation-firing turns · 3,960 tokens

| Rule | Turns | Tokens | Description |
|---|---:|---:|---|
| `POL-001` | 3 | 3,960 | Declare intent before executing mutating operations |

_Why it matters: `POL-001` appeared on turns representing 3,960 tokens. This is the first rule to inspect if you want tighter agent boundaries._

## Advisory flags

None fired in this session.

_Why it matters: no profile-driven advisory thresholds were crossed._

---

_Want this pulse delivered weekly via email?_  
_Join the list: [getsentience.ai/sentience-sync](https://getsentience.ai/sentience-sync)_
