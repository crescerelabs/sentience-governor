# Contributing to Sentience Governor

Thanks for being here. This document says plainly what we want help with,
what needs agreement first, and why, so you do not spend a weekend on something
that was never going to merge.

## The short version

Sentience is a governance tool. Its value depends on users being able to trust
the meaning of its output as evidence. That makes the project deliberately
asymmetric:

- **Reach is open.** New harnesses, adapters, integrations, examples,
  reporting and developer-experience work, docs, and reproducible bug fixes
  are genuinely wanted.
- **Semantics are owned.** What counts as a violation, what an event means,
  and when enforcement fires are maintainer-governed decisions.

## Project roles

Contributions are open to everyone through issues and pull requests.

The project also has approved contributors. They may be employees of
Crescere Labs or trusted external contributors and may regularly triage issues,
review pull requests, and contribute within agreed areas of the codebase.

Approved contributor status does not grant authority to merge into the
protected branch, change governance semantics without prior agreement, publish
packages, or create releases.

The project currently has one maintainer. The maintainer owns final technical
decisions, repository settings, merges, releases, and package publication.
Additional maintainers may be appointed later through an explicit trust and
access decision.

## Welcome, and likely to merge

Sentience only becomes a broadly useful trust screen when it can sit across
many agent runtimes and its evidence is easy to use. These are the categories
that get it there:

- **Integrations and adapters**: a new agent framework, harness, or
  transport. This is the highest-value contribution and where the project
  is deliberately thin.
- **Harness support**: better session identification, resumption, or
  transcript handling for an environment we handle poorly.
- **Examples and trust-screen recipes**: runnable examples showing how to
  govern common workflows, such as coding agents, customer-support agents,
  database agents, browser agents, and internal operations. These should use
  existing semantics rather than introduce new core rules.
- **Reporting and developer experience**: better local reports, trace viewers,
  export tools, setup diagnostics, and workflow improvements that make the
  existing governance evidence easier to understand and use, without changing
  what it means.
- **Reproducible bug fixes**: a fix accompanied by a failing test that
  passes after the change.
- **Documentation**, including "this was confusing." Confusion in a
  governance tool is a defect.
- **Performance and portability**, where behavior is unchanged.
- **Test coverage** for existing behavior.

In short: **help Sentience work in more places, become easier to install, and
make its evidence easier to use.** Discuss first before changing what the
evidence or the rules mean.

## Maintainer-governed changes

The following areas require an issue, design agreement, and explicit maintainer
approval before implementation. This is not because outside ideas are
unwelcome. A downstream operator, reviewer, or auditor must be able to trust
that governance meaning did not change quietly.

- Policy semantics: what constitutes a violation, including its code and
  severity
- The event schema and field meanings in `sentience_governor/schema/`
- Event construction in `sentience_governor/event_builder/`
- Profile schema and reserved slots in `sentience_governor/profile/`
- Enforcement behavior, including anything touching fail-open
- Session identity and continuity

Approved contributors may work in these areas after the proposed behavior has
been agreed in an issue. What will not work is arriving with governance
semantics already changed and expecting the pull request itself to serve as
the design discussion.

**On policy rules specifically.** The rules shipped as authoritative defaults
stay maintainer-governed. New default policies and community-authored
governance rules are not broadly invited into core yet. Experimental policy
work is welcome as a clearly-labelled example under
[examples/](examples/), where it demonstrates an approach without shipping as
an authoritative default.

**Corollary:** a pull request that makes a test pass by weakening what the test
asserts about governance will be declined. If the assertion is wrong, explain
the behavioral issue first and make that the proposed change.

## Before you open a PR

1. Open an issue first for anything beyond a typo or an obvious fix.
2. Run `make test` before pushing. CI runs it again.
3. Run `python3 scripts/check_public_surface.py`. CI runs this too.
4. New behavior comes with a test. Bug fixes come with a test that fails
   without the fix.
5. Keep the pull request to one concern.
6. Follow the surrounding code, tests, and documented public behavior.

## Developer Certificate of Origin

All contributions require a DCO sign-off. Add `-s` to each commit:

```bash
git commit -s -m "your message"
```

This appends `Signed-off-by: Your Name <your@email>`, certifying that you have
the right to submit the contribution under the project's license. Read the
full text at <https://developercertificate.org/>.

DCO sign-off is currently checked manually during review. A pull request with
unsigned commits will be asked to amend them before merge.

## Review and merge

The project currently has one maintainer. Approved contributors may review and
recommend changes, but only the maintainer can merge into the protected branch,
create releases, or publish the package.

Required CI and maintainer review against the acceptance criteria are the final
merge gates.

Review can take a few days. If a pull request goes quiet for two weeks, a
polite nudge on the thread is welcome.

## Reporting bugs

Use the issue templates. The most useful evidence is a minimal trace excerpt
showing the events around the problem.

Never attach a raw trace from a production or client system. Traces can contain
prompts, tool arguments, credentials, identifiers, and filesystem paths.
Provide a minimal redacted excerpt or, preferably, reproduce the problem using
synthetic data.

## Security

Do not report vulnerabilities in a public issue. See [SECURITY.md](SECURITY.md).

## License

Contributions are licensed under **Apache License 2.0**, the same as the
project. See [LICENSE](LICENSE).
