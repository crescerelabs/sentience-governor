"""Governance profile loader, validator, and content-hash helper.

Public surface:

* :class:`GovernanceProfile` — immutable representation of a loaded
  profile (or defaults).
* :class:`ProfileValidationResult` — structured result returned by
  :meth:`GovernanceProfile.validate`.

Module guarantees:

1. **Read-only validation.** ``validate()`` never mutates the file.
2. **Lenient by default.** Unknown keys produce warnings, not
   errors. Strict mode (``strict=True``) errors on unknown keys.
3. **Deterministic content hash.** Same logical content produces
   the same hash regardless of whitespace, comment, or key-order
   differences in the source YAML.
4. **Defaults preserve v0.2.4 behavior.** A session with no profile
   file behaves identically to a v0.2.4 session.

This is Checkpoint 1 of v0.2.5. Runtime integration (session
manager, policy evaluator, advisory flags) lands in CP2. CLI
surface (``sentience profile <verb>``) lands in CP5.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from sentience_governor.profile.schema import (
    ACTIVE_SECTIONS,
    DEFAULT_HIGH_CONSEQUENCE,
    DEFAULT_SESSION_INTENT,
    DEFAULT_TASK_BOUNDARY,
    KNOWN_TOP_LEVEL_KEYS,
    ON_MATCH_FLAG,
    RESERVED_ON_MATCH_VALUES,
    RESERVED_TOP_LEVEL_KEYS,
    SCHEMA_VERSION,
    SECTION_HIGH_CONSEQUENCE,
    SECTION_SESSION_INTENT,
    SECTION_TASK_BOUNDARY,
    VALID_DEMAND_AT_VALUES,
    VALID_ON_MATCH_VALUES,
    VALID_SIGNAL_VALUES,
    default_profile_data,
)

# ---------------------------------------------------------------------------
# Default file location
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_PATH = Path.home() / ".sentience" / "profile.yaml"

# Fingerprint length used by the runtime when populating
# profile_fingerprint on event envelopes (CP3 will use this). 12
# hex chars of SHA256 — ~48 bits of entropy, sufficient for
# detecting mid-session profile changes within a session.
FINGERPRINT_LENGTH = 12


# ---------------------------------------------------------------------------
# Inline documentation for exported profiles (F-V7)
# ---------------------------------------------------------------------------
#
# Operator-readability: a profile written by `sentience profile init`
# (or `export`) must explain itself to a non-developer reading the
# file standalone. These comments are injected at emit time only — they
# never touch the in-memory profile data, so the content hash is
# unaffected (the hash is computed from the parsed dict, not the file
# text). Comment text is value-independent (it explains what a field
# *means*, not what it currently holds), so there is no risk of the
# documentation drifting out of sync with `default_profile_data()`.

# A short banner emitted at the top of the file body (after the
# machine header), before the first field.
_PROFILE_BANNER_COMMENT = (
    "This is your governance profile. Sentience reads it; you own it.\n"
    "Edit the values below to tune what gets flagged. Lines starting\n"
    "with '#' are explanations and are ignored by the runtime."
)

# Per-section explanations (emitted above the section key).
_SECTION_COMMENTS: Dict[str, str] = {
    SECTION_SESSION_INTENT: (
        "session_intent — should the agent declare what it's doing, "
        "and when?"
    ),
    SECTION_TASK_BOUNDARY: (
        "task_boundary — what counts as the agent crossing into a "
        "new task?"
    ),
    SECTION_HIGH_CONSEQUENCE: (
        "high_consequence — which tool calls should always be "
        "surfaced?"
    ),
}

# Per-field explanations, keyed by (section, field). schema_version is
# a top-level scalar so it is keyed by (None, "schema_version").
_FIELD_COMMENTS: Dict[tuple, str] = {
    (None, "schema_version"): "Profile format version. Leave as-is.",
    (SECTION_SESSION_INTENT, "required"): (
        "true = a session with no declared intent is flagged."
    ),
    (SECTION_SESSION_INTENT, "demand_at"): (
        "When intent must be declared. 'session_start' = before the "
        "first tool call."
    ),
    (SECTION_SESSION_INTENT, "prompt_template"): (
        "Reserved for future use; not read by the runtime. null = "
        "default."
    ),
    (SECTION_TASK_BOUNDARY, "signals"): (
        "Which signals mark a new task. Empty list = task-boundary "
        "detection off. Options: 'time_gap', 'dir_change'."
    ),
    (SECTION_TASK_BOUNDARY, "time_gap_seconds"): (
        "Idle gap (seconds) that counts as a new task. Only used if "
        "'time_gap' is in signals."
    ),
    (SECTION_TASK_BOUNDARY, "dir_change_depth"): (
        "How many directory levels of movement count as a new task. "
        "Only used if 'dir_change' is in signals."
    ),
    (SECTION_TASK_BOUNDARY, "on_match"): (
        "What to do when a boundary is detected. 'flag' = surface it "
        "(open tier observes; never blocks)."
    ),
    (SECTION_HIGH_CONSEQUENCE, "tools"): (
        "Tool names that should always be surfaced (e.g. 'db.delete'). "
        "Empty list = none."
    ),
    (SECTION_HIGH_CONSEQUENCE, "on_match"): (
        "What to do when a high-consequence tool is used. 'flag' = "
        "surface it."
    ),
}


def _comment_block(text: str, indent: str = "") -> List[str]:
    """Render multi-line comment ``text`` as ``# ``-prefixed lines."""
    return [f"{indent}# {line}" for line in text.split("\n")]


