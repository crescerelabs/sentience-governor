"""Tests for sentience_governor.wrapper.token_extraction.

Covers the v0.2.3 Track 2 extraction module per
Token-extraction contract:

  * Normalization rules (§1.1) — every defensive demote-to-None case
  * Per-shape extractors (Anthropic, OpenAI, LangChain canonical)
  * Top-level merge precedence (§2.3) — Anthropic-via-LangChain real
    case where cache fields would be lost without merge logic
  * Defensive: extraction never raises, even on malformed inputs
"""

from __future__ import annotations

from typing import Any

import pytest

from sentience_governor.wrapper.token_extraction import (
    CANONICAL_TOKEN_FIELDS,
    _coerce_token_value,
    _empty_canonical,
    _merge_canonical,
    extract_anthropic_usage,
    extract_from_langchain_response,
    extract_langchain_usage_metadata,
    extract_openai_usage,
)


# ---------------------------------------------------------------------------
# §1.1 normalization rules
# ---------------------------------------------------------------------------


class TestCoerceTokenValue:
    """One test per row of the §1.1 normalization table."""

    def test_none_passes_through(self) -> None:
        assert _coerce_token_value(None) is None

    def test_zero_preserved(self) -> None:
        # Zero is a real measurement, not absence.
        assert _coerce_token_value(0) == 0

    def test_positive_int_preserved(self) -> None:
        assert _coerce_token_value(1234) == 1234

    def test_negative_int_demotes_to_none(self) -> None:
        assert _coerce_token_value(-1) is None

    def test_integral_float_coerced_to_int(self) -> None:
        result = _coerce_token_value(1234.0)
        assert result == 1234
        assert isinstance(result, int)

    def test_non_integral_float_demotes_to_none(self) -> None:
        assert _coerce_token_value(1234.5) is None

    def test_negative_float_demotes_to_none(self) -> None:
        assert _coerce_token_value(-1.0) is None

    def test_numeric_string_coerced(self) -> None:
        assert _coerce_token_value("1234") == 1234

    def test_numeric_string_with_leading_zeros_coerced(self) -> None:
        # int("00123") works in Python; we accept it.
        assert _coerce_token_value("00123") == 123

    def test_numeric_string_with_whitespace_coerced(self) -> None:
        # Python's int() strips whitespace; we accept this default.
        assert _coerce_token_value(" 123 ") == 123

    def test_negative_numeric_string_demotes_to_none(self) -> None:
        assert _coerce_token_value("-1") is None

    def test_non_numeric_string_demotes_to_none(self) -> None:
        assert _coerce_token_value("hello") is None

    def test_empty_string_demotes_to_none(self) -> None:
        assert _coerce_token_value("") is None

    def test_whitespace_only_string_demotes_to_none(self) -> None:
        # int("  ") raises ValueError; falls through to None.
        assert _coerce_token_value("  ") is None

    def test_bool_true_demotes_to_none(self) -> None:
        # bool inherits from int but is never a valid token count.
        assert _coerce_token_value(True) is None

    def test_bool_false_demotes_to_none(self) -> None:
        assert _coerce_token_value(False) is None

    def test_list_demotes_to_none(self) -> None:
        assert _coerce_token_value([1, 2, 3]) is None

    def test_dict_demotes_to_none(self) -> None:
        assert _coerce_token_value({"value": 123}) is None

    def test_object_demotes_to_none(self) -> None:
        assert _coerce_token_value(object()) is None


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------


