"""CP2 — obligations 12-16 (cross-ROOT turn state) and 21-23 (cross-BRANCH
turn state within one root). These encode the captured contamination shape."""
import asyncio

from conftest import as_dict, make_handler, rid


class _Resp:
    """Minimal stand-in shaped like a LangChain LLM response with usage."""
    def __init__(self, total, model):
        self.llm_output = {
            "token_usage": {"prompt_tokens": 1, "completion_tokens": total - 1,
                            "total_tokens": total},
            "model_name": model,
        }
        self.generations = []


def snapshots(sink):
    """Events carrying turn telemetry. It lives in `payload`, not at top level."""
    out = []
    for e in sink.events:
        p = (as_dict(e).get("payload") or {})
        if any(k in p for k in ("llm_turn_id", "llm_completion_tokens", "model_identifier")):
            out.append(p)
    return out


def tele(d):
    return (d.get("llm_turn_id"), d.get("total_tokens"), d.get("model_identifier"))


# ------------------------------------------------- 12/13/14: across ROOTS
def _overlapping_roots_with_llm():
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)
    h.on_llm_start({}, ["a"], run_id=rid(), parent_run_id=a)      # A's turn opens
    h.on_llm_start({}, ["b"], run_id=rid(), parent_run_id=b)      # B's opens inside it
    h.on_llm_end(_Resp(7, "model-B"), run_id=rid(), parent_run_id=b)
    h.on_tool_start({"name": "read_thing"}, "b", run_id=rid(), parent_run_id=b)
    h.on_llm_end(_Resp(100, "model-A"), run_id=rid(), parent_run_id=a)
    h.on_tool_start({"name": "read_thing"}, "a", run_id=rid(), parent_run_id=a)
    return sink, h


def test_12_overlapping_roots_do_not_exchange_token_usage():
    sink, _ = _overlapping_roots_with_llm()
    snaps = snapshots(sink)
    totals = {d.get("llm_completion_tokens") for d in snaps if d.get("llm_completion_tokens")}
    assert totals <= {6, 99}, "usage must not be blended across roots"
    assert 6 in totals and 99 in totals, "each root must keep its own usage"


def test_13_overlapping_roots_do_not_exchange_model_or_provider():
    """Each root's tool event must carry ITS OWN model, paired with its own
    turn id. A mixed record is the captured failure shape."""
    sink, _ = _overlapping_roots_with_llm()
    snaps = snapshots(sink)
    assert len(snaps) >= 2, "both roots must emit turn telemetry"
    models = [d.get("model_identifier") for d in snaps]
    assert "model-A" in models and "model-B" in models, \
        "each root must report its own model, not a shared one"
    assert len(set(models)) == 2, "model must not be shared across two roots"


def test_14_overlapping_roots_do_not_exchange_llm_turn_id():
    """Two overlapping roots must never share one llm_turn_id."""
    sink, _ = _overlapping_roots_with_llm()
    ids = [d.get("llm_turn_id") for d in snapshots(sink) if d.get("llm_turn_id")]
    assert len(ids) >= 2, "both roots must emit a turn id"
    assert len(set(ids)) == len(ids), \
        "two overlapping roots must not share one llm_turn_id"


def test_15_llm_start_on_root_a_does_not_reset_root_b():
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)
    h.on_llm_start({}, ["b"], run_id=rid(), parent_run_id=b)
    h.on_llm_end(_Resp(7, "model-B"), run_id=rid(), parent_run_id=b)
    h.on_llm_start({}, ["a"], run_id=rid(), parent_run_id=a)   # must not clear B
    h.on_tool_start({"name": "read_thing"}, "b", run_id=rid(), parent_run_id=b)
    snaps = snapshots(sink)
    assert any(d.get("llm_completion_tokens") == 6 for d in snaps), \
        "root B's usage must survive root A's on_llm_start"


def test_16_turn_id_persists_within_one_root():
    """REGRESSION: one turn id across every event of that turn."""
    sink, h = make_handler(declared=["read_thing"])
    a = rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_llm_start({}, ["a"], run_id=rid(), parent_run_id=a)
    h.on_tool_start({"name": "read_thing"}, "1", run_id=rid(), parent_run_id=a)
    h.on_tool_start({"name": "read_thing"}, "2", run_id=rid(), parent_run_id=a)
    h.on_llm_end(_Resp(5, "m"), run_id=rid(), parent_run_id=a)
    ids = {d.get("llm_turn_id") for d in snapshots(sink) if d.get("llm_turn_id")}
    assert ids, "the turn must emit at least one telemetry-bearing event"
    assert len(ids) == 1, "all events of one turn share one llm_turn_id"


