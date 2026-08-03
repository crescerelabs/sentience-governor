## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- Link the issue. Anything beyond a typo should have one:
     Fixes #NNN / Refs #NNN -->

## Does this change governance semantics?

Governance semantics are maintainer-governed: what counts as a violation, what
an event field means, when enforcement fires, and how sessions are identified.
These need an issue and design agreement before implementation.
See CONTRIBUTING.md, "Maintainer-governed changes".

- [ ] **No.** Behavior of the governance output is unchanged.
- [ ] **Yes**, and the behavior was agreed in the linked issue first.

If yes, describe the before and after precisely:

<!--
Before: <event/violation/enforcement behavior>
After:  <event/violation/enforcement behavior>
-->

## Evidence

<!-- For a bug fix: the trace excerpt or failing test that showed the bug,
     and the same case passing after. Redact anything sensitive.
     For an integration: what a governed session looks like through it. -->

## Checklist

- [ ] `make test` passes locally
- [ ] `python3 scripts/check_public_surface.py` passes locally
- [ ] New behavior has a test; a bug fix has a test that fails without the fix
- [ ] Commits are signed off (`git commit -s`) per the DCO in CONTRIBUTING.md
- [ ] This PR covers one concern
