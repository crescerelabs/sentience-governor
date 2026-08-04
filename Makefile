# Makefile — developer conveniences for sentience-gov-build.
#
# Targets:
#   test            Run the full Python test suite. Fast, offline.
#   release-check   Run the mechanically-checkable release gates
#                   (scripts/release_check.py). A release gate is FALSE until
#                   this prints PASS for it — do NOT tick the Release Gates
#                   table from memory or partial evidence. Paste the output.
#                   Fast pass (skip the wheel build): make release-check ARGS=--no-build
#   fresh-resolve   Standing gate (v0.3.0.1): build a wheel, then install it into
#                   genuinely fresh venvs — base and every declared extra — with
#                   NO tester-supplied pins, and run a feature smoke test for
#                   each. Catches an upstream release that breaks a declared
#                   extra, which is how v0.3.0 shipped a dead MCP server past
#                   every other gate. NETWORK-DEPENDENT, so it is deliberately
#                   its own target and not part of `release-check`.
#                   Required before a release. Run it and paste the output.
#
# Removed in v0.2.8.3: the `acceptance-live` target (and its preflight) drove the
# Sentience Sync cloud-telemetry CLI end-to-end against sync.getsentience.ai.
# That surface was SUNSET in v0.2.8.3 (local-first cleanup), so the target was
# removed. Recoverable from git history if a paid cloud plane ever revives it.

.PHONY: test release-check fresh-resolve

# Each recipe resolves a Python with the project's deps — preferring the in-repo
# venv — in the shell, so it stays portable across BSD make (macOS) and GNU make.

test:
	@PY=$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3); \
	 $$PY -m pytest

release-check:
	@PY=$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3); \
	 $$PY scripts/release_check.py $(ARGS)

fresh-resolve:
	@PY=$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3); \
	 $$PY scripts/fresh_resolve_check.py $(ARGS)
