⚠ Internal documentation for website maintainers

# Customer-facing docs (`docs/website/`)

Source markdown for the public documentation at getsentience.ai. Edit here; the website build pulls from this directory.

## Authorial stance

- **Audience:** cold arrival from getsentience.ai, no repo context, wants to install and use.
- **No GitHub references.** No repo links, no issue-tracker links, no "see the source" callouts.
- **Public docs should read as shipped product docs.** Keep pre-release guidance hidden from normal navigation.
- **Install → first success ≤ 5 minutes.**
- Every page must answer:
  → What do I do next?

## File index

| File | URL path |
| :-- | :-- |
| `index.md` | `/docs` |
| `install.md` | `/docs/install` |
| `quickstart.md` | `/docs/quickstart` |
| `commands.md` | `/docs/commands` |
| `sync-privacy.md` | `/docs/sync-privacy` |
| `troubleshooting.md` | `/docs/troubleshooting` |
| `changelog.md` | `/docs/changelog` |

## Integration notes

- Add site-generator frontmatter at build time (title, slug, nav order).
- Internal links are relative `./filename.md` — adjust for the site's URL scheme.
- On new version: update `changelog.md`, `install.md` version pin (if any), `commands.md` if the CLI surface changed.

---

Stuck? → [Troubleshooting](./troubleshooting.md)
