"""0.1.1 C2 — per-run profile resolution through the capability.

Locked plan §5 and §8.1 items 1-7 and 9-11. What is proven: a run resolves
the profile for its ``agent_id`` once when its session opens (binding, then
default, then none; a failed first match is ``degraded`` and never falls
through to a later binding), the resolved profile is bound through core's
existing ``session_start(profile=...)`` path, provenance is recorded on
``AGENT_REGISTERED`` for ``bound`` and ``degraded`` only, the run stays sticky
by construction, every resolution problem surfaces once at session open and
nothing raises into the developer's run.

Not proven here, by design: that a bound profile changes governance results
(``session_intent``, ``high_consequence.tools``). That is §8.1 item 8, C3.
"""

from __future__ import annotations

import asyncio
import json
import re
import warnings
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RequestUsage

from sentience_governor.profile import GovernanceProfile

from pydantic_ai_governor import SentienceGovernor

pytestmark = pytest.mark.anyio

HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
HEX12 = re.compile(r"^[0-9a-f]{12}$")

# Registration payload keys as 0.1.0 emitted them for a run with no profile:
# the byte-compatibility contract for the `none` path (§8.1 item 1).
REGISTRATION_KEYS_0_1_0 = {
    "agent_id", "agent_version", "declared_capabilities", "deployment_mode",
    "owner_claim", "policy_context", "vendor_id",
}

# Profiles that differ in content (hence fingerprint) without asserting any
# governance effect here; effects are C3.
PROFILE_A = "schema_version: 1\nsession_intent:\n  required: true\n  demand_at: never\n"
PROFILE_B = ("schema_version: 1\nsession_intent:\n  required: true\n  demand_at: never\n"
             "task_boundary:\n  signals: [time_gap]\n  time_gap_seconds: 120\n")
PROFILE_D = ("schema_version: 1\nsession_intent:\n  required: true\n  demand_at: never\n"
             "task_boundary:\n  signals: [dir_change]\n")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


class Config:
    """The operator's ~/.sentience as the suite-wide fixture redirected it."""

    def __init__(self, root: Path):
        self.root = root
        self.resolution = root / "resolution.yaml"
        self.default = root / "profile.yaml"
        (root / "profiles").mkdir(exist_ok=True)

    def profile(self, name: str, text: str) -> Path:
        p = self.root / "profiles" / name
        p.write_text(text, encoding="utf-8")
        return p

    def bind(self, *bindings: tuple) -> None:
        lines = ["schema_version: 1", "bindings:"]
        for pattern, target in bindings:
            lines += [f"  - agent_id: {pattern}", f"    profile: {target}"]
        self.resolution.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_default(self, text: str) -> Path:
        self.default.write_text(text, encoding="utf-8")
        return self.default

    @staticmethod
    def fingerprint(path: Path) -> str:
        return GovernanceProfile.from_file(path).fingerprint()


@pytest.fixture
def config(isolated_resolution_paths) -> Config:
    return Config(isolated_resolution_paths)


def gov(**kw: Any) -> SentienceGovernor:
    kw.setdefault("objective", "Resolve the profile")
    kw.setdefault("scope", ["crm"])
    kw.setdefault("agent_id", "pydantic-crm-agent")
    return SentienceGovernor(**kw)


def events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


def of_type(evs: List[dict], t: str) -> List[dict]:
    return [e for e in evs if e["event_type"] == t]


def registration(evs: List[dict]) -> dict:
    [r] = of_type(evs, "AGENT_REGISTERED")
    return r


def fingerprints(evs: List[dict]) -> set:
    return {e.get("profile_fingerprint") for e in evs}


def crm_fetch(account: str) -> str:
    return f"account {account}: ok"


def answering_model() -> FunctionModel:
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=9, output_tokens=3),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


def one_call_model() -> FunctionModel:
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="crm_fetch", args={"account": "A1"},
                                    tool_call_id="call-1")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
                model_name="m", provider_name="p")
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=30, output_tokens=7),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


async def run(g: SentienceGovernor, model=None, tools=()):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await Agent(model or answering_model(), tools=list(tools),
                             capabilities=[g]).run("go")
    return result, [w for w in caught if issubclass(w.category, UserWarning)]


def governance_errors(capsys) -> List[dict]:
    out = capsys.readouterr().out
    found = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event_type") == "GOVERNANCE_ERROR":
                found.append(ev)
    return found


# ---------------------------------------------------------------------------
# Item 1: none
# ---------------------------------------------------------------------------

