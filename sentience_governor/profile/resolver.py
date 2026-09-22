"""Per-session policy resolution (v0.3.2).

Answers one question for a run that is about to start: *which governance
profile governs it?* Keyed on the run's ``agent_id``, the one attribute that
is on every event envelope, known at every adapter before ``session_start``,
operator- or integrator-controlled, and stable for a session.

Resolution chain, in order, with no other steps::

    matching binding  ->  default profile  ->  none

* **Binding.** ``~/.sentience/resolution.yaml`` (see
  :data:`sentience_governor.profile.loader.DEFAULT_RESOLUTION_PATH`) lists
  bindings; the FIRST whose ``agent_id`` pattern matches is authoritative.
  If its profile loads and is runtime-ready (``validate()`` reports no
  errors), the outcome is ``bound``. If it cannot be loaded (missing,
  unreadable, unparseable, non-mapping root, inadmissible content) or is
  not runtime-ready, the outcome is ``degraded``: later bindings are NOT
  consulted; the chain proceeds to the default step with the failure
  recorded. A resolution file that is
  absent, unparseable, of the wrong schema version, or malformed at the top
  level is not a matched-binding failure: it is ignored with a warning and
  the chain proceeds to the default step.
* **Default.** ``~/.sentience/profile.yaml`` if it exists, loaded as
  ``GovernanceProfile.from_default_path_or_none`` loads it. Since v0.3.2.1
  a default that cannot be loaded or is not runtime-ready does not raise
  here: it is recorded as a warning and the chain proceeds to ``none``
  (or ``degraded`` when a binding had matched). The agent runs without a
  profile and the registration records that. ``from_default_path_or_none``
  itself is unchanged, so the CLI and the MCP server's profile view keep
  raising as before.
* **None.** The pre-profile code path: no transforms, no fingerprint.

This layer never raises. Every problem it can encounter degrades to a
LESS specific outcome, is recorded in ``ResolvedProfile.warnings``, and is
logged once at warning level. Fail-open is preserved and made visible.

Pure: the result is a deterministic function of ``agent_id`` and the
contents of the two files. Nothing is cached across calls, which matters
for the Claude Code hook, where each invocation is a fresh process.

Not in this module, by design: profile authoring, inheritance,
distribution, risk tiers, per-team keys, and any per-session persistence
(the sticky binding of a later checkpoint is layered on top of this
function, not inside it).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import yaml

from sentience_governor.profile import loader as _loader
from sentience_governor.profile.loader import GovernanceProfile

logger = logging.getLogger(__name__)

RESOLUTION_SCHEMA_VERSION = 1

SOURCE_BOUND = "bound"
SOURCE_DEGRADED = "degraded"
SOURCE_DEFAULT = "default"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class ResolvedProfile:
    """The outcome of :func:`resolve_profile`.

    ``profile``
        The ``GovernanceProfile`` to bind to the session, or ``None`` for the
        pre-profile code path.
    ``source``
        One of ``bound`` / ``degraded`` / ``default`` / ``none``.
    ``binding``
        The matched ``agent_id`` pattern for ``bound`` and ``degraded``;
        ``None`` otherwise.
    ``warnings``
        Non-fatal problems encountered while resolving, in order. Empty
        when resolution was clean.
    """

    profile: Optional[GovernanceProfile]
    source: str
    binding: Optional[str]
    warnings: Tuple[str, ...]


def resolve_profile(
    *,
    agent_id: str,
    resolution_path: Optional[Path] = None,
    default_path: Optional[Path] = None,
) -> ResolvedProfile:
    """Resolve the profile that governs a run for ``agent_id``.

    ``resolution_path`` and ``default_path`` default to the standard
    locations and are parameters so the resolver can be tested and so the
    read-only CLI can run it against any pair of files.

    When ``default_path`` is not given, the default step delegates to
    ``GovernanceProfile.from_default_path_or_none()`` itself, so the
    existing entry point's ``DEFAULT_PROFILE_PATH`` lookup is reused rather
    than re-implemented; any exception it raises is caught here and
    recorded (v0.3.2.1). The two module-level paths are read at call time
    from the loader module, never bound at import.
    """
    resolution_path = (
        _loader.DEFAULT_RESOLUTION_PATH
        if resolution_path is None
        else Path(resolution_path)
    )
    warnings: List[str] = []

    # ---- Step 1: binding -------------------------------------------------
    matched_pattern: Optional[str] = None
    try:
        bindings = _load_bindings(resolution_path, warnings)
        for pattern, target in bindings:
            if not fnmatch.fnmatchcase(agent_id, pattern):
                continue
            matched_pattern = pattern
            try:
                profile = GovernanceProfile.from_file(target)
            except (
                FileNotFoundError,
                ValueError,
                TypeError,
                OSError,
                yaml.YAMLError,
            ) as exc:
                _warn(
                    warnings,
                    f"binding '{pattern}' matched agent '{agent_id}' but its "
                    f"profile could not be loaded from {target}: "
                    f"{exc.__class__.__name__}: {exc}. Resolution is degraded; "
                    "later bindings are not consulted.",
                )
                break  # first match is authoritative, even when it fails
            readiness = _loader._runtime_readiness(profile)
            if readiness.errors:
                _warn(
                    warnings,
                    f"binding '{pattern}' matched agent '{agent_id}' but its "
                    f"profile at {target} is not runtime-ready: "
                    f"{_first_errors(readiness.errors)}. Resolution is "
                    "degraded; later bindings are not consulted.",
                )
                break  # first match is authoritative, even when it fails
            return ResolvedProfile(
                profile=profile,
                source=SOURCE_BOUND,
                binding=pattern,
                warnings=tuple(warnings),
            )
    except Exception as exc:  # the new layer never raises into the runtime
        _warn(
            warnings,
            f"resolution failed unexpectedly ({exc.__class__.__name__}: {exc}); "
            "falling back to the default profile.",
        )

    # ---- Step 2: default (v0.3.2.1: fails open, never raises) ------------
    default_profile = _load_default(default_path, warnings)
    if default_profile is not None:
        return ResolvedProfile(
            profile=default_profile,
            source=SOURCE_DEGRADED if matched_pattern is not None else SOURCE_DEFAULT,
            binding=matched_pattern,
            warnings=tuple(warnings),
        )

    # ---- Step 3: none ----------------------------------------------------
    return ResolvedProfile(
        profile=None,
        source=SOURCE_DEGRADED if matched_pattern is not None else SOURCE_NONE,
        binding=matched_pattern,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Default profile
# ---------------------------------------------------------------------------


def _load_default(
    default_path: Optional[Path], warnings: List[str]
) -> Optional[GovernanceProfile]:
    """Load the default profile, or ``None`` with a warning when it is absent,
    unloadable, or not runtime-ready.

    A malformed default is a configuration error the operator must see; it
    is not a reason to stop the governed application. The warning names the
    file and the first errors; the caller resolves to ``none`` (or
    ``degraded``) and the registration records that no profile was
    activated.
    """
    shown_path = _loader.DEFAULT_PROFILE_PATH if default_path is None else Path(default_path)
    try:
        if default_path is None:
            profile = GovernanceProfile.from_default_path_or_none()
        else:
            default_path = Path(default_path)
            profile = GovernanceProfile.from_file(default_path) if default_path.is_file() else None
    except Exception as exc:  # fail open: the layer never raises into the runtime
        _warn(
            warnings,
            f"default profile could not be loaded from {shown_path}: "
            f"{exc.__class__.__name__}: {exc}. Running without a profile.",
        )
        return None
    if profile is None:
        return None
    readiness = _loader._runtime_readiness(profile)
    if readiness.errors:
        _warn(
            warnings,
            f"default profile at {shown_path} is not runtime-ready: "
            f"{_first_errors(readiness.errors)}. Running without a profile.",
        )
        return None
    return profile


def _first_errors(errors: List[str], limit: int = 3) -> str:
    shown = "; ".join(errors[:limit])
    if len(errors) > limit:
        shown += f"; and {len(errors) - limit} more"
    return shown


# ---------------------------------------------------------------------------
# Resolution file
# ---------------------------------------------------------------------------


def _load_bindings(path: Path, warnings: List[str]) -> List[Tuple[str, Path]]:
    """Return ``[(agent_id_pattern, absolute_profile_path), ...]`` in file order.

    Returns an empty list, with a warning where appropriate, for every way
    the file can be absent or malformed at the top level. Individual
    bindings that are malformed are skipped with a warning; the rest of the
    file is still used.
    """
    if not path.is_file():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(warnings, f"resolution file {path} could not be read: {exc}; ignoring it.")
        return []
    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        _warn(warnings, f"resolution file {path} is not valid YAML: {exc}; ignoring it.")
        return []
    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        _warn(
            warnings,
            f"resolution file {path} root must be a mapping; got "
            f"{type(loaded).__name__}; ignoring it.",
        )
        return []
    version = loaded.get("schema_version")
    if version != RESOLUTION_SCHEMA_VERSION:
        _warn(
            warnings,
            f"resolution file {path} has schema_version={version!r}; this runtime "
            f"understands {RESOLUTION_SCHEMA_VERSION}; ignoring it.",
        )
        return []
    entries = loaded.get("bindings")
    if entries is None:
        return []
    if not isinstance(entries, list):
        _warn(
            warnings,
            f"resolution file {path}: 'bindings' must be a list; got "
            f"{type(entries).__name__}; ignoring it.",
        )
        return []

    bindings: List[Tuple[str, Path]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _warn(warnings, f"resolution file {path}: bindings[{index}] must be a mapping; skipped.")
            continue
        pattern = entry.get("agent_id")
        target = entry.get("profile")
        if not isinstance(pattern, str) or not pattern:
            _warn(
                warnings,
                f"resolution file {path}: bindings[{index}].agent_id must be a "
                "non-empty string; skipped.",
            )
            continue
        if not isinstance(target, str) or not target:
            _warn(
                warnings,
                f"resolution file {path}: bindings[{index}].profile must be a "
                "non-empty path; skipped.",
            )
            continue
        unknown = sorted(k for k in entry if k not in ("agent_id", "profile"))
        if unknown:
            _warn(
                warnings,
                f"resolution file {path}: bindings[{index}] has unknown keys "
                + ", ".join(unknown)
                + "; the binding is still used.",
            )
        bindings.append((pattern, _expand_target(path, target)))
    return bindings


def _expand_target(resolution_path: Path, target: str) -> Path:
    """``~`` is expanded; a relative path resolves against the resolution file's directory."""
    expanded = Path(target).expanduser()
    if not expanded.is_absolute():
        expanded = resolution_path.parent / expanded
    return expanded


def _warn(warnings: List[str], message: str) -> None:
    warnings.append(message)
    logger.warning("profile resolution: %s", message)


__all__ = [
    "RESOLUTION_SCHEMA_VERSION",
    "ResolvedProfile",
    "SOURCE_BOUND",
    "SOURCE_DEFAULT",
    "SOURCE_DEGRADED",
    "SOURCE_NONE",
    "resolve_profile",
]
