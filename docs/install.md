# Install

```bash
pipx install sentience-governor
sentience --help
```

That's the install. Two commands. The package registers four entry points on your `$PATH`: `sentience`, `sentience-cli`, `sentience-claude-code-hook`, and `sentience-mcp-server`.

## Requirements

- Python 3.10 or newer
- pipx
- macOS, Linux, or Windows

## Verify

```bash
pipx list
```

Should show `package sentience-governor X.Y.Z`.

## Upgrade

```bash
pipx upgrade sentience-governor
```

## Uninstall

```bash
pipx uninstall sentience-governor
```

## If you don't have pipx

```bash
brew install pipx          # macOS
# or:
python3 -m pip install --user pipx   # Linux / WSL
pipx ensurepath
```

Restart your shell after `pipx ensurepath`.

## Library integration (different use case)

If you're importing `sentience_governor` as a Python module in your own code (MCP wrapper, LangChain callback, custom agent runtime), install into your project's virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install sentience-governor
```

In this mode, the CLIs are only on `$PATH` while the venv is active. Use this path **only** for library integration. For CLI-only usage, pipx is the right tool.

## First-run choices

| You want to… | Go to |
| :-- | :-- |
| See what Claude Code is actually doing | [Quickstart — Claude Code hook](./quickstart.md#recommended-claude-code-hook-fastest-way-to-see-value) |
| Wrap your own MCP or LangChain agent | [Quickstart — Advanced](./quickstart.md#advanced-agent-wrapper) |

None of these require an account.

## Remove local data (optional)

```bash
rm -rf ~/.sentience/
```

Deletes per-session traces and any configuration. Recreated on next run.

---

Stuck? → [Troubleshooting](./troubleshooting.md)
