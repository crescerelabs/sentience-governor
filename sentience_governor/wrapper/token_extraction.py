"""Defensive extraction of LLM token usage from heterogeneous response shapes.

Returns a dict of canonical hint field names → ``int | None`` values.
Missing fields return as ``None`` (zero would be a measurement; ``None``
is absence).

Two design rules govern every extraction in this module — both are
load-bearing for v0.2.3 Track 2:

1. **Normalize defensively (per §1.1):** any malformed provider value
   (string, float, negative, wrong type) demotes to ``None`` silently.
   Extraction NEVER raises. Token data is best-effort signal — a busted
   provider response cannot break the agent's primary work.

2. **Merge, don't early-return (per §2.3):** the top-level helper probes
   all known shapes and merges results with precedence-biased fill.
   Higher-precedence non-None values win; lower-precedence layers only
   fill gaps. Crucially, Anthropic-via-LangChain carries cache fields
   on ``response_metadata['usage']`` that don't surface through
   ``usage_metadata`` — early-returning on ``usage_metadata`` would
   silently drop them.

Cache-token semantics (per §1.2): we preserve provider-reported raw
values without normalisation. Anthropic excludes cache reads from
``input_tokens`` (their convention); OpenAI includes them. Sentience
does not subtract, recompute, or attempt to "harmonise" the two —
downstream cost calculation is responsible for understanding the
per-provider semantics.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# The five numeric token fields. ``model_identifier``, ``provider``, and
# ``llm_turn_id`` are not part of this constant because they aren't
# subject to the same numeric-normalisation rules — they're carried
# through as-is by callers. The merge helper still fills them via the
# string-merge code path.
CANONICAL_TOKEN_FIELDS = (
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "llm_cached_read_tokens",
    "llm_cached_write_tokens",
    "llm_reasoning_tokens",
)


# ---------------------------------------------------------------------------
# Normalisation (§1.1)
# ---------------------------------------------------------------------------


def _coerce_token_value(value: Any) -> Optional[int]:
    """Apply §1.1 normalisation rules to a single provider value.

    Returns a non-negative int if the value is cleanly convertible,
    otherwise ``None``. Never raises.

    Accepted inputs:
      * ``None`` → ``None``
      * non-negative int → preserved (including ``0``)
      * non-negative integral float (e.g. ``1234.0``) → coerced to int
      * non-negative numeric string (e.g. ``"1234"``, ``"00123"``,
        ``" 123 "``) → coerced to int (whitespace trim matches Python's
        default ``int()`` behaviour)

    Rejected (returns ``None``):
      * negative numbers
      * non-integral floats (``1234.5``)
      * non-numeric strings
      * booleans (``bool`` is an ``int`` subclass in Python; we filter
        explicitly so ``True`` doesn't sneak through as ``1``)
      * lists, dicts, any other type
    """
    if value is None:
        return None
    # bool BEFORE int — bool inherits from int but is never a valid
    # token count.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        ivalue = int(value)
        return ivalue if ivalue >= 0 else None
    if isinstance(value, str):
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return None
        return ivalue if ivalue >= 0 else None
    return None


def _empty_canonical() -> Dict[str, Optional[int]]:
    """Return a canonical-fields dict with every value ``None``."""
    return {field: None for field in CANONICAL_TOKEN_FIELDS}


def _merge_canonical(
    base: Dict[str, Optional[int]],
    overlay: Dict[str, Optional[int]],
) -> Dict[str, Optional[int]]:
    """Merge per §2.3: overlay fills ``None`` slots, never overwrites.

    Higher-precedence dict goes on the left (``base``). Call repeatedly
    with progressively lower-precedence layers as overlays.

    Pure: returns a new dict, doesn't mutate either input.
    """
    result = dict(base)
    for field in CANONICAL_TOKEN_FIELDS:
        if result.get(field) is None and overlay.get(field) is not None:
            result[field] = overlay[field]
    return result


# ---------------------------------------------------------------------------
# Per-shape extractors
# ---------------------------------------------------------------------------


def _get(value: Any, key: str) -> Any:
    """Read ``key`` from a dict OR getattr from an object.

    Anthropic SDK returns objects with attributes; LangChain often hands
    us dicts. This helper papers over the difference so each extractor
    can be shape-agnostic.
    """
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def extract_anthropic_usage(usage: Any) -> Dict[str, Optional[int]]:
    """Native Anthropic ``Message.usage`` (or dict-shaped equivalent).

    Also reused for the ``response.response_metadata['usage']`` shape
    that Anthropic-via-LangChain produces — both carry the same fields.
    """
    return {
        "llm_prompt_tokens": _coerce_token_value(_get(usage, "input_tokens")),
        "llm_completion_tokens": _coerce_token_value(_get(usage, "output_tokens")),
        "llm_cached_read_tokens": _coerce_token_value(
            _get(usage, "cache_read_input_tokens")
        ),
        "llm_cached_write_tokens": _coerce_token_value(
            _get(usage, "cache_creation_input_tokens")
        ),
        # Anthropic doesn't currently expose reasoning tokens as a
        # separate field. None until they do.
        "llm_reasoning_tokens": None,
    }


def extract_openai_usage(usage_dict: Any) -> Dict[str, Optional[int]]:
    """OpenAI-style usage dict (older LangChain or direct OpenAI SDK).

    ``cached_tokens`` is reported as a sub-count under
    ``prompt_tokens_details``; ``prompt_tokens`` already includes the
    cached portion (per OpenAI's convention — the opposite of
    Anthropic's). We surface both as canonical fields without doing any
    arithmetic, per §1.2.
    """
    if not isinstance(usage_dict, dict):
        return _empty_canonical()
    details = usage_dict.get("prompt_tokens_details") or {}
    output_details = usage_dict.get("completion_tokens_details") or {}
    return {
        "llm_prompt_tokens": _coerce_token_value(usage_dict.get("prompt_tokens")),
        "llm_completion_tokens": _coerce_token_value(
            usage_dict.get("completion_tokens")
        ),
        "llm_cached_read_tokens": _coerce_token_value(
            details.get("cached_tokens")
            if isinstance(details, dict)
            else None
        ),
        # OpenAI doesn't separately report cache writes (their pricing
        # model doesn't charge for cache writes the way Anthropic's
        # does).
        "llm_cached_write_tokens": None,
        "llm_reasoning_tokens": _coerce_token_value(
            output_details.get("reasoning_tokens")
            if isinstance(output_details, dict)
            else None
        ),
    }


def extract_langchain_usage_metadata(metadata: Any) -> Dict[str, Optional[int]]:
    """LangChain canonical ``usage_metadata`` shape (newer LC versions).

    LangChain canonicalised provider responses into a shared shape:
    ``input_tokens`` / ``output_tokens`` at the top level, with cache
    and reasoning details nested under ``input_token_details`` and
    ``output_token_details`` respectively.
    """
    if not isinstance(metadata, dict):
        return _empty_canonical()
    input_details = metadata.get("input_token_details") or {}
    output_details = metadata.get("output_token_details") or {}
    return {
        "llm_prompt_tokens": _coerce_token_value(metadata.get("input_tokens")),
        "llm_completion_tokens": _coerce_token_value(metadata.get("output_tokens")),
        "llm_cached_read_tokens": _coerce_token_value(
            input_details.get("cache_read")
            if isinstance(input_details, dict)
            else None
        ),
        "llm_cached_write_tokens": _coerce_token_value(
            input_details.get("cache_creation")
            if isinstance(input_details, dict)
            else None
        ),
        "llm_reasoning_tokens": _coerce_token_value(
            output_details.get("reasoning")
            if isinstance(output_details, dict)
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Top-level merge (§2.3)
# ---------------------------------------------------------------------------


def extract_from_langchain_response(response: Any) -> Dict[str, Optional[int]]:
    """Probe multiple LangChain response shapes; merge with precedence.

    Per §2.3: NEVER early-return. Anthropic-via-LangChain has cache
    fields on ``response_metadata['usage']`` that don't surface through
    ``usage_metadata``; early-returning loses them.

    Precedence order (highest first):

      1. Anthropic-shaped ``response_metadata['usage']`` — provider-
         specific cache fields
      2. LangChain canonical ``usage_metadata`` — newer LC shape
      3. Legacy ``llm_output['token_usage']`` — older LC shape

    Higher-precedence non-None values win; lower-precedence layers
    only fill gaps. Never raises (defensive: any extraction failure
    falls back to ``None`` for the affected fields).
    """
    result = _empty_canonical()

    # 1. Anthropic-specific (highest precedence: carries cache fields).
    response_metadata = getattr(response, "response_metadata", None) or {}
    if isinstance(response_metadata, dict):
        anthro_usage = response_metadata.get("usage")
        if anthro_usage:
            try:
                result = _merge_canonical(
                    result, extract_anthropic_usage(anthro_usage)
                )
            except Exception:
                pass  # defensive — never raise from extraction

    # 2. LangChain canonical usage_metadata.
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, dict) and usage_metadata:
        try:
            result = _merge_canonical(
                result, extract_langchain_usage_metadata(usage_metadata)
            )
        except Exception:
            pass

    # 3. Legacy llm_output['token_usage'].
    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        token_usage = llm_output.get("token_usage")
        if isinstance(token_usage, dict) and token_usage:
            try:
                result = _merge_canonical(result, extract_openai_usage(token_usage))
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Provider cache-convention + derived token burn (v0.2.6.1 CP1)
# ---------------------------------------------------------------------------
#
# `llm_prompt_tokens` does not mean the same thing across providers (the §1.2
# caveat above, made executable here). The cache-aware extractors preserve
# raw provider-native categories; this is the single place that codifies how
# those categories compose into a non-double-counting total — the "downstream
# cost calculation" §1.2 names as responsible for per-provider semantics.
#
# Convention knowledge lives ONLY here. Consumers (the burn-rate /
# undeclared-intent analyzers, the Claude Code SessionEnd emission path) call
# `derive_turn_token_burn` and never parse model names or re-implement provider
# behavior.

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

# Confidence stamps for a derived burn value.
BURN_CONFIDENCE_FULL = "full"
BURN_CONFIDENCE_CONVENTION_PARTIAL = "convention-partial"

# Canonical fields whose raw values are echoed in a burn breakdown. Wider than
# the four that compose the burn total (reasoning is preserved for visibility
# but, like the burn-rate analyzer's `_TOKEN_FIELDS`, is NOT summed into burn —
# providers fold reasoning into output inconsistently, so adding it risks a
# double-count).
_BURN_BREAKDOWN_FIELDS = CANONICAL_TOKEN_FIELDS


def _nonneg_int(value: Any) -> int:
    """Coerce to a non-negative int for summation; anything else → ``0``.

    ``None`` (absence) and any malformed value contribute zero to a burn
    total — never raises. Mirrors ``_coerce_token_value`` but collapses the
    absence case to ``0`` because a *sum* needs a number, not a tri-state.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    return 0


def provider_cache_convention(provider: Any) -> Optional[Dict[str, bool]]:
    """How a provider's ``llm_prompt_tokens`` treats cache tokens.

    Returns a dict with two **independent** flags:

      * ``prompt_includes_cache_read``  — is ``cache_read`` already counted
        inside ``llm_prompt_tokens``?
      * ``prompt_includes_cache_write`` — is ``cache_write`` already counted
        inside ``llm_prompt_tokens``?

    Returns ``None`` when the convention is unknown (caller applies the
    conservative fallback). Never raises.

    * **Anthropic** — ``input_tokens`` EXCLUDES both cache categories, so both
      flags are ``False`` (cache is additive to the prompt).
    * **OpenAI** — ``prompt_tokens`` INCLUDES the cached-read portion, so
      ``prompt_includes_cache_read`` is ``True`` (adding it again would
      double-count); OpenAI does not report cache writes.
    """
    if not isinstance(provider, str):
        return None
    p = provider.strip().lower()
    if p == PROVIDER_ANTHROPIC:
        return {
            "prompt_includes_cache_read": False,
            "prompt_includes_cache_write": False,
        }
    if p == PROVIDER_OPENAI:
        return {
            "prompt_includes_cache_read": True,
            "prompt_includes_cache_write": False,
        }
    return None


def derive_turn_token_burn(
    tokens: Dict[str, Optional[int]],
    provider: Any,
) -> Dict[str, Any]:
    """Convention-aware total token/context burn for one turn.

    NOT a flat cross-runtime sum: cache additivity depends on the provider
    convention (Anthropic adds cache to the prompt; OpenAI's prompt already
    includes cache-read). See §D2.

    Args:
        tokens: canonical token dict (``llm_prompt_tokens`` etc.); values are
            ``int | None``. Missing/None fields contribute zero.
        provider: the persisted ``provider`` string (or ``None``/unknown).

    Returns a dict (plain primitives, byte-stable):

      * ``turn_token_burn`` — the derived non-double-counting total (int).
      * ``confidence`` — ``"full"`` when the convention is known, else
        ``"convention-partial"``.
      * ``convention`` — the resolved convention flags, or ``None`` if unknown.
      * ``breakdown`` — the raw canonical values echoed back (cache evidence is
        NEVER discarded, even when convention-partial).

    Fallback (unknown provider/convention): ``prompt + output`` only — the
    safest value that cannot double-count — with cache preserved in
    ``breakdown`` and ``confidence = "convention-partial"``. Never silently
    overcounts; never silently drops cache.
    """
    prompt = _nonneg_int(tokens.get("llm_prompt_tokens"))
    output = _nonneg_int(tokens.get("llm_completion_tokens"))
    cache_read = _nonneg_int(tokens.get("llm_cached_read_tokens"))
    cache_write = _nonneg_int(tokens.get("llm_cached_write_tokens"))

    convention = provider_cache_convention(provider)
    breakdown = {field: tokens.get(field) for field in _BURN_BREAKDOWN_FIELDS}

    if convention is None:
        # Conservative: prompt + output, no cache (cache stays in breakdown).
        return {
            "turn_token_burn": prompt + output,
            "confidence": BURN_CONFIDENCE_CONVENTION_PARTIAL,
            "convention": None,
            "breakdown": breakdown,
        }

    burn = prompt + output
    if not convention["prompt_includes_cache_read"]:
        burn += cache_read
    if not convention["prompt_includes_cache_write"]:
        burn += cache_write
    return {
        "turn_token_burn": burn,
        "confidence": BURN_CONFIDENCE_FULL,
        "convention": dict(convention),
        "breakdown": breakdown,
    }


__all__ = [
    "CANONICAL_TOKEN_FIELDS",
    "extract_anthropic_usage",
    "extract_openai_usage",
    "extract_langchain_usage_metadata",
    "extract_from_langchain_response",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI",
    "BURN_CONFIDENCE_FULL",
    "BURN_CONFIDENCE_CONVENTION_PARTIAL",
    "provider_cache_convention",
    "derive_turn_token_burn",
]