def render_commented_yaml(data: Dict[str, Any]) -> str:
    """Render ``data`` as YAML with injected explanatory comments.

    Walks the profile's two-level structure (top-level scalars +
    one level of section dicts) and inserts section/field comments
    from the maps above. Unknown keys/sections (e.g. operator-added
    fields, or reserved keys like ``extends``) are emitted without a
    comment rather than dropped — so an edited profile round-trips
    safely.

    The actual value formatting is delegated to ``yaml.safe_dump`` so
    nulls, lists, ints, bools, and strings all quote correctly.
    """
    lines: List[str] = []
    lines.extend(_comment_block(_PROFILE_BANNER_COMMENT))
    lines.append("")

    for key, value in data.items():
        if isinstance(value, dict):
            # Section: comment above the section key, field comments
            # above each field.
            section_comment = _SECTION_COMMENTS.get(key)
            if section_comment:
                lines.extend(_comment_block(section_comment))
            lines.append(f"{key}:")
            if not value:
                # Empty mapping — emit inline {} to stay valid YAML.
                lines[-1] = f"{key}: {{}}"
                continue
            for field_name, field_value in value.items():
                field_comment = _FIELD_COMMENTS.get((key, field_name))
                if field_comment:
                    lines.extend(_comment_block(field_comment, indent="  "))
                fragment = yaml.safe_dump(
                    {field_name: field_value},
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                ).rstrip("\n")
                lines.extend(f"  {frag_line}" for frag_line in fragment.split("\n"))
        else:
            # Top-level scalar (e.g. schema_version).
            field_comment = _FIELD_COMMENTS.get((None, key))
            if field_comment:
                lines.extend(_comment_block(field_comment))
            fragment = yaml.safe_dump(
                {key: value},
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            ).rstrip("\n")
            lines.extend(fragment.split("\n"))
        lines.append("")  # blank line between top-level entries

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


