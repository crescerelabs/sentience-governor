# Pydantic AI integration

Sentience Governor's Pydantic AI support ships as a **separate
distribution**, `pydantic-ai-governor`, not as part of the core package.

```bash
pip install pydantic-ai-governor
```

```python
from pydantic_ai import Agent
from pydantic_ai_governor import SentienceGovernor

agent = Agent("openai:gpt-4o", capabilities=[SentienceGovernor()])
```

Every run of that agent opens its own Sentience Governor session and writes a
trace to `~/.sentience/traces/pydantic-ai/<run_id>.jsonl` in the normal
Sentience Governor format. Analyzer compatibility was verified against the
analyzers its test suite exercises: `compute_pulse`, the undeclared-intent
analysis, and the token and tool attribution paths those cover.

## Why a separate distribution

The dependency runs one way. `pydantic-ai-governor` depends on
`sentience-governor`; core acquires no Pydantic AI dependency, mandatory or
optional. Installing Sentience Governor never pulls in Pydantic AI, and the
two distributions are versioned and released independently.

## Where the documentation lives

The integration's own README is the reference, and it is the page published
with the distribution:
[`integrations/pydantic-ai-governor/README.md`](https://github.com/crescerelabs/sentience-governor/blob/main/integrations/pydantic-ai-governor/README.md).

It covers declaring a run's objective and scope, classifying tools, what
happens when a classification is missing, session and concurrency behavior,
what evidence is produced, and what that evidence does and does not prove.
