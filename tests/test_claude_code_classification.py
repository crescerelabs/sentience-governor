"""v0.3.2 CP5: classification becomes governance-visible.

* The Claude Code hook classifies every Bash call from
  ``tool_input.command`` (pure CP4 classifier), carries the object on
  SCOPE_ASSERTED, and derives the coarse ``target_system`` from it
  (``shell/<domain>`` iff complete and exactly one domain, else ``shell``).
  Non-Bash calls omit the object and keep their legacy targets;
  ``operation_type`` stays EXECUTE for Bash.
* The task-boundary namespace guard treats every Bash target as the one
  ``shell`` namespace, so a semantic subtype change never manufactures a
  ``dir_change`` or ``file_type_shift``; Bash → Edit still crosses.
* ``high_consequence.operations`` rules are evaluated per single
  ClassifiedEffect (never a combination of fields from separate effects)
  and raise the existing HIGH_CONSEQUENCE_DETECTED flag, deduplicated with
  the unchanged ``tools`` composite matcher.

The accepted 546-fixture corpus is the ``target_system`` oracle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder import builder as builder_module
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile import loader as loader_module
from sentience_governor.profile.schema import (
    DEMAND_AT_NEVER,
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
    default_profile_data,
)
from sentience_governor.schema.events import (
    AdvisoryFlag,
    ClassifiedEffect,
    ClassifiedSegment,
    DeploymentMode,
    EventType,
    OperationClassification,
    OperationType,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.wrapper import claude_code_hook as cch
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook
from sentience_governor.wrapper.shell_classification import classify_shell_command, target_system_for

CORPUS = Path(__file__).parent / "fixtures" / "shell_classification" / "cp4d_corpus.json"
FIXTURES: List[dict] = json.loads(CORPUS.read_text(encoding="utf-8"))["fixtures"]
HEX64 = re.compile(r"[0-9a-f]{64}")
SESSION = "sess-cp5-0001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Isolated profile environment: no resolution file, a default profile
    path under tmp (absent unless a test writes it), traces under tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", home / "resolution.yaml")
    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", home / "profile.yaml")
    monkeypatch.delenv("SENTIENCE_CLAUDE_CODE_AGENT_ID_PREFIX", raising=False)
    return home


def _write_profile(home: Path, *, operations=None, tools=None, demand_at="session_start") -> None:
    import yaml

    data = {"schema_version": 1, "session_intent": {"required": True, "demand_at": demand_at}}
    hc: Dict[str, Any] = {}
    if operations is not None:
        hc["operations"] = operations
    if tools is not None:
        hc["tools"] = tools
    if hc:
        data["high_consequence"] = hc
    (home / "profile.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _pre(tool: str, session: str = SESSION, use_id: str = "use-1", **tool_input) -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool,
            "tool_input": tool_input, "tool_use_id": use_id, "cwd": "/tmp"}


def _post(tool: str, session: str = SESSION, use_id: str = "use-1", **tool_input) -> dict:
    return {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": tool,
            "tool_input": tool_input, "tool_response": {"ok": True}, "tool_use_id": use_id, "cwd": "/tmp"}


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


def _events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    return [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines() if l.strip()]


def _scopes(sink: Path) -> List[dict]:
    return [e for e in _events(sink) if e["event_type"] == EventType.SCOPE_ASSERTED.value]


def _bash_scope(sink: Path, command: str, use_id: str = "use-1") -> dict:
    _run(_pre("Bash", use_id=use_id, command=command), sink)
    return _scopes(sink)[-1]


def _profile(**overrides) -> GovernanceProfile:
    data = default_profile_data()
    data["session_intent"]["demand_at"] = DEMAND_AT_NEVER
    hc = data["high_consequence"]
    if "operations" in overrides:
        hc["operations"] = overrides["operations"]
    if "tools" in overrides:
        hc["tools"] = overrides["tools"]
    if "signals" in overrides:
        data["task_boundary"]["signals"] = overrides["signals"]
    return GovernanceProfile(data)


def _builder(profile: Optional[GovernanceProfile] = None, session: str = "s"):
    sm = SessionManager()
    cache = InProcessCache()
    sm.session_start(session_id=session, agent_id="agent", profile=profile)
    cache.init_session(session)
    return EventBuilder(session_manager=sm, cache=cache, agent_id="agent", session_id=session,
                        deployment_mode=DeploymentMode.vendor_managed)


def _scope_for(builder: EventBuilder, command: str):
    oc = classify_shell_command(command)
    return builder.build_scope_asserted(
        tool_id="Bash", asserted_permissions=["execute"], target_system=target_system_for(oc),
        operation_type=OperationType.EXECUTE, operation_classification=oc,
    )


def _flagged(builder: EventBuilder, command: str) -> bool:
    return AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in _scope_for(builder, command).advisory_flags


# ---------------------------------------------------------------------------
# target_system: the accepted corpus is the oracle (row 34 and §9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_target_system_matches_accepted_corpus(fixture: dict):
    assert target_system_for(classify_shell_command(fixture["command"])) == fixture["target_system"]


class TestTargetSystemRule:
    @pytest.mark.parametrize("cmd,expected", [
        ("git status", "shell/version_control"),
        ("cat a > b", "shell/filesystem"),
        ("git status > f", "shell"),
        ("curl -o x https://example.com", "shell"),
        ("rm -rf /tmp && aws ec2 describe-instances", "shell"),
        ("git status && git log", "shell/version_control"),
        ("cd a && git status", "shell/version_control"),
        ("./deploy.sh", "shell"),
        ("git status && ./deploy.sh", "shell"),
        ("echo $(git status)", "shell"),
        ("git status > \"$(x)\"", "shell"),
        ("git status 2>&1", "shell/version_control"),
        ("ls x 2>/dev/null", "shell/filesystem"),
        ("cat a > /dev/null", "shell/filesystem"),
        ("cat a > /dev/stdout", "shell/filesystem"),
        ("curl -o /dev/null https://x", "shell/network"),
        ("cd /tmp", "shell"),
        ("", "shell"),
        ("echo x > f", "shell/filesystem"),
        ("aws ecr batch-delete-image --repository-name r", "shell"),
        ("sudo -u root make", "shell"),
    ])
    def test_rule(self, cmd, expected):
        assert target_system_for(classify_shell_command(cmd)) == expected

    def test_unknown_forces_shell_even_with_one_known_domain(self):
        oc = OperationClassification(classifier="shell_rules", classifier_version=1, complete=False, destructive=None,
                                     segments=[ClassifiedSegment(executable="git", subcommand="status", effects=[
                                         ClassifiedEffect(domain="version_control", action="read", destructive=False),
                                         ClassifiedEffect(domain="unknown", action="unknown", destructive=None)])])
        assert target_system_for(oc) == "shell"

    def test_never_raises(self):
        assert target_system_for(None) == "shell"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# hook wiring and runtime evidence (rows 32, 35 and §7 of the brief)
# ---------------------------------------------------------------------------


class TestHookEvidence:
    @pytest.mark.parametrize("cmd,domain,action,target", [
        ("git status", "version_control", "read", "shell/version_control"),
        ("mkdir -p out", "filesystem", "create", "shell/filesystem"),
        ("chmod +x run.sh", "filesystem", "modify", "shell/filesystem"),
        ("rm -rf build", "filesystem", "delete", "shell/filesystem"),
        ("python3 app.py", "process", "execute", "shell/process"),
        ("./deploy.sh", "unknown", "unknown", "shell"),
        ("aws ec2 terminate-instances --instance-ids i-1", "cloud_infrastructure", "delete", "shell/cloud_infrastructure"),
    ])
    def test_bash_scope_carries_classification_and_target(self, env, tmp_path, cmd, domain, action, target):
        sink = tmp_path / "t.jsonl"
        scope = _bash_scope(sink, cmd)
        p = scope["payload"]
        assert p["tool_id"] == "Bash" and p["operation_type"] == "EXECUTE"
        assert p["target_system"] == target
        oc = p["operation_classification"]
        assert oc["classifier"] == "shell_rules" and oc["classifier_version"] == 1
        assert oc["segments"][0]["effects"][0]["domain"] == domain
        assert oc["segments"][0]["effects"][0]["action"] == action
        assert oc["complete"] == (domain != "unknown")

    def test_multi_effect_event_serializes_every_effect(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        p = _bash_scope(sink, "curl -o page.html https://example.com")["payload"]
        assert p["target_system"] == "shell"
        assert p["operation_classification"]["segments"] == [{"executable": "curl", "effects": [
            {"domain": "network", "action": "read", "destructive": False},
            {"domain": "filesystem", "action": "modify", "destructive": None}]}]
        assert p["operation_classification"]["destructive"] is None
        p = _bash_scope(sink, "rm -rf /tmp/x && aws ec2 describe-instances", use_id="use-2")["payload"]
        segs = p["operation_classification"]["segments"]
        assert [s["executable"] for s in segs] == ["rm", "aws"]
        assert segs[1]["subcommand"] == "ec2 describe-instances"
        assert p["operation_classification"]["destructive"] is True

    def test_neutral_and_neutral_with_redirection(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        p = _bash_scope(sink, "cd /tmp && export A=1")["payload"]
        assert p["operation_classification"] == {"classifier": "shell_rules", "classifier_version": 1,
                                                 "complete": True, "destructive": False, "segments": []}
        assert p["target_system"] == "shell"
        p = _bash_scope(sink, "echo x > f", use_id="use-2")["payload"]
        assert p["operation_classification"]["segments"] == [{"executable": "echo", "effects": [
            {"domain": "filesystem", "action": "modify", "destructive": None}]}]
        assert p["target_system"] == "shell/filesystem"

    def test_unsupported_nesting_and_dev_null(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        p = _bash_scope(sink, "echo $(aws ec2 terminate-instances --instance-ids i-1)")["payload"]
        assert p["operation_classification"]["complete"] is False
        assert p["operation_classification"]["segments"][0]["effects"] == [{"domain": "unknown", "action": "unknown", "destructive": None}]
        assert p["target_system"] == "shell"
        p = _bash_scope(sink, "ls tests/ 2>/dev/null", use_id="use-2")["payload"]
        assert p["operation_classification"]["segments"][0]["effects"] == [{"domain": "filesystem", "action": "read", "destructive": False}]
        assert p["target_system"] == "shell/filesystem"

    def test_missing_or_non_string_command_is_explicit_unknown(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        _run(_pre("Bash"), sink)  # no command key
        p = _scopes(sink)[-1]["payload"]
        assert p["operation_classification"]["complete"] is False
        assert p["operation_classification"]["segments"][0]["effects"][0]["domain"] == "unknown"
        assert p["target_system"] == "shell"
        _run({**_pre("Bash", use_id="use-2"), "tool_input": {"command": ["ls"]}}, sink)
        p = _scopes(sink)[-1]["payload"]
        assert p["operation_classification"]["complete"] is False

    def test_non_bash_omits_object_and_keeps_legacy_target(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        for tool, kwargs, target, op in [
            ("Edit", {"file_path": "/x/a.py"}, "filesystem", "WRITE"),
            ("Read", {"file_path": "/x/a.py"}, "filesystem", "READ"),
            ("WebFetch", {"url": "https://x"}, "web", "READ"),
            ("mcp__srv__list_things", {}, "srv", "READ"),
        ]:
            _run(_pre(tool, use_id=f"u-{tool}", **kwargs), sink)
            p = _scopes(sink)[-1]["payload"]
            assert "operation_classification" not in p, tool
            assert p["target_system"] == target and p["operation_type"] == op, tool

    def test_pre_post_provenance_agree_and_chain_intact(self, env, tmp_path):
        sink = tmp_path / "t.jsonl"
        _run(_pre("Bash", command="git status"), sink)
        _run(_post("Bash", command="git status"), sink)
        _run(_pre("Bash", use_id="use-2", command="./deploy.sh"), sink)
        _run(_post("Bash", use_id="use-2", command="./deploy.sh"), sink)
        events = _events(sink)
        types = [e["event_type"] for e in events]
        assert types.count(EventType.AGENT_REGISTERED.value) == 1
        assert [e["event_sequence_number"] for e in events] == list(range(1, len(events) + 1))
        for prev, cur in zip(events, events[1:]):
            assert cur["previous_event_id"] == prev["event_id"]
        snaps = [e for e in events if e["event_type"] == EventType.CONTEXT_SNAPSHOT.value]
        assert [s["payload"]["provenance"] for s in snaps] == [["shell/version_control"], ["shell/version_control"], ["shell"], ["shell"]]
        assert not HEX64.search(sink.read_text(encoding="utf-8"))
        assert not any(e["event_type"] == EventType.MEMORY_WRITE_ATTEMPT.value for e in events)

    def test_flags_and_consequence_identical_across_classified_commands(self, env, tmp_path):
        """Row 32: classification changes no flag, violation or consequence
        on its own (no profile here: the legacy evaluation is untouched)."""
        sink = tmp_path / "t.jsonl"
        a = _bash_scope(sink, "ls -la", use_id="u1")
        b = _bash_scope(sink, "./deploy.sh", use_id="u2")
        c = _bash_scope(sink, "rm -rf /tmp/x && aws ec2 describe-instances", use_id="u3")
        for e in (b, c):
            assert e["advisory_flags"] == a["advisory_flags"]
            assert e["policy_violations"] == a["policy_violations"]
            assert e["simulated_consequence"] == a["simulated_consequence"]
            assert e["pass_through"] is True

    def test_classifier_failure_is_fail_open(self, env, tmp_path, monkeypatch):
        def boom(_cmd):
            raise RuntimeError("defect")
        monkeypatch.setattr(cch, "classify_shell_command", boom)
        sink = tmp_path / "t.jsonl"
        scope = _bash_scope(sink, "git status")
        assert scope["payload"]["target_system"] == "shell"
        assert "operation_classification" not in scope["payload"]
        assert scope["payload"]["operation_type"] == "EXECUTE"

    def test_operations_rule_flags_through_the_hook(self, env, tmp_path):
        """Profile with an operations rule bound as the machine default: the
        hook's Bash event carries the flag; a non-matching command and a
        non-Bash event do not."""
        _write_profile(env, operations=[{"domain": "cloud_infrastructure", "destructive": True}])
        sink = tmp_path / "t.jsonl"
        hit = _bash_scope(sink, "aws ec2 terminate-instances --instance-ids i-1", use_id="u1")
        miss = _bash_scope(sink, "rm -rf /tmp/x && aws ec2 describe-instances", use_id="u2")
        read = _bash_scope(sink, "aws ec2 describe-instances", use_id="u3")
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED.value in hit["advisory_flags"]
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED.value not in miss["advisory_flags"]
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED.value not in read["advisory_flags"]
        _run(_pre("Edit", use_id="u4", file_path="/x"), sink)
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED.value not in _scopes(sink)[-1]["advisory_flags"]


# ---------------------------------------------------------------------------
# namespace guard (row 31)
# ---------------------------------------------------------------------------


class TestNamespaceGuard:
    def test_shell_subtypes_never_change_dir_or_ext(self):
        for target in ("shell", "shell/version_control", "shell/filesystem", "shell/cloud_infrastructure"):
            assert builder_module._extract_dir(target, 2) == "shell"
            assert builder_module._extract_dir(target, 1) == "shell"
            assert builder_module._extract_file_ext(target) is None
        assert builder_module._extract_dir("filesystem", 2) == "filesystem"
        assert builder_module._extract_dir("src/foo/bar.py", 2) == "src/foo"
        assert builder_module._extract_file_ext("src/foo/bar.py") == "py"
        assert builder_module._extract_dir("shellfish/x", 1) == "shellfish"

    def test_dir_change_does_not_fire_across_shell_subtypes(self):
        b = _builder(_profile(signals=[SIGNAL_DIR_CHANGE]))
        e1 = _scope_for(b, "git status")
        e2 = _scope_for(b, "ls -la")
        e3 = _scope_for(b, "aws ec2 describe-instances")
        e4 = _scope_for(b, "./deploy.sh")
        for e in (e1, e2, e3, e4):
            assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e.advisory_flags

    def test_file_type_shift_never_from_a_domain_token(self):
        b = _builder(_profile(signals=[SIGNAL_FILE_TYPE_SHIFT]))
        _scope_for(b, "git status")
        e2 = _scope_for(b, "cat a > b")
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e2.advisory_flags
        # A genuine extension shift on a file target still fires as today.
        b.build_scope_asserted(tool_id="Edit", asserted_permissions=["write"], target_system="src/a.py", operation_type=OperationType.WRITE)
        e4 = b.build_scope_asserted(tool_id="Edit", asserted_permissions=["write"], target_system="docs/readme.md", operation_type=OperationType.WRITE)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in e4.advisory_flags

    def test_bash_to_edit_still_crosses_dir_change(self):
        b = _builder(_profile(signals=[SIGNAL_DIR_CHANGE]))
        _scope_for(b, "git status")
        e2 = b.build_scope_asserted(tool_id="Edit", asserted_permissions=["write"], target_system="filesystem", operation_type=OperationType.WRITE)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in e2.advisory_flags


# ---------------------------------------------------------------------------
# operations evaluator (rows 24-28, 43-44 at builder level)
# ---------------------------------------------------------------------------


class TestOperationsEvaluator:
    def test_scalar_predicates(self):
        b = _builder(_profile(operations=[{"domain": "filesystem", "action": "delete"}]))
        assert _flagged(b, "rm -rf /tmp/x")
        assert not _flagged(b, "ls -la")
        assert not _flagged(b, "git rm x")  # version_control/delete, wrong domain

    def test_list_valued_domain_and_action(self):
        b = _builder(_profile(operations=[{"domain": ["cloud_infrastructure", "version_control"], "action": ["delete", "modify"]}]))
        assert _flagged(b, "git push --force")
        assert _flagged(b, "kubectl delete pod x")
        assert not _flagged(b, "git status")
        assert not _flagged(b, "rm -rf x")

    def test_destructive_true_false_and_null(self):
        b_true = _builder(_profile(operations=[{"destructive": True}]))
        b_false = _builder(_profile(operations=[{"destructive": False}]))
        assert _flagged(b_true, "git reset --hard") and not _flagged(b_false, "git reset --hard")
        assert _flagged(b_false, "git status") and not _flagged(b_true, "git status")
        # null satisfies neither (row 27)
        for cmd in ("cp a b", "terraform apply", "terraform apply && cp a b", "./deploy.sh"):
            assert not _flagged(b_true, cmd), cmd
            assert not _flagged(b_false, cmd), cmd

    def test_unknown_domain_rule(self):
        b = _builder(_profile(operations=[{"domain": "unknown"}]))
        assert _flagged(b, "./deploy.sh")
        assert _flagged(b, "IID=$(aws ec2 run-instances --image-id x)")
        assert _flagged(b, "bash -c 'rm -rf /'")
        assert not _flagged(b, "git status")

    def test_empty_rule_matches_any_effect_but_not_a_neutral_command(self):
        b = _builder(_profile(operations=[{}]))
        assert _flagged(b, "git status")
        assert not _flagged(b, "cd /tmp")  # no effects to satisfy

    @pytest.mark.parametrize("bad", [
        "not-a-mapping", {"domain": "cloud"}, {"action": "write"}, {"destructive": "yes"}, {"domain": []},
        {"domain": "filesystem", "extra": 1}, {"domain": ["filesystem", 3]}, None, 42,
    ])
    def test_malformed_rules_are_skipped(self, bad):
        b = _builder(_profile(operations=[bad, {"domain": "filesystem", "action": "delete"}]))
        assert not _flagged(b, "git status")
        assert _flagged(b, "rm -rf x")  # the valid rule still works

    def test_rules_ignored_without_classification(self):
        b = _builder(_profile(operations=[{}]))
        e = b.build_scope_asserted(tool_id="Edit", asserted_permissions=["write"], target_system="filesystem", operation_type=OperationType.WRITE)
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in e.advisory_flags

    def test_no_profile_no_evaluation(self):
        b = _builder(None)
        assert not _flagged(b, "rm -rf /")

    def test_on_match_stays_flag_and_no_new_identifiers(self):
        b = _builder(_profile(operations=[{"domain": "filesystem", "action": "delete"}]))
        e = _scope_for(b, "rm -rf x")
        assert e.advisory_flags.count(AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED) == 1
        assert e.pass_through is True
        assert set(e.policy_violations) <= {"POL-001"}
        assert all(f in {a.value for a in AdvisoryFlag} for f in e.advisory_flags)


class TestNoSyntheticMatches:
    def test_curl_o(self):
        for rule in ({"domain": "network", "action": "modify"}, {"domain": "network", "action": "create"},
                     {"domain": "filesystem", "action": "create"}, {"domain": "network", "destructive": True}):
            assert not _flagged(_builder(_profile(operations=[rule])), "curl -o x https://example.com"), rule
        for rule in ({"domain": "network", "action": "read"}, {"domain": "filesystem", "action": "modify"}):
            assert _flagged(_builder(_profile(operations=[rule])), "curl -o x https://example.com"), rule

    def test_rm_and_aws_describe(self):
        cmd = "rm -rf /tmp/x && aws ec2 describe-instances"
        for rule in ({"domain": "cloud_infrastructure", "destructive": True}, {"domain": "cloud_infrastructure", "action": "delete"},
                     {"domain": "filesystem", "action": "read"}):
            assert not _flagged(_builder(_profile(operations=[rule])), cmd), rule
        for rule in ({"domain": "filesystem", "action": "delete"}, {"destructive": True}, {"domain": "cloud_infrastructure", "action": "read"}):
            assert _flagged(_builder(_profile(operations=[rule])), cmd), rule

    def test_git_push_force(self):
        assert not _flagged(_builder(_profile(operations=[{"domain": "network", "destructive": True}])), "git push --force")
        assert _flagged(_builder(_profile(operations=[{"domain": "version_control", "destructive": True}])), "git push --force")

    def test_git_clone(self):
        assert not _flagged(_builder(_profile(operations=[{"domain": "version_control", "action": "create", "destructive": True}])), "git clone https://x/y.git")
        assert _flagged(_builder(_profile(operations=[{"domain": "version_control", "action": "create"}])), "git clone https://x/y.git")

    def test_fabricated_object_row28(self):
        oc = OperationClassification(classifier="shell_rules", classifier_version=1, complete=True, destructive=True, segments=[
            ClassifiedSegment(executable="x", effects=[ClassifiedEffect(domain="filesystem", action="delete", destructive=True),
                                                      ClassifiedEffect(domain="network", action="read", destructive=False)])])
        m = builder_module._operation_rule_matches
        assert not m({"domain": "filesystem", "action": "read"}, oc)
        assert not m({"domain": "network", "destructive": True}, oc)
        assert not m({"domain": "network", "action": "delete"}, oc)
        assert m({"domain": "filesystem", "action": "delete", "destructive": True}, oc)


class TestToolsAndOperationsCoexist:
    def test_both_surfaces_one_flag(self):
        b = _builder(_profile(tools=["Bash:shell/cloud_infrastructure"], operations=[{"domain": "cloud_infrastructure"}]))
        e = _scope_for(b, "aws ec2 describe-instances")
        assert e.advisory_flags.count(AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED) == 1

    def test_either_surface_alone(self):
        b_tools = _builder(_profile(tools=["Bash:shell$"]))
        assert _flagged(b_tools, "./deploy.sh")       # plain shell target
        assert not _flagged(b_tools, "git status")   # shell/version_control does not end-anchor match
        b_ops = _builder(_profile(operations=[{"domain": "version_control"}]))
        assert _flagged(b_ops, "git status")
        assert not _flagged(b_ops, "./deploy.sh")

    def test_tools_composite_is_still_two_part(self):
        b = _builder(_profile(tools=["^Bash:shell/version_control$"]))
        assert _flagged(b, "git status")
        assert not _flagged(b, "git status > f")  # target is `shell`
        b2 = _builder(_profile(tools=["Bash:shell"]))
        assert _flagged(b2, "git status") and _flagged(b2, "./deploy.sh")