class ProfileValidationResult:
    """Structured result from :meth:`GovernanceProfile.validate`.

    Attributes:
        is_valid: True if no errors were found. Warnings do not
            affect validity in lenient mode; in strict mode any
            warning becomes an error.
        errors: list of error messages (load-blocking).
        warnings: list of warning messages (non-blocking; surfaced
            to the operator but the runtime proceeds with
            defaults).
        strict: whether validation was run in strict mode.
    """

    def __init__(
        self,
        is_valid: bool,
        errors: List[str],
        warnings: List[str],
        strict: bool,
    ) -> None:
        self.is_valid = is_valid
        self.errors = list(errors)
        self.warnings = list(warnings)
        self.strict = strict

    def __repr__(self) -> str:
        return (
            f"ProfileValidationResult(is_valid={self.is_valid}, "
            f"errors={len(self.errors)}, warnings={len(self.warnings)}, "
            f"strict={self.strict})"
        )

    def format_human(self) -> str:
        """Format the result for CLI display."""
        lines = []
        if self.is_valid:
            lines.append("Profile is valid.")
        else:
            lines.append("Profile validation FAILED.")
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  - {err}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for warn in self.warnings:
                lines.append(f"  - {warn}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# GovernanceProfile
# ---------------------------------------------------------------------------


class GovernanceProfile:
    """Immutable representation of a governance profile.

    Construct via classmethods (:meth:`from_default_path`,
    :meth:`from_file`, :meth:`defaults`); never call the constructor
    directly with raw data unless you've already validated.

    The profile is immutable after construction. Operators editing
    the profile file mid-session do not affect a running session —
    they affect the next session start. This avoids mid-session
    behavioral drift.
    """

    def __init__(
        self,
        data: Dict[str, Any],
        *,
        source_path: Optional[Path] = None,
    ) -> None:
        # Store a deep copy so external callers cannot mutate
        # profile state after construction.
        self._data = json.loads(json.dumps(data))
        self._source_path = source_path

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def defaults(cls) -> "GovernanceProfile":
        """Return a profile populated with sensible defaults.

        Used when no profile file is present. The defaults preserve
        v0.2.4 behavior — a session with this profile behaves
        identically to a session without any profile loaded.
        """
        return cls(default_profile_data(), source_path=None)

    @classmethod
    def from_default_path(cls) -> "GovernanceProfile":
        """Load ``~/.sentience/profile.yaml`` or return defaults if missing.

        This is the canonical loader. Runtime integration (CP2)
        calls this at session start.

        Returns defaults silently if the file does not exist;
        raises :class:`ValueError` if the file exists but is
        unparseable.
        """
        if not DEFAULT_PROFILE_PATH.is_file():
            return cls.defaults()
        return cls.from_file(DEFAULT_PROFILE_PATH)

    @classmethod
    def from_default_path_or_none(cls) -> Optional["GovernanceProfile"]:
        """Return the loaded profile, or None if no profile file exists.

        Convenience for wrapper integration (CP3): wrappers pass the
        result directly to ``SessionManager.session_start(profile=...)``.
        When None, the session takes the v0.2.4 code path (no profile
        transforms applied, no profile metadata in the trace). When
        non-None, the session is governed by the operator-authored
        profile.

        Distinct from ``from_default_path`` which always returns a
        ``GovernanceProfile`` (defaults when missing). Use this when
        the caller wants to distinguish "operator authored a profile"
        from "running with defaults."
        """
        if not DEFAULT_PROFILE_PATH.is_file():
            return None
        return cls.from_file(DEFAULT_PROFILE_PATH)

    @classmethod
    def from_file(cls, path: Path) -> "GovernanceProfile":
        """Load a profile from an explicit path.

        Args:
            path: Path to a YAML profile file.

        Returns:
            A constructed GovernanceProfile.

        Raises:
            FileNotFoundError: if the path does not exist.
            ValueError: if the file is unparseable YAML.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Profile file not found: {path}")
        try:
            raw_text = path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Failed to parse profile YAML at {path}: {exc}"
            ) from exc

        # Empty file or all comments → defaults
        if loaded is None:
            return cls.defaults()

        if not isinstance(loaded, dict):
            raise ValueError(
                f"Profile root must be a YAML mapping, got "
                f"{type(loaded).__name__} at {path}"
            )

        # Merge with defaults so callers always see complete shape.
        merged = _merge_with_defaults(loaded)
        return cls(merged, source_path=path)

    # ------------------------------------------------------------------
    # Accessors (read-only)
    # ------------------------------------------------------------------

    @property
    def source_path(self) -> Optional[Path]:
        """The file path this profile was loaded from, or None for defaults."""
        return self._source_path

    @property
    def schema_version(self) -> int:
        return int(self._data.get("schema_version", SCHEMA_VERSION))

    @property
    def session_intent(self) -> Dict[str, Any]:
        return dict(self._data.get(SECTION_SESSION_INTENT, {}))

    @property
    def task_boundary(self) -> Dict[str, Any]:
        return dict(self._data.get(SECTION_TASK_BOUNDARY, {}))

    @property
    def high_consequence(self) -> Dict[str, Any]:
        return dict(self._data.get(SECTION_HIGH_CONSEQUENCE, {}))

    def to_dict(self) -> Dict[str, Any]:
        """Return a fresh dict copy of the profile data.

        Callers cannot mutate the GovernanceProfile through the
        returned dict — it's a deep copy.
        """
        return json.loads(json.dumps(self._data))

    # ------------------------------------------------------------------
    # Content hash (deterministic across whitespace/comment/order)
    # ------------------------------------------------------------------

    def content_hash(self) -> str:
        """Return SHA256 of the profile's canonical form, hex-encoded.

        The hash is deterministic across:
        - whitespace / comment differences in the source YAML
        - key-ordering differences within mappings
        - sequence-ordering differences are NOT normalized
          (signals/tools are operator-meaningful)

        Used as the integrity primitive: the same profile content
        always hashes to the same value; modified content always
        hashes differently.
        """
        canonical = json.dumps(
            self._data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def fingerprint(self) -> str:
        """Return the short fingerprint used on event envelopes."""
        return self.content_hash()[:FINGERPRINT_LENGTH]

    # ------------------------------------------------------------------
    # Validation (READ-ONLY — never mutates the file)
    # ------------------------------------------------------------------

    def validate(self, strict: bool = False) -> ProfileValidationResult:
        """Validate the profile against the v0.2.5 schema.

        Read-only by design. Never modifies the source file.

        Args:
            strict: when True, unknown top-level keys and unknown
                values within known fields become errors instead of
                warnings.

        Returns:
            ProfileValidationResult with is_valid, errors, warnings.
        """
        errors: List[str] = []
        warnings: List[str] = []

        # ---------- schema_version ----------
        sv = self._data.get("schema_version")
        if sv is None:
            warnings.append(
                "Missing 'schema_version' header field; defaulting to "
                f"{SCHEMA_VERSION}."
            )
        elif sv != SCHEMA_VERSION:
            warnings.append(
                f"schema_version={sv} does not match runtime schema "
                f"version {SCHEMA_VERSION}. Future versions may add "
                "additive extensions; reading as best-effort."
            )

        # ---------- top-level keys ----------
        for key in self._data.keys():
            if key in KNOWN_TOP_LEVEL_KEYS:
                if key in RESERVED_TOP_LEVEL_KEYS:
                    warnings.append(
                        f"Top-level key '{key}' is reserved for a "
                        "future v0.2.x release and is ignored in "
                        "v0.2.5."
                    )
                continue
            msg = (
                f"Unknown top-level key '{key}' in profile. "
                "Recognized keys: "
                + ", ".join(sorted(KNOWN_TOP_LEVEL_KEYS))
                + "."
            )
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

        # ---------- session_intent ----------
        si = self._data.get(SECTION_SESSION_INTENT)
        if si is not None:
            if not isinstance(si, dict):
                errors.append(
                    f"'{SECTION_SESSION_INTENT}' must be a mapping; "
                    f"got {type(si).__name__}."
                )
            else:
                demand_at = si.get("demand_at")
                if demand_at is not None and demand_at not in VALID_DEMAND_AT_VALUES:
                    msg = (
                        f"'{SECTION_SESSION_INTENT}.demand_at' value "
                        f"'{demand_at}' is not recognized. Valid values: "
                        + ", ".join(sorted(VALID_DEMAND_AT_VALUES))
                        + "."
                    )
                    if strict:
                        errors.append(msg)
                    else:
                        warnings.append(msg)
                required = si.get("required")
                if required is not None and not isinstance(required, bool):
                    msg = (
                        f"'{SECTION_SESSION_INTENT}.required' must be "
                        "true or false."
                    )
                    if strict:
                        errors.append(msg)
                    else:
                        warnings.append(msg)

        # ---------- task_boundary ----------
        tb = self._data.get(SECTION_TASK_BOUNDARY)
        if tb is not None:
            if not isinstance(tb, dict):
                errors.append(
                    f"'{SECTION_TASK_BOUNDARY}' must be a mapping; "
                    f"got {type(tb).__name__}."
                )
            else:
                signals = tb.get("signals", [])
                if not isinstance(signals, list):
                    errors.append(
                        f"'{SECTION_TASK_BOUNDARY}.signals' must be a "
                        f"list; got {type(signals).__name__}."
                    )
                else:
                    for sig in signals:
                        if sig not in VALID_SIGNAL_VALUES:
                            msg = (
                                f"'{SECTION_TASK_BOUNDARY}.signals' "
                                f"contains unknown signal '{sig}'. "
                                "Valid signals: "
                                + ", ".join(sorted(VALID_SIGNAL_VALUES))
                                + "."
                            )
                            if strict:
                                errors.append(msg)
                            else:
                                warnings.append(msg)
                self._validate_on_match(
                    tb.get("on_match"),
                    section_name=SECTION_TASK_BOUNDARY,
                    errors=errors,
                    warnings=warnings,
                    strict=strict,
                )

        # ---------- high_consequence ----------
        hc = self._data.get(SECTION_HIGH_CONSEQUENCE)
        if hc is not None:
            if not isinstance(hc, dict):
                errors.append(
                    f"'{SECTION_HIGH_CONSEQUENCE}' must be a mapping; "
                    f"got {type(hc).__name__}."
                )
            else:
                tools = hc.get("tools", [])
                if not isinstance(tools, list):
                    errors.append(
                        f"'{SECTION_HIGH_CONSEQUENCE}.tools' must be a "
                        f"list; got {type(tools).__name__}."
                    )
                self._validate_on_match(
                    hc.get("on_match"),
                    section_name=SECTION_HIGH_CONSEQUENCE,
                    errors=errors,
                    warnings=warnings,
                    strict=strict,
                )

        is_valid = len(errors) == 0
        return ProfileValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            strict=strict,
        )

    @staticmethod
    def _validate_on_match(
        value: Any,
        *,
        section_name: str,
        errors: List[str],
        warnings: List[str],
        strict: bool,
    ) -> None:
        """Validate an on_match value (shared between task_boundary + high_consequence)."""
        if value is None:
            return
        if value in VALID_ON_MATCH_VALUES:
            return
        if value in RESERVED_ON_MATCH_VALUES:
            # Reserved values; warn cleanly. Runtime
            # treats as "flag" + emits a warning at session start.
            warnings.append(
                f"'{section_name}.on_match' value '{value}' is "
                "reserved for future enforcement and is treated "
                "as 'flag' by this runtime."
            )
            return
        msg = (
            f"'{section_name}.on_match' value '{value}' is not "
            "recognized. Only 'flag' is valid in v0.2.5."
        )
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)

    # ------------------------------------------------------------------
    # Export (writes header + content)
    # ------------------------------------------------------------------

    def export(self, path: Path) -> None:
        """Write the profile to ``path`` with a fresh header.

        The header contains schema_version, content_hash, and an
        ISO-8601 UTC timestamp. The header is recomputed every
        export — operators editing the file by hand and then
        running export get a fresh, consistent header.

        Args:
            path: destination file path. Parent directories are
                created if they don't exist.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        content_hash = self.content_hash()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        header_lines = [
            f"# Schema version: {self.schema_version}",
            f"# Content hash: sha256:{content_hash}",
            f"# Generated: {timestamp}",
            "",
        ]

        # F-V7: emit with inline explanatory comments so the file is
        # readable standalone by a non-developer operator. Comments are
        # injected at emit time only — they never affect the content
        # hash (computed above from self._data, not the file text).
        body = render_commented_yaml(self._data)

        path.write_text("\n".join(header_lines) + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_with_defaults(loaded: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a loaded profile dict with defaults so the resulting
    dict has every active section populated.

    Reserved keys (``extends``, ``policies``, ``custom_rules``) are
    preserved as-is from the loaded data — the validator surfaces
    them as warnings; the loader leaves them in the dict so the
    operator's intent is preserved.
    """
    merged = default_profile_data()

    # Preserve operator-supplied schema_version even if it differs
    # from runtime — validator will surface a warning.
    if "schema_version" in loaded:
        merged["schema_version"] = loaded["schema_version"]

    # Active sections: shallow-merge so operator-supplied fields
    # override defaults but unspecified fields keep their defaults.
    for section_name in ACTIVE_SECTIONS:
        if section_name in loaded:
            supplied = loaded[section_name]
            if isinstance(supplied, dict):
                merged[section_name] = {
                    **merged[section_name],
                    **supplied,
                }
            else:
                # Type mismatch — keep the operator's value so
                # validator can flag it; don't silently coerce.
                merged[section_name] = supplied

    # Reserved keys: preserve operator's value untouched.
    for key in RESERVED_TOP_LEVEL_KEYS:
        if key in loaded:
            merged[key] = loaded[key]

    # Any other unknown keys (lenient mode): preserve so validator
    # can surface them.
    for key, value in loaded.items():
        if key not in KNOWN_TOP_LEVEL_KEYS:
            merged[key] = value

    return merged
