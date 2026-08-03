# Security policy

Sentience Governor is governance software. A flaw in it can cause an agent
action to go unrecorded, or a policy violation to go unreported. We treat
those as security issues, not just bugs.

## Reporting a vulnerability

**Do not open a public issue for a security report.**

Use GitHub's private reporting: the **Security** tab of this repository →
**Report a vulnerability**. That opens a private advisory visible only to
maintainers.

If private reporting is unavailable to you, email
**[security@crescerelabs.com](mailto:security@crescerelabs.com)**.

Please include:

- the security impact and the conditions required to reproduce it
- the affected version, install method, and harness: Claude Code, LangChain,
  MCP, or direct integration
- a minimal synthetic reproduction or the relevant governance event lines

Do not send a production or client trace. Redact sensitive data and see
*Handling traces* below.

## What to expect

| | |
| :-- | :-- |
| **Acknowledgement** | within 3 business days |
| **Initial assessment** | within 10 business days |
| **Fix or documented mitigation** | targeted within 90 days of confirmation |
| **Disclosure** | coordinated; we will credit you unless you ask us not to |

Sentience is currently maintained by one maintainer. These are honest targets,
not a contractual SLA. Critical or actively exploited issues will be
prioritized. If a target slips, we will communicate rather than go quiet.

## Supported versions

Security fixes are provided for the **latest released version**. There is no
long-term-support branch, and fixes are not routinely backported. If you are
pinned to an older release, expect to upgrade to receive a fix.

## In scope

- **Evidence integrity.** Anything that causes a supported agent action to be
  performed without the governance record required by the documented behavior,
  or that lets an event be forged, altered, or silently dropped across a
  boundary Sentience is expected to protect.
- **Policy evasion.** Anything that lets an agent, prompt, or tool response
  cause a policy check to be skipped or produce a result contrary to the
  documented policy semantics.
- **Enforcement bypass.** In a release with enforcement enabled, any path that
  converts a block into a permit outside the documented fail-open conditions.
- **Local data exposure.** Unintended disclosure of trace contents,
  credentials, or filesystem paths, including traces written with unexpectedly
  broad permissions.
- **Supply chain.** Anything affecting the integrity of the official source,
  build or release workflow, or published `sentience-governor` distribution.

## Out of scope

- **Documented fail-open behavior.** In releases that support enforcement,
  operation under the documented fail-open conditions is expected behavior,
  not a vulnerability. A report showing that fail-open occurs outside those
  conditions, or can be triggered unexpectedly by an agent or attacker, is in
  scope.
- **The agent's own conduct.** Sentience records and evaluates what an agent
  does; it does not make the agent trustworthy. An agent doing something
  harmful and being correctly recorded doing it is Sentience working as
  designed.
- **Direct trace modification by the local user.** A user with the same
  operating-system permissions as the process can read, modify, or delete its
  local trace files. Reports remain in scope if they demonstrate an unexpected
  privilege-boundary crossing or impact beyond this documented local trust
  assumption.
- **Upstream vulnerabilities without Sentience-specific impact.** Report the
  root cause to Anthropic, OpenAI, LangChain, or the relevant MCP SDK project.
  Sentience-specific exploitability, unsafe integration behavior, or material
  amplification remains in scope here.
- Automated scanner output with no demonstrated security impact.

## Handling traces

Governance traces can contain prompts, tool arguments, credentials,
identifiers, and file paths from real work. **Do not attach a raw trace from a
production or client system.**

Prefer a synthetic reproduction. If that is not possible, provide only the
minimal redacted event lines required to demonstrate the issue. If a real
trace is genuinely necessary, contact us first so that we can arrange an
appropriate private transfer method.

## Documented limitations

A documented limitation is not a vulnerability by itself. A new bypass,
unexpected trigger, or security consequence arising from a documented
limitation is a valid report. Reference the limitation so that we can
distinguish the known boundary from the new impact.
