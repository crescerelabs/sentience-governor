# Sentience Governor for Pydantic AI

**Agent Execution Evidence at the Pydantic AI runtime execution boundary.**

Governance-relevant evidence of what an agent actually dispatched at runtime,
against the objective and scope it declared. This is not logging, tracing or
observability.

> **Status: scaffolding.** The package exists and carries no capability logic
> yet. `SentienceGovernor` and the documentation for using it arrive with the
> next checkpoint. Nothing here is published.

## Relationship to `sentience-governor`

This is an independent distribution that depends on the core package. The
dependency runs one way only:

```
pydantic-ai-governor  ->  sentience-governor
                      ->  pydantic-ai-slim
```

Core acquires no Pydantic AI dependency, mandatory or optional. The two
distributions have independent versions, changelogs, artifacts, tags and
release trains, and both follow the same release discipline. Releasing one
does not require rebuilding, versioning, tagging or republishing the other.

`pydantic-ai-governor` is a distribution name following the ecosystem's
`pydantic-ai-<name>` convention. It does not imply that Pydantic owns,
operates or endorses Sentience Governor.

## Compatibility

| Requires | Range |
| :-- | :-- |
| `sentience-governor` | `>=0.3.1.2,<0.3.2` |
| `pydantic-ai-slim` | `>=2.37.0,<2.38` |
| Python | `>=3.10` |

These bounds are deliberate published compatibility contracts rather than
defaults. They are widened only after measured verification against a new
version, and only in a new release of this distribution.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
