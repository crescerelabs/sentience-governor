# Undeclared-Intent Spend — session `high-und...`

- Status: `ok`
- Total turns: 6
- Undeclared turns: 3

## Headline

| Metric | Tokens | Share |
|---|---:|---:|
| Total compute | 7,900 | 100.0% |
| Undeclared | 4,040 | 51.1% |
| Declared | 3,860 | 48.9% |

## Undeclared turns

| Turn | Tool(s) | Tokens | Reason(s) |
|---|---|---:|---|
| `turn-4...` | slack.write_message | 1,130 | INTENT_MISSING, POL-001 |
| `turn-5...` | postgres.execute | 1,660 | INTENT_MISSING, POL-001 |
| `turn-6...` | crm.update_contact | 1,250 | INTENT_MISSING, POL-001 |

## Operational interpretation

4,040 tokens were attributed to turns that touched execution outside this session's declared operational intent. Were these valid and expected? If not, policy can intervene at the execution boundary — review, constraint, confirmation, or block.

---

This is one session. A consolidated view across all your runs — drift trends, compute attributed across workflows, agent behavior compared over time — is what's next. If you'd find it useful, the most helpful thing is hearing what it should answer.

Reply: <operators@crescerelabs.com>

Or stay informed: <https://getsentience.ai/launch-list>

