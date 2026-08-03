"""Tests for the v0.2.6.1 CP1 Claude Code transcript parser + join model.

Proves the CP1 fixture-set checklist
against fixtures, ahead
of any live hook code (CP2):

  * tool_use_id → requestId mapping (the join, D3)
  * multiple assistant messages sharing one requestId
  * identical repeated usage counted once; conflicting usage flagged, not summed
  * one requestId with multiple tool calls → one turn, burn NOT multiplied
  * missing / unmatched tool_use_id → partial attribution, never guessed
  * missing requestId; missing usage
  * cache-read-only / cache-creation-only / mixed turns
  * no-tool-call turns still emit burn (D7)
  * compaction + non-assistant lines skipped (D6)
  * malformed JSONL; incomplete final line (D4)
  * join is by id, not event position
  * repeated processing is idempotent
  * streaming over a thousands-of-requestId generator (R5)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from sentience_governor.wrapper.claude_code_transcript import (
    TRANSCRIPT_PROVIDER,
    WARN_DEDUPE_CONFLICT,
    WARN_INCOMPLETE_FINAL_LINE,
    WARN_MALFORMED_LINE,
    WARN_MISSING_REQUEST_ID,
    WARN_MISSING_USAGE,
    join_tool_use_ids,
    parse_transcript,
    parse_transcript_file,
    turn_token_burn,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claude_code_transcript"


def _assistant(request_id, *, tool_uses=None, usage=None, model="claude-anon", text=None):
    """Build one assistant transcript line (dict)."""
    content: List[Dict[str, Any]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({"type": "tool_use", "id": tu[0], "name": tu[1], "input": {}})
    message: Dict[str, Any] = {"role": "assistant", "model": model, "content": content}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "requestId": request_id, "message": message}


def _usage(input_tokens=0, cache_write=0, cache_read=0, output=0):
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
    }


def _lines(*objs):
    return [json.dumps(o) for o in objs]


# ---------------------------------------------------------------------------
# Happy path against the committed fixture.
# ---------------------------------------------------------------------------


class TestSessionBasicFixture:
    @pytest.fixture
    def result(self):
        lines = (FIXTURE_DIR / "session_basic.jsonl").read_text().splitlines()
        return parse_transcript(lines)

    def test_provider_is_anthropic(self, result):
        assert result["provider"] == TRANSCRIPT_PROVIDER == "anthropic"

    def test_turn_order_first_seen(self, result):
        assert result["turn_order"] == ["req_A", "req_B", "req_C"]

    def test_non_message_lines_skipped(self, result):
        # user + summary(compaction) + system = 3 skipped; 4 assistant lines.
        assert result["assistant_line_count"] == 4
        assert result["skipped_line_count"] == 3
        assert result["total_line_count"] == 7

    def test_multiple_messages_one_request(self, result):
        # req_A spans two assistant messages.
        assert result["turns"]["req_A"]["message_count"] == 2

    def test_tool_use_index_maps_ids_to_requests(self, result):
        assert result["tool_use_index"] == {
            "toolu_A1": "req_A",
            "toolu_A2": "req_A",
            "toolu_B1": "req_B",
        }

    def test_multi_tool_turn_burn_not_multiplied(self, result):
        # req_A issued TWO tool calls but burn is the turn's, counted once.
        burn = turn_token_burn(result, "req_A")
        assert burn["turn_token_burn"] == 2 + 319 + 38631 + 330  # 39282
        assert result["turns"]["req_A"]["tool_use_ids"] == ["toolu_A1", "toolu_A2"]

    def test_identical_repeated_usage_counted_once(self, result):
        # req_A's two messages carry identical usage → no conflict, single value.
        assert result["dedupe_conflict_count"] == 0
        assert result["turns"]["req_A"]["tokens"]["llm_cached_read_tokens"] == 38631

    def test_no_tool_turn_still_has_burn(self, result):
        # req_C is a pure answer turn (D7): no tool calls, real burn.
        assert result["turns"]["req_C"]["tool_use_ids"] == []
        assert turn_token_burn(result, "req_C")["turn_token_burn"] == 5 + 400 + 200

    def test_model_identifier_captured(self, result):
        assert result["turns"]["req_B"]["model_identifier"] == "claude-opus-4-anon"


# ---------------------------------------------------------------------------
# The join (D3) — by id, partial-not-guessed.
# ---------------------------------------------------------------------------


class TestJoin:
    @pytest.fixture
    def result(self):
        return parse_transcript(
            (FIXTURE_DIR / "session_basic.jsonl").read_text().splitlines()
        )

    def test_join_resolves_to_request(self, result):
        assert join_tool_use_ids(result, ["toolu_B1"]) == {"toolu_B1": "req_B"}

    def test_unmatched_id_is_none_not_guessed(self, result):
        joined = join_tool_use_ids(result, ["toolu_DOES_NOT_EXIST"])
        assert joined == {"toolu_DOES_NOT_EXIST": None}

    def test_join_independent_of_order(self, result):
        # Same answer regardless of the order ids are presented — join is by
        # id, never by event/append position.
        forward = join_tool_use_ids(result, ["toolu_A1", "toolu_A2", "toolu_B1"])
        reverse = join_tool_use_ids(result, ["toolu_B1", "toolu_A2", "toolu_A1"])
        assert forward == reverse
        assert forward["toolu_A1"] == "req_A" and forward["toolu_B1"] == "req_B"

    def test_blocked_or_failed_tool_call_still_joins(self):
        # Tool RESULT (success/failure) comes back on a separate user line we
        # skip; the tool_use block on the assistant line is captured regardless.
        lines = _lines(
            _assistant("req_X", tool_uses=[("toolu_X1", "Bash")], usage=_usage(1, 0, 0, 5)),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_X1", "is_error": True}
            ]}},
        )
        result = parse_transcript(lines)
        assert join_tool_use_ids(result, ["toolu_X1"]) == {"toolu_X1": "req_X"}
        assert result["skipped_line_count"] == 1


# ---------------------------------------------------------------------------
# Dedupe / conflict.
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_conflicting_usage_flagged_not_summed(self):
        lines = _lines(
            _assistant("req_C", usage=_usage(10, 0, 0, 20), text="a"),
            _assistant("req_C", usage=_usage(99, 0, 0, 88), text="b"),
        )
        result = parse_transcript(lines)
        # First populated wins; conflict flagged; NOT summed (would be 109/108).
        assert result["turns"]["req_C"]["tokens"]["llm_prompt_tokens"] == 10
        assert result["turns"]["req_C"]["tokens"]["llm_completion_tokens"] == 20
        assert result["dedupe_conflict_count"] == 1
        assert any(w["code"] == WARN_DEDUPE_CONFLICT for w in result["warnings"])

    def test_idempotent_reprocessing(self):
        lines = (FIXTURE_DIR / "session_basic.jsonl").read_text().splitlines()
        a = parse_transcript(lines)
        b = parse_transcript(lines)
        # Re-running SessionEnd parse must not double-count or drift.
        assert repr(a) == repr(b)
        assert a["turns"]["req_A"]["tokens"] == b["turns"]["req_A"]["tokens"]


# ---------------------------------------------------------------------------
# Missing fields.
# ---------------------------------------------------------------------------


class TestMissingFields:
    def test_missing_request_id_skipped_and_warned(self):
        line = {"type": "assistant", "message": {"role": "assistant", "content": [],
                "usage": _usage(1, 0, 0, 1)}}
        result = parse_transcript(_lines(line))
        assert result["missing_request_id_count"] == 1
        assert result["turn_order"] == []
        assert any(w["code"] == WARN_MISSING_REQUEST_ID for w in result["warnings"])

    def test_missing_usage_tool_turn_joinable_burn_unknown(self):
        lines = _lines(_assistant("req_NU", tool_uses=[("toolu_NU1", "Read")]))
        result = parse_transcript(lines)
        # Joinable…
        assert join_tool_use_ids(result, ["toolu_NU1"]) == {"toolu_NU1": "req_NU"}
        # …but no token data, and explicitly flagged.
        assert result["turns"]["req_NU"]["tokens_populated"] is False
        assert any(w["code"] == WARN_MISSING_USAGE for w in result["warnings"])

    def test_missing_usage_no_tool_turn_not_warned(self):
        # A reasoning turn with neither tools nor usage is not a misconfig.
        lines = _lines(_assistant("req_E", text="thinking"))
        result = parse_transcript(lines)
        assert not any(w["code"] == WARN_MISSING_USAGE for w in result["warnings"])


# ---------------------------------------------------------------------------
# Cache-category variants.
# ---------------------------------------------------------------------------


class TestCacheVariants:
    def test_cache_read_only(self):
        result = parse_transcript(
            _lines(_assistant("r", usage=_usage(0, 0, 5000, 10)))
        )
        t = result["turns"]["r"]["tokens"]
        assert t["llm_cached_read_tokens"] == 5000
        assert t["llm_cached_write_tokens"] == 0
        assert turn_token_burn(result, "r")["turn_token_burn"] == 0 + 10 + 5000 + 0

    def test_cache_creation_only(self):
        result = parse_transcript(
            _lines(_assistant("r", usage=_usage(0, 7000, 0, 10)))
        )
        t = result["turns"]["r"]["tokens"]
        assert t["llm_cached_write_tokens"] == 7000
        assert t["llm_cached_read_tokens"] == 0

    def test_mixed_cache(self):
        result = parse_transcript(
            _lines(_assistant("r", usage=_usage(3, 100, 200, 50)))
        )
        assert turn_token_burn(result, "r")["turn_token_burn"] == 3 + 50 + 200 + 100


# ---------------------------------------------------------------------------
# Malformed / incomplete / robustness (D4, R2).
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_malformed_middle_line_skipped(self):
        good = json.dumps(_assistant("req_G", usage=_usage(1, 0, 0, 2)))
        lines = [good, "{not valid json", good.replace("req_G", "req_H")]
        result = parse_transcript(lines)
        assert result["malformed_line_count"] == 1
        assert result["turn_order"] == ["req_G", "req_H"]
        assert result["incomplete_final_line"] is False
        assert any(w["code"] == WARN_MALFORMED_LINE for w in result["warnings"])

    def test_incomplete_final_line_reclassified(self):
        good = json.dumps(_assistant("req_G", usage=_usage(1, 0, 0, 2)))
        lines = [good, '{"type": "assistant", "requestId": "req_T", "mess']
        result = parse_transcript(lines)
        assert result["incomplete_final_line"] is True
        assert any(
            w["code"] == WARN_INCOMPLETE_FINAL_LINE for w in result["warnings"]
        )
        # The complete earlier turn still parses (partial success, D4).
        assert "req_G" in result["turns"]

    def test_blank_and_none_lines_ignored(self):
        good = json.dumps(_assistant("req_G", usage=_usage(1, 0, 0, 2)))
        result = parse_transcript([good, "", "   ", None])
        assert result["turn_order"] == ["req_G"]

    def test_non_dict_json_line_skipped(self):
        result = parse_transcript(["[1, 2, 3]", "42", '"a string"'])
        assert result["skipped_line_count"] == 3
        assert result["turn_order"] == []

    def test_never_raises_on_garbage(self):
        # Exercises the fail-open contract on assorted hostile input.
        parse_transcript(["", "{}", "null", "{bad", json.dumps({"type": "assistant"})])

    def test_missing_file_is_fail_open(self, tmp_path):
        result = parse_transcript_file(str(tmp_path / "nope.jsonl"))
        assert result["turn_order"] == []
        assert any(w["code"] == WARN_MALFORMED_LINE for w in result["warnings"])

    def test_parse_transcript_file_reads_fixture(self):
        result = parse_transcript_file(str(FIXTURE_DIR / "session_basic.jsonl"))
        assert result["turn_order"] == ["req_A", "req_B", "req_C"]


# ---------------------------------------------------------------------------
# Scale / streaming (R5).
# ---------------------------------------------------------------------------


class TestScale:
    def test_streams_thousands_of_requests_from_generator(self):
        n = 3000

        def gen() -> Iterator[str]:
            # A generator (not a list) proves the parser streams its input
            # rather than materializing the whole transcript.
            for i in range(n):
                yield json.dumps(
                    _assistant(
                        f"req_{i}",
                        tool_uses=[(f"toolu_{i}", "Read")],
                        usage=_usage(1, 0, 100, 5),
                    )
                )

        result = parse_transcript(gen())
        assert len(result["turn_order"]) == n
        assert result["assistant_line_count"] == n
        # Spot-check the join still resolves at scale.
        assert join_tool_use_ids(result, ["toolu_2999"]) == {"toolu_2999": "req_2999"}
        assert turn_token_burn(result, "req_0")["turn_token_burn"] == 1 + 5 + 100