# ------------------------------- 21/22/23: across BRANCHES in ONE root
def _parallel_branches(sink, h, root, br_a, br_b):
    """Replays the captured interleaving: B opens inside A and closes first."""
    h.on_chain_start({"name": "A"}, {}, run_id=br_a, parent_run_id=root)
    h.on_llm_start({}, ["a"], run_id=rid(), parent_run_id=br_a)     # A turn opens
    h.on_chain_start({"name": "B"}, {}, run_id=br_b, parent_run_id=root)
    h.on_llm_start({}, ["b"], run_id=rid(), parent_run_id=br_b)     # opens INSIDE A
    h.on_llm_end(_Resp(7, "model-B"), run_id=rid(), parent_run_id=br_b)
    h.on_tool_start({"name": "read_thing"}, "b", run_id=rid(), parent_run_id=br_b)
    h.on_llm_end(_Resp(100, "model-A"), run_id=rid(), parent_run_id=br_a)
    h.on_tool_start({"name": "read_thing"}, "a", run_id=rid(), parent_run_id=br_a)


def test_21_parallel_branches_do_not_contaminate_turn_telemetry_sync():
    """DEFECT, the captured shape: A's tool must not receive B's llm_turn_id,
    and usage/model must stay branch-correct."""
    sink, h = make_handler(declared=["read_thing"])
    root, br_a, br_b = rid(), rid(), rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)
    _parallel_branches(sink, h, root, br_a, br_b)

    snaps = [d for d in snapshots(sink)]
    by_total = {d.get("llm_completion_tokens"): d for d in snaps if d.get("llm_completion_tokens")}
    assert 6 in by_total and 99 in by_total, "both branches must report usage"
    a_evt, b_evt = by_total[99], by_total[6]
    assert a_evt.get("llm_turn_id") != b_evt.get("llm_turn_id"), \
        "A's tool must NOT carry B's llm_turn_id"
    assert a_evt.get("model_identifier") == "model-A"
    assert b_evt.get("model_identifier") == "model-B"


def test_22_parallel_branches_do_not_contaminate_async():
    sink, h = make_handler(declared=["read_thing"])
    root, br_a, br_b = rid(), rid(), rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)

    async def branch(br, tag, total, model, delay):
        h.on_chain_start({"name": tag}, {}, run_id=br, parent_run_id=root)
        h.on_llm_start({}, [tag], run_id=rid(), parent_run_id=br)
        await asyncio.sleep(delay)
        h.on_llm_end(_Resp(total, model), run_id=rid(), parent_run_id=br)
        h.on_tool_start({"name": "read_thing"}, tag, run_id=rid(), parent_run_id=br)

    async def main():
        await asyncio.gather(branch(br_a, "a", 100, "model-A", 0.05),
                             branch(br_b, "b", 7, "model-B", 0.01))
    asyncio.run(main())

    by_total = {d.get("llm_completion_tokens"): d for d in snapshots(sink) if d.get("llm_completion_tokens")}
    assert 6 in by_total and 99 in by_total
    assert by_total[99].get("llm_turn_id") != by_total[6].get("llm_turn_id"), \
        "async: A's tool must NOT carry B's llm_turn_id"


def test_23_tool_nested_deeper_than_its_node_uses_correct_branch_scope():
    """A tool under a sub-chain inside a branch must resolve to that branch."""
    sink, h = make_handler(declared=["read_thing"])
    root, br_a, br_b, sub_a = rid(), rid(), rid(), rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)
    h.on_chain_start({"name": "A"}, {}, run_id=br_a, parent_run_id=root)
    h.on_llm_start({}, ["a"], run_id=rid(), parent_run_id=br_a)
    h.on_chain_start({"name": "B"}, {}, run_id=br_b, parent_run_id=root)
    h.on_llm_start({}, ["b"], run_id=rid(), parent_run_id=br_b)
    h.on_llm_end(_Resp(7, "model-B"), run_id=rid(), parent_run_id=br_b)
    h.on_llm_end(_Resp(100, "model-A"), run_id=rid(), parent_run_id=br_a)
    h.on_chain_start({"name": "subA"}, {}, run_id=sub_a, parent_run_id=br_a)
    h.on_tool_start({"name": "read_thing"}, "deep-a", run_id=rid(), parent_run_id=sub_a)

    snaps = [d for d in snapshots(sink) if d.get("llm_completion_tokens")]
    assert snaps, "deep tool must carry branch telemetry"
    assert snaps[-1].get("llm_completion_tokens") == 99, \
        "deep tool under branch A must carry A's usage, not B's"
    assert snaps[-1].get("model_identifier") == "model-A"
