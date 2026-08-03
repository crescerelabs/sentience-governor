# Makefile — developer conveniences for sentience-gov-build.
#
# Targets:
#   test            Run the full Python test suite. Fast, offline.
#   release-check   Run the mechanically-checkable release gates
#                   (scripts/release_check.py). A release gate is FALSE until
#                   this prints PASS for it — do NOT tick the Release Gates
#                   table from memory or partial evidence. Paste the output.
#                   Fast pass (skip the wheel build): make release-check ARGS=--no-build
#
# Removed in v0.2.8.3: the `acceptance-live` target (and its preflight) drove the
# Sentience Sync cloud-telemetry CLI end-to-end against sync.getsentience.ai.
# That surface was SUNSET in v0.2.8.3 (local-first cleanup), so the target was
# removed. Recoverable from git history if a paid cloud plane ever revives it.

.PHONY: test release-check

# Each recipe resolves a Python with the project's deps — preferring the in-repo
# venv — in the shell, so it stays portable across BSD make (macOS) and GNU make.

test:
	@PY=$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3); \
	 $$PY -m pytest

release-check:
	@PY=$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3); \
	 $$PY scripts/release_check.py $(ARGS)