async def test_no_resolution_and_no_default_is_the_0_1_0_shape(isolated_home, capsys):
    result, warned = await run(gov())
    evs = events(isolated_home, result.run_id)
    reg = registration(evs)
    assert set(reg["payload"].keys()) == REGISTRATION_KEYS_0_1_0
    assert "profile_resolution" not in reg["payload"] and "profile_binding" not in reg["payload"]
    assert all("profile_fingerprint" not in e for e in evs)
    assert warned == [] and governance_errors(capsys) == []


# ---------------------------------------------------------------------------
# Item 2: default
# ---------------------------------------------------------------------------

async def test_default_profile_only_fingerprints_every_event_without_provenance(isolated_home, config):
    fp = Config.fingerprint(config.set_default(PROFILE_A))
    assert HEX12.match(fp)
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    evs = events(isolated_home, result.run_id)
    assert len(evs) >= 4
    assert fingerprints(evs) == {fp}
    reg = registration(evs)["payload"]
    assert "profile_resolution" not in reg and "profile_binding" not in reg
    assert reg.get("profile_loaded") is True
    assert warned == []


# ---------------------------------------------------------------------------
# Item 3: bound
# ---------------------------------------------------------------------------

async def test_matching_binding_is_bound_with_pattern_and_fingerprint(isolated_home, config):
    a = config.profile("A.yaml", PROFILE_A)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    evs = events(isolated_home, result.run_id)
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "bound"
    assert reg["profile_binding"] == "pydantic-crm-*"
    assert fingerprints(evs) == {Config.fingerprint(a)}
    assert warned == []


# ---------------------------------------------------------------------------
# Item 4: first match wins
# ---------------------------------------------------------------------------

async def test_first_matching_binding_wins_over_a_later_catch_all(isolated_home, config):
    a = config.profile("A.yaml", PROFILE_A)
    b = config.profile("B.yaml", PROFILE_B)
    assert Config.fingerprint(a) != Config.fingerprint(b)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"), ("pydantic-*", "profiles/B.yaml"))
    result, _ = await run(gov())
    evs = events(isolated_home, result.run_id)
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "bound" and reg["profile_binding"] == "pydantic-crm-*"
    assert fingerprints(evs) == {Config.fingerprint(a)}

    # An agent the first pattern does not match falls to the catch-all.
    result2, _ = await run(gov(agent_id="pydantic-other"))
    reg2 = registration(events(isolated_home, result2.run_id))["payload"]
    assert reg2["profile_binding"] == "pydantic-*"
    assert fingerprints(events(isolated_home, result2.run_id)) == {Config.fingerprint(b)}


# ---------------------------------------------------------------------------
# Item 5: degraded
# ---------------------------------------------------------------------------

async def test_failed_first_match_is_degraded_uses_the_default_and_warns_once(isolated_home, config, capsys):
    config.profile("B.yaml", PROFILE_B)
    d = config.set_default(PROFILE_D)
    config.bind(("pydantic-crm-*", "profiles/missing.yaml"), ("pydantic-*", "profiles/B.yaml"))
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    evs = events(isolated_home, result.run_id)
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "degraded"
    assert reg["profile_binding"] == "pydantic-crm-*"          # the failed first match, retained
    assert fingerprints(evs) == {Config.fingerprint(d)}         # the default, not B
    # Exactly one developer-facing warning, at session open, none per tool call.
    assert len(warned) == 1
    assert "pydantic-crm-*" in str(warned[0].message) and "missing.yaml" in str(warned[0].message)
    errors = governance_errors(capsys)
    assert len(errors) == 1 and errors[0]["session_id"] == result.run_id
    assert "degraded" in errors[0]["payload"]["failure_reason"]
    assert result.output == "done"


async def test_degraded_without_a_default_resolves_to_no_profile(isolated_home, config):
    config.bind(("pydantic-crm-*", "profiles/missing.yaml"))
    result, warned = await run(gov())
    evs = events(isolated_home, result.run_id)
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "degraded" and reg["profile_binding"] == "pydantic-crm-*"
    assert all("profile_fingerprint" not in e for e in evs)
    assert len(warned) == 1


# ---------------------------------------------------------------------------
# Item 6: sticky by construction
# ---------------------------------------------------------------------------

