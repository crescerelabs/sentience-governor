"""Tests for the v0.2.6.1 CP1 burn-derivation helpers.

Covers `provider_cache_convention` + `derive_turn_token_burn` in
`sentience_governor.wrapper.token_extraction` — the single place that
codifies how provider-native token categories compose into a
non-double-counting `turn_token_burn` (plan §D2 / §R3, acceptance #6/#12).

The load-bearing cases:
  * Anthropic / cache-additive convention → prompt + output + cache_read +
    cache_write.
  * OpenAI / cache-inclusive convention → prompt + output (cache_read already
    inside prompt; adding it would double-count).
  * Unknown provider → conservative prompt + output, cache preserved in the
    breakdown, marked convention-partial.
"""

from __future__ import annotations

from sentience_governor.wrapper.token_extraction import (
    BURN_CONFIDENCE_CONVENTION_PARTIAL,
    BURN_CONFIDENCE_FULL,
    derive_turn_token_burn,
    provider_cache_convention,
)


def _tokens(prompt=None, output=None, cache_read=None, cache_write=None, reasoning=None):
    return {
        "llm_prompt_tokens": prompt,
        "llm_completion_tokens": output,
        "llm_cached_read_tokens": cache_read,
        "llm_cached_write_tokens": cache_write,
        "llm_reasoning_tokens": reasoning,
    }


class TestProviderCacheConvention:
    def test_anthropic_excludes_both_cache_categories(self):
        conv = provider_cache_convention("anthropic")
        assert conv == {
            "prompt_includes_cache_read": False,
            "prompt_includes_cache_write": False,
        }

    def test_openai_includes_cache_read_only(self):
        conv = provider_cache_convention("openai")
        assert conv == {
            "prompt_includes_cache_read": True,
            "prompt_includes_cache_write": False,
        }

    def test_case_and_whitespace_insensitive(self):
        assert provider_cache_convention("  Anthropic ") == provider_cache_convention(
            "anthropic"
        )

    def test_unknown_provider_returns_none(self):
        assert provider_cache_convention("cohere") is None
        assert provider_cache_convention("") is None
        assert provider_cache_convention(None) is None
        assert provider_cache_convention(123) is None


class TestDeriveTurnTokenBurn:
    def test_anthropic_is_cache_additive(self):
        # The verified real turn: 2 + 319 + 38631 + 330 = 39282.
        result = derive_turn_token_burn(
            _tokens(prompt=2, output=319, cache_read=38631, cache_write=330),
            "anthropic",
        )
        assert result["turn_token_burn"] == 39282
        assert result["confidence"] == BURN_CONFIDENCE_FULL
        assert result["convention"] == {
            "prompt_includes_cache_read": False,
            "prompt_includes_cache_write": False,
        }

    def test_openai_does_not_double_count_cache_read(self):
        # prompt already includes the 1000 cached-read tokens → burn = 100 + 20.
        result = derive_turn_token_burn(
            _tokens(prompt=100, output=20, cache_read=1000, cache_write=None),
            "openai",
        )
        assert result["turn_token_burn"] == 120
        assert result["confidence"] == BURN_CONFIDENCE_FULL

    def test_same_tokens_different_convention_diverge(self):
        toks = _tokens(prompt=100, output=20, cache_read=1000, cache_write=50)
        anthropic = derive_turn_token_burn(toks, "anthropic")["turn_token_burn"]
        openai = derive_turn_token_burn(toks, "openai")["turn_token_burn"]
        # Anthropic adds BOTH caches (prompt excludes them). OpenAI skips
        # cache_read (already inside prompt) but still adds cache_write (not in
        # prompt) — so the metric is NOT a flat cross-runtime sum. (Real OpenAI
        # traces report no cache_write; the synthetic 50 here just exercises the
        # independent-flags path.)
        assert anthropic == 100 + 20 + 1000 + 50  # 1170
        assert openai == 100 + 20 + 50  # 170 — cache_read excluded, cache_write added
        assert anthropic != openai

    def test_openai_realistic_no_cache_write(self):
        # Real OpenAI usage: cache_write is None → burn = prompt + output only.
        toks = _tokens(prompt=100, output=20, cache_read=1000, cache_write=None)
        assert derive_turn_token_burn(toks, "openai")["turn_token_burn"] == 120

    def test_unknown_provider_is_conservative_and_partial(self):
        result = derive_turn_token_burn(
            _tokens(prompt=100, output=20, cache_read=1000, cache_write=50),
            "mystery-runtime",
        )
        # Conservative: prompt + output only, never overcount.
        assert result["turn_token_burn"] == 120
        assert result["confidence"] == BURN_CONFIDENCE_CONVENTION_PARTIAL
        assert result["convention"] is None

    def test_unknown_provider_preserves_cache_in_breakdown(self):
        # Cache evidence must never be silently dropped, even when partial.
        result = derive_turn_token_burn(
            _tokens(prompt=100, output=20, cache_read=1000, cache_write=50),
            None,
        )
        assert result["breakdown"]["llm_cached_read_tokens"] == 1000
        assert result["breakdown"]["llm_cached_write_tokens"] == 50

    def test_none_fields_contribute_zero(self):
        result = derive_turn_token_burn(_tokens(prompt=None, output=None), "anthropic")
        assert result["turn_token_burn"] == 0

    def test_negative_and_bool_values_demote_to_zero(self):
        result = derive_turn_token_burn(
            _tokens(prompt=-5, output=True, cache_read=10, cache_write=None),
            "anthropic",
        )
        # -5 → 0, True → 0, cache_read 10 added → 10.
        assert result["turn_token_burn"] == 10

    def test_reasoning_not_summed_into_burn(self):
        # Reasoning is preserved in breakdown but excluded from burn (parity
        # with the burn-rate analyzer's four-field model).
        result = derive_turn_token_burn(
            _tokens(prompt=10, output=20, reasoning=999), "anthropic"
        )
        assert result["turn_token_burn"] == 30
        assert result["breakdown"]["llm_reasoning_tokens"] == 999

    def test_breakdown_echoes_raw_values(self):
        toks = _tokens(prompt=2, output=319, cache_read=38631, cache_write=330)
        result = derive_turn_token_burn(toks, "anthropic")
        assert result["breakdown"]["llm_prompt_tokens"] == 2
        assert result["breakdown"]["llm_cached_read_tokens"] == 38631

    def test_byte_stable_repr(self):
        toks = _tokens(prompt=2, output=319, cache_read=38631, cache_write=330)
        a = derive_turn_token_burn(toks, "anthropic")
        b = derive_turn_token_burn(toks, "anthropic")
        assert repr(a) == repr(b)
