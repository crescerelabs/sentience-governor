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
import math
import re
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
    OPERATION_ACTIONS,
    OPERATION_DOMAINS,
    OPERATION_RULE_KEYS,
    OPTIONAL_ADDITIVE_FIELDS,
    RESERVED_ON_MATCH_VALUES,
    RESERVED_TOP_LEVEL_KEYS,
    SCHEMA_VERSION,
    SECTION_HIGH_CONSEQUENCE,
    SECTION_SESSION_INTENT,
    SECTION_TASK_BOUNDARY,
    SET_VALUED_RULE_PREDICATES,
    VALID_DEMAND_AT_VALUES,
    VALID_ON_MATCH_VALUES,
    VALID_SIGNAL_VALUES,
    default_profile_data,
)

# ---------------------------------------------------------------------------
# Default file locations
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_PATH = Path.home() / ".sentience" / "profile.yaml"

# v0.3.2: optional per-agent resolution file (see profile/resolver.py).
# Deliberately a separate file from profile.yaml: bindings for OTHER
# agents must not change the fingerprint of the profile that governs
# THIS session.
DEFAULT_RESOLUTION_PATH = Path.home() / ".sentience" / "resolution.yaml"

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
    (SECTION_HIGH_CONSEQUENCE, "operations"): (
        "Rules over what a shell command does, e.g. "
        "{domain: cloud_infrastructure, destructive: true}. "
        "Empty list = none. Order does not matter."
    ),
    (SECTION_HIGH_CONSEQUENCE, "on_match"): (
        "What to do when a high-consequence tool or operation is used. "
        "'flag' = surface it."
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
        # v0.3.2.1: admissible representation. Every mapping key must be a
        # string before the JSON round trip below, which would otherwise
        # coerce integer, boolean and null keys to strings silently (and
        # raise TypeError for date keys). Keys unknown to every consumer
        # were never legitimate; rejecting them here, in the one place all
        # construction paths pass, is the whole of the check. Profiles with
        # string keys are untouched, so canonical bytes and fingerprints are
        # unchanged.
        _reject_non_string_keys(data, "<root>")
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

    def canonical_bytes(self) -> bytes:
        """Return the exact bytes ``content_hash`` hashes.

        v0.3.2: this is the one canonical representation of the profile's
        effective governance posture. The full content hash, the short
        fingerprint, and (from a later checkpoint) the immutable snapshot
        files are all derived from these bytes; there is no second
        hashing algorithm or second serialization.
        """
        return json.dumps(
            canonical_profile_data(self._data),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    def content_hash(self) -> str:
        """Return the full 64-hex SHA-256 of the profile's canonical form.

        The hash is deterministic across:
        - whitespace / comment differences in the source YAML
        - key-ordering differences within mappings
        - sequence-ordering differences are NOT normalized for the
          historical ordered fields (signals/tools are operator-meaningful)

        v0.3.2 additions, applied by :func:`canonical_profile_data`:
        - registered optional-additive fields (``high_consequence.operations``)
          are omitted when absent-equivalent, so an unchanged legacy
          profile keeps its historical hash when the runtime gains a new
          optional capability;
        - ``high_consequence.operations`` is an unordered, deduplicated set
          of rules for identity, with set-valued predicates sorted, because
          rules are evaluated existentially and their order carries no
          governance meaning.

        Used as the integrity primitive: the same effective posture
        always hashes to the same value; a changed posture always hashes
        differently.
        """
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def fingerprint(self) -> str:
        """Return the short fingerprint used on event envelopes.

        The first :data:`FINGERPRINT_LENGTH` (12) characters of
        :meth:`content_hash`. Public evidence identifier only; storage
        identity and integrity checks use the full hash.
        """
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
        elif isinstance(sv, bool) or not isinstance(sv, int):
            # v0.3.2.1: the runtime applies int() to this value, so a
            # non-integer is a runtime hazard, not a preference. Booleans
            # are integers to Python and are excluded explicitly.
            errors.append(
                f"'schema_version' must be an integer; got "
                f"{type(sv).__name__}."
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
                    for index, sig in enumerate(signals):
                        if not isinstance(sig, str):
                            errors.append(
                                f"'{SECTION_TASK_BOUNDARY}.signals' entries "
                                f"must be strings; entry {index} is "
                                f"{type(sig).__name__}."
                            )
                            continue
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
                self._validate_numeric_parameter(
                    tb.get("time_gap_seconds"),
                    field=f"'{SECTION_TASK_BOUNDARY}.time_gap_seconds'",
                    integer_only=False,
                    minimum=0,
                    errors=errors,
                )
                self._validate_numeric_parameter(
                    tb.get("dir_change_depth"),
                    field=f"'{SECTION_TASK_BOUNDARY}.dir_change_depth'",
                    integer_only=True,
                    minimum=1,
                    errors=errors,
                )
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
                else:
                    for index, pattern in enumerate(tools):
                        if not isinstance(pattern, str):
                            # v0.3.2.1: each entry reaches re.search at
                            # runtime; a non-string raises there.
                            errors.append(
                                f"'{SECTION_HIGH_CONSEQUENCE}.tools' entries "
                                f"must be strings; entry {index} is "
                                f"{type(pattern).__name__}."
                            )
                            continue
                        try:
                            re.compile(pattern)
                        except re.error as exc:
                            # The runtime already skips a pattern that does
                            # not compile; until v0.3.2.1 it did so silently.
                            warnings.append(
                                f"'{SECTION_HIGH_CONSEQUENCE}.tools' entry "
                                f"{index} is not a valid regular expression: "
                                f"{exc}."
                            )
                self._validate_operations(
                    hc.get("operations"),
                    errors=errors,
                    warnings=warnings,
                    strict=strict,
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
    def _validate_numeric_parameter(
        value: Any,
        *,
        field: str,
        integer_only: bool,
        minimum: int,
        errors: List[str],
    ) -> None:
        """Validate a runtime-consumed numeric parameter (v0.3.2.1).

        The runtime compares ``time_gap_seconds`` with ``>=`` and uses
        ``dir_change_depth`` as a slice bound, so each must be a real
        number of the right kind, finite, and within range. Booleans are
        rejected explicitly (``True`` would otherwise pass as ``1``).
        ``None`` means absent and is not checked here; defaults apply.
        """
        if value is None:
            return
        if isinstance(value, bool):
            errors.append(f"{field} must be a number; got bool.")
            return
        if integer_only:
            if not isinstance(value, int):
                errors.append(
                    f"{field} must be a positive integer; got "
                    f"{type(value).__name__}."
                )
                return
        elif not isinstance(value, (int, float)):
            errors.append(f"{field} must be a number; got {type(value).__name__}.")
            return
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{field} must be finite; got {value!r}.")
            return
        if value < minimum:
            errors.append(f"{field} must be at least {minimum}; got {value!r}.")

    @staticmethod
    def _validate_operations(
        value: Any,
        *,
        errors: List[str],
        warnings: List[str],
        strict: bool,
    ) -> None:
        """Validate ``high_consequence.operations`` (v0.3.2).

        Shape: a list of rule mappings over ``domain`` / ``action`` /
        ``destructive``. ``domain`` and ``action`` take a string or a list
        of strings from the vocabularies in :mod:`schema`; ``destructive``
        takes ``true`` or ``false``. A rule with no predicates would match
        every classified operation and is warned about.

        Lenient by default: a malformed rule or an unknown vocabulary
        value is a warning (an error in strict mode), and the runtime
        evaluator, when it lands, skips such a rule rather than crashing.
        A non-list ``operations`` value is always an error, matching the
        existing ``tools`` rule.
        """
        field = f"'{SECTION_HIGH_CONSEQUENCE}.operations'"
        if value is None:
            return  # absent-equivalent
        if not isinstance(value, list):
            errors.append(f"{field} must be a list; got {type(value).__name__}.")
            return

        def _report(msg: str) -> None:
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

        for index, rule in enumerate(value):
            where = f"{field}[{index}]"
            if not isinstance(rule, dict):
                _report(
                    f"{where} must be a mapping; got {type(rule).__name__}. "
                    "The rule is skipped at runtime."
                )
                continue
            if not rule:
                warnings.append(
                    f"{where} specifies no predicates and would match every "
                    "classified operation."
                )
            for key in rule:
                if key not in OPERATION_RULE_KEYS:
                    _report(
                        f"{where} has unknown key '{key}'. Recognized keys: "
                        + ", ".join(sorted(OPERATION_RULE_KEYS))
                        + ". The rule is skipped at runtime."
                    )
            for key, vocabulary, label in (
                ("domain", OPERATION_DOMAINS, "domains"),
                ("action", OPERATION_ACTIONS, "actions"),
            ):
                if key not in rule:
                    continue
                raw = rule[key]
                members = raw if isinstance(raw, list) else [raw]
                for member in members:
                    if not isinstance(member, str) or member not in vocabulary:
                        _report(
                            f"{where}.{key} value {member!r} is not recognized. "
                            f"Valid {label}: " + ", ".join(sorted(vocabulary))
                            + ". The rule is skipped at runtime."
                        )
            if "destructive" in rule and not isinstance(rule["destructive"], bool):
                _report(
                    f"{where}.destructive must be true or false; got "
                    f"{rule['destructive']!r}. The rule is skipped at runtime."
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


def canonical_profile_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical, hashable form of merged profile data (v0.3.2).

    Two transformations, and only these:

    1. **Omission-aware fields.** Each ``(section, key)`` registered in
       :data:`OPTIONAL_ADDITIVE_FIELDS` is removed when its value is
       ``None`` or equals its absent-equivalent. This is what lets a
       legacy profile keep its historical fingerprint after the runtime
       adds a new optional field to the defaults. Fields that are not
       registered are hashed exactly as they always have been.
    2. **Unordered operation rules.** ``high_consequence.operations`` is
       evaluated existentially (any matching rule raises the same flag),
       so for identity it is an unordered set: each rule is canonicalized
       (set-valued predicates ``domain`` / ``action`` sorted and
       deduplicated; other keys untouched), identical rules are
       deduplicated, and the rules are ordered by their canonical
       serialization. The original YAML order is preserved in the
       profile itself for readability; only identity treats it as a set.

    The historical ordered fields (``task_boundary.signals``,
    ``high_consequence.tools``) are not touched: their order still
    changes the hash, as it always has.

    Pure: returns a fresh structure and never mutates ``data``. Idempotent:
    canonicalizing a canonical form yields the same bytes, which the
    snapshot machinery of a later checkpoint relies on.
    """
    canonical: Dict[str, Any] = json.loads(json.dumps(data))

    for (section, key), absent in OPTIONAL_ADDITIVE_FIELDS.items():
        holder = canonical.get(section)
        if isinstance(holder, dict) and key in holder:
            if holder[key] is None or holder[key] == absent:
                del holder[key]

    hc = canonical.get(SECTION_HIGH_CONSEQUENCE)
    if isinstance(hc, dict) and isinstance(hc.get("operations"), list):
        hc["operations"] = _canonical_operation_rules(hc["operations"])

    return canonical


def _canonical_operation_rules(rules: List[Any]) -> List[Any]:
    """Canonicalize an ``operations`` list as an unordered, deduplicated set."""
    serialized = set()
    for rule in rules:
        if isinstance(rule, dict):
            rule = dict(rule)
            for predicate in SET_VALUED_RULE_PREDICATES:
                value = rule.get(predicate)
                if isinstance(value, list):
                    # Members are strings in a valid rule; sort by their
                    # JSON serialization so a malformed mixed-type list is
                    # still ordered deterministically rather than raising.
                    rule[predicate] = [
                        json.loads(m)
                        for m in sorted({json.dumps(v, sort_keys=True) for v in value})
                    ]
        serialized.add(json.dumps(rule, sort_keys=True, separators=(",", ":")))
    return [json.loads(s) for s in sorted(serialized)]


def _reject_non_string_keys(value: Any, path: str) -> None:
    """Raise ``ValueError`` naming the key path for any non-string mapping key.

    Walks mappings and sequences; scalars are not inspected (value types are
    the JSON round trip's concern, which raises ``TypeError`` for dates,
    bytes and sets exactly as before).
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"Profile mapping keys must be strings; got "
                    f"{type(key).__name__} key {key!r} at {path}."
                )
            _reject_non_string_keys(child, f"{path}.{key}" if path != "<root>" else key)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_non_string_keys(child, f"{path}[{index}]")


def _runtime_readiness(profile: "GovernanceProfile") -> ProfileValidationResult:
    """The single readiness decision for binding a profile at runtime (v0.3.2.1).

    Internal. Returns ``profile.validate(strict=False)``; callers on the binding
    path (resolver, session start, snapshot rehydration) treat ``errors`` as
    "unusable". It adds no rules of its own: ``validate()`` is the one
    definition of an invalid profile. Not part of the public API.
    """
    return profile.validate(strict=False)


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
