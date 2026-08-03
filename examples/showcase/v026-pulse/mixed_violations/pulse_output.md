# Sentience Pulse — session `demo-v02...`

- Status: `ok`
- Total events: 7
- Total turns: 3
- Duration: 0s
- Profile: `5c3ce82958dd` (schema v1, loaded)

## Undeclared-intent spend

| Metric | Tokens | Share |
|---|---:|---:|
| Total compute | 4,800 | 100.0% |
| Undeclared | 1,680 | 35.0% |
| Declared | 3,120 | 65.0% |
| Cached read | 0 | 0.0% |
| Cached write | 0 | 0.0% |
| Prompt | 4,000 | 83.3% |
| Completion | 800 | 16.7% |

_Per-turn usage is deduped by requestId._

_Why it matters: this is where agent work became harder to attribute._

### Tool calls

| Operation | Calls |
|---|---:|
| Execute | 0 |
| Read | 1 |
| Write | 2 |
| Delete | 0 |
| **Total** | **3** |

_Top tools by call count: fs.write (2), fs.read (1)._

### Tool-token attribution

**Tokens on turns that fired ≥1 tool call:** 0 (0.0% of total)


## Policy-violation burn rate

Compute associated with turns where policy rules fired.

- 3 violation-firing turns · 4,800 tokens

| Rule | Turns | Tokens | Description |
|---|---:|---:|---|
| `POL-001` | 1 | 1,680 | Declare intent before executing mutating operations |
| `POL-003` | 1 | 2,400 | Vendor should tag tool responses with classification metadata |
| `POL-005` | 1 | 720 | Provide explicit authorization before escalating context sensitivity |

> By-rule token totals are not additive when multiple rules fire on the same turn. Sum the by-rule rows and you may exceed violation_associated_tokens or even total_tokens — that is expected, not a bug. Top-level violation_associated_tokens counts unique violation-firing turns only.

_Why it matters: `POL-003` appeared on turns representing 2,400 tokens. This is the first rule to inspect if you want tighter agent boundaries._

## Advisory flags

| Flag | Count |
|---|---:|
| `CONTEXT_UNCLASSIFIED` | 1 |
| `HIGH_CONSEQUENCE_DETECTED` | 1 |
| `SENSITIVITY_ESCALATION` | 1 |

_Why it matters: triggered 1 high-consequence operation. Review the trace for context._

---

_Want this pulse delivered weekly via email?_  
_Join the list: [getsentience.ai/sentience-sync](https://getsentience.ai/sentience-sync)_