async def test_configuration_changes_after_open_do_not_affect_the_run(isolated_home, config):
    a = config.profile("A.yaml", PROFILE_A)
    b = config.profile("B.yaml", PROFILE_B)
    fp_a, fp_b = Config.fingerprint(a), Config.fingerprint(b)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))

    def reconfigure(account: str) -> str:
        # Mid-run: repoint the binding to B and rewrite A itself.
        config.bind(("pydantic-crm-*", "profiles/B.yaml"))
        a.write_text(PROFILE_D, encoding="utf-8")
        return f"account {account}: reconfigured"

    result, _ = await run(gov(), model=one_call_model(), tools=[Tool(reconfigure, name="crm_fetch")])
    evs = events(isolated_home, result.run_id)
    # The configuration changed inside the run, at the tool call. Every event
    # recorded after that point (the tool's CONTEXT_SNAPSHOT and the second
    # model turn's token snapshot) still carries A's original identity.
    [asserted] = of_type(evs, "SCOPE_ASSERTED")
    later = [e for e in evs if e["event_sequence_number"] > asserted["event_sequence_number"]]
    assert len(later) >= 2
    assert fingerprints(later) == {fp_a}
    assert fingerprints(evs) == {fp_a}
    assert Config.fingerprint(a) != fp_a                        # the file really changed

    # A new run resolves fresh and follows the changed configuration.
    result2, _ = await run(gov())
    assert fingerprints(events(isolated_home, result2.run_id)) == {fp_b}


# ---------------------------------------------------------------------------
# Item 7: isolation across concurrent runs
# ---------------------------------------------------------------------------

async def test_concurrent_runs_with_different_agents_do_not_leak_profiles(isolated_home, config):
    a = config.profile("A.yaml", PROFILE_A)
    b = config.profile("B.yaml", PROFILE_B)
    config.bind(("agent-a", "profiles/A.yaml"), ("agent-b", "profiles/B.yaml"))
    ga, gb = gov(agent_id="agent-a"), gov(agent_id="agent-b")
    ra, rb = await asyncio.gather(
        Agent(one_call_model(), tools=[Tool(crm_fetch)], capabilities=[ga]).run("go"),
        Agent(one_call_model(), tools=[Tool(crm_fetch)], capabilities=[gb]).run("go"),
    )
    ea, eb = events(isolated_home, ra.run_id), events(isolated_home, rb.run_id)
    assert fingerprints(ea) == {Config.fingerprint(a)}
    assert fingerprints(eb) == {Config.fingerprint(b)}
    assert registration(ea)["payload"]["profile_binding"] == "agent-a"
    assert registration(eb)["payload"]["profile_binding"] == "agent-b"


async def test_concurrent_runs_on_one_agent_share_one_resolution(isolated_home, config):
    a = config.profile("A.yaml", PROFILE_A)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))
    g = gov()
    agent = Agent(answering_model(), capabilities=[g])
    r1, r2 = await asyncio.gather(agent.run("go"), agent.run("go"))
    assert r1.run_id != r2.run_id
    for r in (r1, r2):
        evs = events(isolated_home, r.run_id)
        assert fingerprints(evs) == {Config.fingerprint(a)}
        assert registration(evs)["payload"]["profile_resolution"] == "bound"


# ---------------------------------------------------------------------------
# Item 8: profile-driven governance through the companion
# ---------------------------------------------------------------------------

CRM_READ = {"sentience_governor": {
    "operation": "READ", "target_system": "crm", "classification": ["internal"]}}
PROFILE_GOV = (
    "schema_version: 1\n"
    "session_intent:\n  demand_at: never\n"
    "high_consequence:\n"
    "  tools: ['crm_fetch:crm']\n"
    "  operations:\n    - domain: network\n      action: read\n"
)
PROFILE_OPS_ONLY = (
    "schema_version: 1\n"
    "high_consequence:\n  operations:\n    - domain: network\n      action: read\n"
)