class TestMergeCanonical:
    def test_overlay_fills_none_slots(self) -> None:
        base = _empty_canonical()
        overlay = {field: 100 for field in CANONICAL_TOKEN_FIELDS}
        result = _merge_canonical(base, overlay)
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] == 100

    def test_higher_precedence_never_overwritten(self) -> None:
        base = {field: 999 for field in CANONICAL_TOKEN_FIELDS}
        overlay = {field: 1 for field in CANONICAL_TOKEN_FIELDS}
        result = _merge_canonical(base, overlay)
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] == 999

    def test_partial_fill(self) -> None:
        base = {**_empty_canonical(), "llm_prompt_tokens": 50}
        overlay = {**_empty_canonical(), "llm_completion_tokens": 30}
        result = _merge_canonical(base, overlay)
        assert result["llm_prompt_tokens"] == 50
        assert result["llm_completion_tokens"] == 30
        assert result["llm_cached_read_tokens"] is None

    def test_overlay_none_does_not_overwrite(self) -> None:
        base = {field: 5 for field in CANONICAL_TOKEN_FIELDS}
        overlay = _empty_canonical()  # all None
        result = _merge_canonical(base, overlay)
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] == 5

    def test_returns_new_dict_not_mutating_inputs(self) -> None:
        base = _empty_canonical()
        overlay = {field: 10 for field in CANONICAL_TOKEN_FIELDS}
        result = _merge_canonical(base, overlay)
        # Inputs unchanged.
        assert base["llm_prompt_tokens"] is None
        assert overlay["llm_prompt_tokens"] == 10
        # Result is independent.
        assert result["llm_prompt_tokens"] == 10


# ---------------------------------------------------------------------------
# Per-shape extractors
# ---------------------------------------------------------------------------


