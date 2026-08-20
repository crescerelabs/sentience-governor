⚠ Internal note for documentation maintainers

# Documentation (`docs/`)

This directory is the canonical home of the user documentation. It is read
on GitHub, so write for someone reading it there: relative links between
pages, no build step, no site-generator frontmatter.

## Authorial stance

- **Audience:** someone who has just found the project and wants to install
  it and get a first trace. Assume no prior context.
- **Install → first success ≤ 5 minutes.**
- Every page should answer: *what do I do next?*
- Link to source or issues only where it genuinely helps the reader. These
  pages live in the repository, so a pointer to `examples/` or to a module
  is fair game — it is no longer a foreign reference.

## File index

| File | What it is |
| :-- | :-- |
| `index.md` | Entry point and navigation |
| `install.md` | Install instructions |
| `quickstart.md` | Zero to first trace |
| `commands.md` | CLI reference |
| `profile.md` | Governance profiles |
| `troubleshooting.md` | Common issues |
| `changelog.md` | User-facing release notes |
| `integrations/langchain.md` | LangChain / LangGraph |
| `integrations/mcp.md` | MCP-style clients |
| `guide/sentience_governor.md` | The full operator manual |
| `guide/README.md` | Index for the manual |
| `install-pre-release.md` | **Deliberately unlinked** from `index.md`; handed to testers directly |
| `ARCHITECTURE.md` | Architecture reference |
| `design/` | Design records for individual changes |

## Two changelogs, on purpose

| File | Audience | Ships in the wheel? |
| :-- | :-- | :-- |
| Root `CHANGELOG.md` | Developers, PyPI. **Canonical factual record**, Keep a Changelog format | Yes |
| `docs/changelog.md` | Users. Narrative release notes | No |

`docs/changelog.md` may word things differently, but it must never
contradict the root file, and it must never describe a removed surface as
though it still exists. `release_check.py` enforces both: an entry for the
current version must exist here, and the removed-surface scan covers these
pages.

## On a new release

1. Add the version to root `CHANGELOG.md`.
2. Add a matching entry to `docs/changelog.md`.
3. Update `commands.md` if the CLI surface changed.
4. Update the version banners in `guide/README.md` and `guide/sentience_governor.md` §15.
5. If a command or feature was **removed**, add its pattern to
   `scripts/release_check_forbidden.txt` so no page keeps presenting it as
   active.
6. Run `make release-check`.

---

Stuck? → [Troubleshooting](./troubleshooting.md)