async def test_bound_profile_drives_governance_of_companion_events(isolated_home, config):
    """Core's profile transforms apply to the companion's events once a
    profile is bound: `demand_at: never` suppresses POL-001 on the scope
    assertion, and a `high_consequence.tools` pattern over
    `tool_name:target_system` flags it. The baseline run without a profile
    shows the same call producing POL-001 and no flag."""
    tool = Tool(crm_fetch, metadata=CRM_READ)
    baseline, warned0 = await run(gov(scope=["billing"]), model=one_call_model(), tools=[tool])
    scope0 = of_type(events(isolated_home, baseline.run_id), "SCOPE_ASSERTED")[0]
    assert warned0 == []
    assert "POL-001" in scope0["policy_violations"]
    assert "HIGH_CONSEQUENCE_DETECTED" not in (scope0.get("advisory_flags") or [])

    p = config.profile("gov.yaml", PROFILE_GOV)
    config.bind(("pydantic-*", "profiles/gov.yaml"))
    result, warned = await run(gov(scope=["billing"]), model=one_call_model(), tools=[tool])
    assert result.output == "done" and warned == []
    evs = events(isolated_home, result.run_id)
    scope = of_type(evs, "SCOPE_ASSERTED")[0]
    assert scope["payload"]["tool_id"] == "crm_fetch" and scope["payload"]["target_system"] == "crm"
    assert "POL-001" not in scope["policy_violations"]          # demand_at: never
    assert "HIGH_CONSEQUENCE_DETECTED" in scope["advisory_flags"]  # tools: ['crm_fetch:crm']
    assert fingerprints(evs) == {Config.fingerprint(p)}
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "bound" and reg["profile_binding"] == "pydantic-*"


async def test_operations_rules_never_fire_for_companion_events(isolated_home, config):
    """The companion emits no `operation_classification`, so a
    `high_consequence.operations` rule has nothing to match, even one that
    would match a network read. Pinned so nobody expects Bash semantics from
    a Pydantic tool call. The profile is bound and its fingerprint recorded;
    only the rule is inert."""
    d = config.set_default(PROFILE_OPS_ONLY)
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch, metadata=CRM_READ)])
    assert result.output == "done" and warned == []
    evs = events(isolated_home, result.run_id)
    assert all("operation_classification" not in (e.get("payload") or {}) for e in evs)
    scope = of_type(evs, "SCOPE_ASSERTED")[0]
    assert "HIGH_CONSEQUENCE_DETECTED" not in (scope.get("advisory_flags") or [])
    assert fingerprints(evs) == {Config.fingerprint(d)}


# ---------------------------------------------------------------------------
# Item 9: fail-open
# ---------------------------------------------------------------------------

async def test_malformed_default_profile_warns_once_and_runs_with_no_profile(isolated_home, config, capsys):
    # Unparseable YAML. Since core 0.3.2.1 the resolver's default step no
    # longer raises: it resolves `none` with a warning naming the file, and
    # the companion reports that warning once at session open. The run
    # completes with no profile either way.
    config.set_default("schema_version: 1\nsession_intent: [\n")
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "SCOPE_ASSERTED")) == 1
    assert all("profile_fingerprint" not in e for e in evs)
    reg = registration(evs)["payload"]
    assert "profile_resolution" not in reg and "profile_binding" not in reg
    assert len(warned) == 1
    message = str(warned[0].message)
    assert "with a problem" in message and "could not be loaded" in message and "profile.yaml" in message
    errors = governance_errors(capsys)
    assert len(errors) == 1 and errors[0]["payload"]["failure_reason"].startswith("profile resolution none:")


async def test_invalid_default_profile_resolves_to_no_profile_with_one_warning(isolated_home, config, capsys):
    # Parseable but not valid (core issue #19). Core 0.3.2.1 refuses it at
    # the default step; the companion surfaces the one warning and runs on.
    config.set_default("schema_version: 1\nhigh_consequence:\n  tools: [1, 2]\n")
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "SCOPE_ASSERTED")) == 1
    assert all("profile_fingerprint" not in e for e in evs)
    reg = registration(evs)["payload"]
    assert "profile_resolution" not in reg and "profile_binding" not in reg
    assert len(warned) == 1
    message = str(warned[0].message)
    assert "not runtime-ready" in message and "high_consequence.tools" in message
    errors = governance_errors(capsys)
    assert len(errors) == 1 and errors[0]["payload"]["failure_reason"].startswith("profile resolution none:")


async def test_invalid_bound_profile_degrades_to_the_default_with_one_warning(isolated_home, config, capsys):
    # A matched binding whose file parses but is invalid: degraded to the
    # machine default, exactly like a missing file; no later binding.
    bad = config.profile("bad.yaml", "schema_version: 1\nhigh_consequence:\n  tools: [1, 2]\n")
    d = config.set_default(PROFILE_D)
    config.bind(("pydantic-*", "profiles/bad.yaml"), ("deploy-*", "profiles/bad.yaml"))
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert fingerprints(evs) == {Config.fingerprint(d)}
    reg = registration(evs)["payload"]
    assert reg["profile_resolution"] == "degraded" and reg["profile_binding"] == "pydantic-*"
    assert len(warned) == 1 and "not runtime-ready" in str(warned[0].message) and bad.name in str(warned[0].message)
    errors = governance_errors(capsys)
    assert len(errors) == 1 and errors[0]["payload"]["failure_reason"].startswith("profile resolution degraded:")


