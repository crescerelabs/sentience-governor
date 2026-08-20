# CP2 tests — v0.3.0.2 LangGraph session continuity

**Contract:** internal design spec `765ab57`,
`docs/design/0001-langgraph-root-scoped-governance-sessions.md`.

**Placement note.** These belong in `tests/` in the public repo alongside
`test_langchain_handler.py`. They are here because the public repo is off
limits (operator, 2026-08-18). They import the product via `PYTHONPATH` and
must move when public-repo work is authorised.

## Running

```bash
cd tests/v0_3_0_2_cp2
PYTHONPATH=/Users/rohit-nallapeta/sentience-governor:. \
  /Users/rohit-nallapeta/sentience-governor/.venv/bin/python -m pytest . -q
```

## Expected state at CP2 (against v0.3.0.1)

**13 failed, 9 passed.** The 13 encode reproduced defects and must fail until
CP3. The 9 are regression/compatibility and already hold.

Obligation 11 (the existing 32 adapter tests) is verified separately against
the public repo and those files are not touched.