class _AnthropicUsage:
    """Mimics anthropic.types.Message.usage (object with attributes)."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestExtractAnthropicUsage:
    def test_native_usage_object(self) -> None:
        usage = _AnthropicUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=30,
        )
        result = extract_anthropic_usage(usage)
        assert result == {
            "llm_prompt_tokens": 100,
            "llm_completion_tokens": 50,
            "llm_cached_read_tokens": 200,
            "llm_cached_write_tokens": 30,
            "llm_reasoning_tokens": None,
        }

    def test_dict_shape(self) -> None:
        # Anthropic-via-LangChain uses dicts under response_metadata['usage'].
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 30,
        }
        result = extract_anthropic_usage(usage)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_cached_read_tokens"] == 200
        assert result["llm_cached_write_tokens"] == 30

    def test_missing_cache_fields_yield_none(self) -> None:
        usage = _AnthropicUsage(input_tokens=100, output_tokens=50)
        result = extract_anthropic_usage(usage)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50
        assert result["llm_cached_read_tokens"] is None
        assert result["llm_cached_write_tokens"] is None

    def test_zero_preserved(self) -> None:
        usage = _AnthropicUsage(input_tokens=0, output_tokens=0)
        result = extract_anthropic_usage(usage)
        assert result["llm_prompt_tokens"] == 0
        assert result["llm_completion_tokens"] == 0


class TestExtractOpenAIUsage:
    def test_basic_dict(self) -> None:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        result = extract_openai_usage(usage)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50
        # OpenAI doesn't separately report cache writes.
        assert result["llm_cached_write_tokens"] is None

    def test_with_cached_tokens(self) -> None:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
        result = extract_openai_usage(usage)
        assert result["llm_cached_read_tokens"] == 30
        # OpenAI's prompt_tokens INCLUDES cached. We preserve raw,
        # don't normalize.
        assert result["llm_prompt_tokens"] == 100

    def test_with_reasoning_tokens(self) -> None:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
        result = extract_openai_usage(usage)
        assert result["llm_reasoning_tokens"] == 20

    def test_non_dict_input_returns_all_none(self) -> None:
        result = extract_openai_usage("not a dict")
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] is None

    def test_malformed_details_does_not_raise(self) -> None:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": "not a dict",
        }
        result = extract_openai_usage(usage)
        # Doesn't raise; cache field falls through to None.
        assert result["llm_cached_read_tokens"] is None


class TestExtractLangChainUsageMetadata:
    def test_basic_metadata(self) -> None:
        metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        result = extract_langchain_usage_metadata(metadata)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50

    def test_with_nested_details(self) -> None:
        metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 20, "cache_creation": 10},
            "output_token_details": {"reasoning": 5},
        }
        result = extract_langchain_usage_metadata(metadata)
        assert result["llm_cached_read_tokens"] == 20
        assert result["llm_cached_write_tokens"] == 10
        assert result["llm_reasoning_tokens"] == 5

    def test_non_dict_input_returns_all_none(self) -> None:
        result = extract_langchain_usage_metadata(None)
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] is None


# ---------------------------------------------------------------------------
# Top-level merge — the load-bearing case
# ---------------------------------------------------------------------------


class _FakeLangChainResponse:
    """Stand-in for a LangChain response object."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestExtractFromLangChainResponse:
    def test_usage_metadata_only(self) -> None:
        response = _FakeLangChainResponse(
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
        )
        result = extract_from_langchain_response(response)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50

    def test_legacy_llm_output_token_usage(self) -> None:
        response = _FakeLangChainResponse(
            llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}},
        )
        result = extract_from_langchain_response(response)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50

    def test_response_metadata_anthropic_only(self) -> None:
        response = _FakeLangChainResponse(
            response_metadata={
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 10,
                },
            },
        )
        result = extract_from_langchain_response(response)
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_cached_read_tokens"] == 30
        assert result["llm_cached_write_tokens"] == 10

    def test_anthropic_via_langchain_merge_preserves_cache_fields(self) -> None:
        """The load-bearing test from §2.3.

        Real-world Anthropic-via-LangChain responses have BOTH:
          - usage_metadata (input_tokens, output_tokens — canonical LC)
          - response_metadata['usage'] (cache fields — Anthropic-only)

        Early-returning on usage_metadata would silently drop the
        cache fields. The merge logic must preserve them.
        """
        response = _FakeLangChainResponse(
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
            response_metadata={
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 10,
                },
            },
        )
        result = extract_from_langchain_response(response)
        # Token counts populated from highest-precedence (response_metadata).
        assert result["llm_prompt_tokens"] == 100
        assert result["llm_completion_tokens"] == 50
        # Cache fields preserved — would have been LOST by early-return.
        assert result["llm_cached_read_tokens"] == 30
        assert result["llm_cached_write_tokens"] == 10

    def test_higher_precedence_value_not_overwritten_by_lower(self) -> None:
        """Even if lower-precedence sources have different values, they
        do not overwrite higher-precedence ones."""
        response = _FakeLangChainResponse(
            response_metadata={
                "usage": {"input_tokens": 999, "output_tokens": 999},
            },
            usage_metadata={"input_tokens": 1, "output_tokens": 1},
            llm_output={"token_usage": {"prompt_tokens": 0, "completion_tokens": 0}},
        )
        result = extract_from_langchain_response(response)
        # Highest-precedence (response_metadata) wins.
        assert result["llm_prompt_tokens"] == 999
        assert result["llm_completion_tokens"] == 999

    def test_no_data_returns_all_none(self) -> None:
        response = _FakeLangChainResponse()
        result = extract_from_langchain_response(response)
        for field in CANONICAL_TOKEN_FIELDS:
            assert result[field] is None

    def test_malformed_response_metadata_does_not_raise(self) -> None:
        response = _FakeLangChainResponse(
            response_metadata="not a dict",
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
        )
        # Must not raise — extraction is exception-safe.
        result = extract_from_langchain_response(response)
        # Fell through to usage_metadata.
        assert result["llm_prompt_tokens"] == 100

    def test_malformed_token_values_normalize_to_none(self) -> None:
        response = _FakeLangChainResponse(
            usage_metadata={
                "input_tokens": "not a number",
                "output_tokens": -50,
            },
        )
        result = extract_from_langchain_response(response)
        assert result["llm_prompt_tokens"] is None
        assert result["llm_completion_tokens"] is None