async def test_a_profile_core_hands_back_that_fails_validation_is_not_bound(isolated_home, config, capsys, monkeypatch):
    # The companion's own readiness belt: if a resolver ever returned a
    # profile that core's validate() rejects, the session opens with no
    # profile, provenance stays truthful, and one warning says why.
    import pydantic_ai_governor.capability as cap
    from sentience_governor.profile.resolver import ResolvedProfile

    invalid = GovernanceProfile({"schema_version": 1, "high_consequence": {"tools": [1, 2]}})
    monkeypatch.setattr(
        cap, "resolve_profile",
        lambda **kwargs: ResolvedProfile(profile=invalid, source="bound", binding="pydantic-*", warnings=()),
    )
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "SCOPE_ASSERTED")) == 1
    assert all("profile_fingerprint" not in e for e in evs)
    reg = registration(evs)["payload"]
    assert "profile_loaded" not in reg
    assert reg["profile_resolution"] == "degraded" and reg["profile_binding"] == "pydantic-*"
    assert len(warned) == 1 and "is not valid" in str(warned[0].message) and "high_consequence.tools" in str(warned[0].message)
    errors = governance_errors(capsys)
    assert len(errors) == 1 and errors[0]["payload"]["failure_reason"].startswith("profile resolution degraded:")


async def test_malformed_resolution_file_warns_and_the_default_applies(isolated_home, config, capsys):
    d = config.set_default(PROFILE_D)
    config.resolution.write_text("bindings: [not: [valid", encoding="utf-8")
    result, warned = await run(gov())
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert fingerprints(evs) == {Config.fingerprint(d)}
    reg = registration(evs)["payload"]
    assert "profile_resolution" not in reg                     # default: not recorded
    assert len(warned) == 1
    assert len(governance_errors(capsys)) == 1


async def test_no_resolution_exception_escapes_into_the_run(isolated_home, config, monkeypatch):
    import pydantic_ai_governor.capability as cap

    def explode(**kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(cap, "resolve_profile", explode)
    result, warned = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    assert result.output == "done"
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "AGENT_REGISTERED")) == 1 and len(of_type(evs, "SCOPE_ASSERTED")) == 1
    assert len(warned) == 1 and "RuntimeError" in str(warned[0].message)


# ---------------------------------------------------------------------------
# Item 10: no companion persistence machinery
# ---------------------------------------------------------------------------

async def test_no_content_hash_sidecar_or_snapshot_directory(isolated_home, config):
    config.profile("A.yaml", PROFILE_A)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))
    result, _ = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])
    trace_root = isolated_home / ".sentience" / "traces" / "pydantic-ai"
    text = (trace_root / f"{result.run_id}.jsonl").read_text(encoding="utf-8")
    assert not HEX64.search(text)
    assert sorted(p.name for p in trace_root.iterdir()) == [f"{result.run_id}.jsonl"]
    assert not (trace_root / "profiles").exists()
    assert not list(trace_root.glob("*.index"))


# ---------------------------------------------------------------------------
# Item 11: existing compatibility
# ---------------------------------------------------------------------------

async def test_unknown_to_read_fallback_and_no_operation_classification(isolated_home, config):
    config.profile("A.yaml", PROFILE_A)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))
    result, _ = await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])  # no metadata: UNKNOWN
    evs = events(isolated_home, result.run_id)
    [asserted] = of_type(evs, "SCOPE_ASSERTED")
    assert asserted["payload"]["operation_type"] == "READ"
    assert asserted["payload"]["asserted_permissions"] == []
    assert not any("operation_classification" in json.dumps(e) for e in evs)


def test_resolution_happens_exactly_once_per_session_open(isolated_home, config, monkeypatch):
    import pydantic_ai_governor.capability as cap
    calls: List[str] = []
    real = cap.resolve_profile

    def counting(**kwargs):
        calls.append(kwargs["agent_id"])
        return real(**kwargs)

    monkeypatch.setattr(cap, "resolve_profile", counting)
    config.profile("A.yaml", PROFILE_A)
    config.bind(("pydantic-crm-*", "profiles/A.yaml"))

    async def go():
        return await run(gov(), model=one_call_model(), tools=[Tool(crm_fetch)])

    asyncio.run(go())
    assert calls == ["pydantic-crm-agent"]
