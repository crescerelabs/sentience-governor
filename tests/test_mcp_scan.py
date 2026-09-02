"""v0.3.1.1 §5 — the retrospective review over MCP (`sentience_scan`).

One capability with progressive disclosure over the SAME `retro.scan` result
the CLI renders. These tests pin three things the design depends on: the
payload shape a client consumes, the user-facing vocabulary that keeps
Reader's internal finding classes out of the conversation, and the semantic
parity contract (§7) — neither surface may discover a finding the other
lacks.

The payload builder is pure of any `mcp` dependency, so it is tested
directly; tool registration is tested behind `importorskip("mcp")`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentience_governor import retro
from sentience_governor.analyze.renderers import render_scan_detail
from sentience_governor.mcp_server.server import scan_payload

# ---------------------------------------------------------------------------
# Corpus construction — the same shapes `test_retro_scan.py` builds, kept
# local so this file states its own fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Isolated $HOME and $CLAUDE_CONFIG_DIR for every test."""
    home = tmp_path / "home"
    config = home / ".claude"
    (config / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture
def config_root(isolated_env):
    return isolated_env


@pytest.fixture
def home():
    return Path(os.environ["HOME"])


def git_repo(home: Path, name: str) -> Path:
    repo = home / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    return repo


def write_block(path, tool: str = "Write") -> dict:
    return {"name": tool, "input": {"file_path": str(path), "content": "c"}}


def shell_block(command: str = "ls") -> dict:
    return {"name": "Bash", "input": {"command": command}}


def activity(session_id, *, tools=(), cwd="/home/user/proj-a",
             timestamp="2026-08-24T12:00:00.000Z") -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "message": {"role": "assistant",
                    "content": [dict(t, type="tool_use") for t in tools]},
    }


def install(config_root: Path, records) -> None:
    d = config_root / "projects" / "-home-user-proj-a"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")


def cross_corpus(config_root, home, *, dests=("repo-b",), ops=1):
    """One session working in repo-a that wrote into other repositories."""
    source = git_repo(home, "repo-a")
    records = []
    for name in dests:
        dest = git_repo(home, name)
        records.append(activity(
            "s", cwd=str(source),
            tools=[write_block(dest / ("f%d.txt" % i)) for i in range(ops)]))
    install(config_root, records)
    return config_root


def mixed_corpus(config_root, home):
    """Three distinct populations, which the real corpus also exhibits:

    5 sessions reviewed · 2 carrying findings · 1 standout. The second
    carrier holds only `non_project` findings, which `select_standout_
    sessions` deliberately does not promote when something stronger exists.
    """
    source = git_repo(home, "repo-a")
    other = git_repo(home, "repo-b")
    elsewhere = home / "scratch"
    elsewhere.mkdir(parents=True, exist_ok=True)
    records = [
        activity("s-standout", cwd=str(source), tools=[
            write_block(other / "docs" / "guide.md"),
            write_block(other / "docs" / "guide.md"),
            write_block(other / "src" / "main.py"),
        ]),
        activity("s-carrier", cwd=str(source), tools=[
            write_block(elsewhere / "notes.md"),
            write_block(elsewhere / "notes.md"),
        ]),
    ]
    # Reviewed, tool-bearing, but producing nothing Reader can classify.
    for i in range(3):
        records.append(activity("s-quiet-%d" % i, cwd=str(source),
                                tools=[shell_block("echo %d" % i)]))
    install(config_root, records)
    return config_root


def all_paths(obj):
    """Every `path` value appearing anywhere in a nested structure."""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "path" and isinstance(value, str):
                found.append(value)
            found.extend(all_paths(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(all_paths(item))
    return found


def all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= all_keys(item)
    return keys


def flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------


class TestScanSummary:
    """§5.3 — step A returns a summary a client can present, not the corpus."""

    def test_returns_the_summary_structure_not_raw_findings(
        self, config_root, home
    ):
        payload = scan_payload(root=cross_corpus(config_root, home, ops=3))
        assert payload["status"] == "ok"
        assert payload["reviewed"]["read_only"] is True
        assert payload["reviewed"]["sessions"] == 1
        # The shipped `--json` contract's raw arrays are NOT the MCP payload.
        for absent in ("findings", "by_session", "session_labels",
                       "findings_omitted", "targets_omitted"):
            assert absent not in all_keys(payload)
        [session] = payload["standout"]
        assert session["session"]
        assert session["working_in"] == "~/repo-a"
        [group] = session["groups"]
        assert group["destination"] == "~/repo-b"
        assert group["write_operations"] == 3

    def test_summary_groups_carry_no_targets(self, config_root, home):
        payload = scan_payload(root=cross_corpus(config_root, home, ops=3))
        assert "targets" not in payload["standout"][0]["groups"][0]

    def test_period_is_the_calendar_date(self, config_root, home):
        payload = scan_payload(root=cross_corpus(config_root, home))
        assert payload["reviewed"]["period_start"] == "2026-08-24"
        assert payload["reviewed"]["period_end"] == "2026-08-24"

    def test_carries_the_reviewer_limitation_in_both_modes(
        self, config_root, home
    ):
        root = cross_corpus(config_root, home)
        for detail in (False, True):
            limits = scan_payload(detail=detail, root=root)["limits"]
            assert "retrospective reviewer, not live governance" in limits
            assert "cannot establish what you intended or authorized" in limits

    def test_not_evaluated_reports_what_reader_did_not_look_at(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        not_evaluated = payload["not_evaluated"]
        assert not_evaluated["shell_commands"] == 3
        assert not_evaluated["prompt_content"] == "never inspected"
        assert not_evaluated["oversize_records_skipped"] == 0


class TestDetailAvailable:
    """§5.3 — how a client learns a continuation exists without the human
    having to know one does."""

    def test_true_when_findings_exist(self, config_root, home):
        payload = scan_payload(root=cross_corpus(config_root, home))
        assert payload["detail_available"] is True

    def test_false_when_nothing_was_found(self, config_root, home):
        source = git_repo(home, "repo-a")
        install(config_root, [activity("s", cwd=str(source),
                                       tools=[shell_block()])])
        payload = scan_payload(root=config_root)
        assert payload["standout"] == []
        assert payload["detail_available"] is False

    def test_absent_from_detail_which_is_the_continuation(
        self, config_root, home
    ):
        payload = scan_payload(detail=True, root=cross_corpus(config_root, home))
        assert "detail_available" not in payload


class TestScanDetail:
    """§5.4 — step B returns the evidence, grouped by session."""

    def test_returns_targets_other_reviewed_and_accounting(
        self, config_root, home
    ):
        payload = scan_payload(detail=True,
                               root=mixed_corpus(config_root, home))
        [standout] = payload["standout"]
        assert standout["session"]
        targets = standout["groups"][0]["targets"]
        assert {t["path"] for t in targets} == {"docs/guide.md", "src/main.py"}

        [other] = payload["other_reviewed"]
        assert other["note"] == "Did not meet Reader's standout criteria."
        assert other["groups"][0]["kind"] == "no_identifiable_project"

        without = payload["sessions_without_findings"]
        assert without["count"] == 3
        assert "not a statement that they were clean" in without["note"].lower()

    def test_cross_project_targets_are_relative_to_the_destination(
        self, config_root, home
    ):
        payload = scan_payload(detail=True,
                               root=mixed_corpus(config_root, home))
        group = payload["standout"][0]["groups"][0]
        assert group["destination"] == "~/repo-b"
        for target in group["targets"]:
            assert not target["path"].startswith("~")
            assert not target["path"].startswith("/")

    def test_unidentifiable_targets_are_absolute_and_home_abbreviated(
        self, config_root, home
    ):
        payload = scan_payload(detail=True,
                               root=mixed_corpus(config_root, home))
        group = payload["other_reviewed"][0]["groups"][0]
        assert [t["path"] for t in group["targets"]] == ["~/scratch/notes.md"]

    def test_operation_counts_survive_into_the_targets(
        self, config_root, home
    ):
        payload = scan_payload(detail=True,
                               root=mixed_corpus(config_root, home))
        targets = {t["path"]: t["write_operations"]
                   for t in payload["standout"][0]["groups"][0]["targets"]}
        assert targets == {"docs/guide.md": 2, "src/main.py": 1}

    def test_bounded_is_always_present_in_detail(self, config_root, home):
        payload = scan_payload(detail=True, root=cross_corpus(config_root, home))
        # Present at zero as well, so a client can disclose truncation
        # without inferring it from silence (§4.6).
        assert payload["bounded"] == {"findings_omitted": 0,
                                      "targets_omitted": 0}

    def test_summary_does_not_carry_bounded(self, config_root, home):
        payload = scan_payload(root=cross_corpus(config_root, home))
        assert "bounded" not in payload


class TestKindVocabulary:
    """§5.4 — user-facing vocabulary; Reader's internal classes stay internal."""

    def test_kinds_are_user_facing(self, config_root, home):
        payload = scan_payload(detail=True,
                               root=mixed_corpus(config_root, home))
        kinds = {g["kind"] for block in payload["standout"] + payload["other_reviewed"]
                 for g in block["groups"]}
        assert kinds == {"another_project", "no_identifiable_project"}

    def test_internal_class_names_never_appear(self, config_root, home):
        for detail in (False, True):
            serialized = json.dumps(scan_payload(
                detail=detail, root=mixed_corpus(config_root, home)))
            for internal in ("cross_project", "non_project", "claude_config",
                             "dest_root", "source_root", "op_count",
                             "finding_class"):
                assert internal not in serialized

    def test_global_configuration_kind_for_config_findings(
        self, config_root, home
    ):
        source = git_repo(home, "repo-a")
        install(config_root, [activity("s", cwd=str(source), tools=[
            write_block(home / ".claude" / "settings.json")])])
        payload = scan_payload(detail=True, root=config_root)
        [group] = payload["standout"][0]["groups"]
        assert group["kind"] == "global_configuration"

    def test_config_findings_carry_no_trace_of_their_internal_class(
        self, config_root, home
    ):
        """The mapping and its guard, over a corpus that actually holds a
        `claude_config` finding.

        `test_internal_class_names_never_appear` sweeps a corpus with no
        config finding in it, so it proves absence where the string was
        never at risk. This is the case where the translation has to
        happen: in both modes, `global_configuration` appears and
        `claude_config` does not.
        """
        source = git_repo(home, "repo-a")
        install(config_root, [activity("s", cwd=str(source), tools=[
            write_block(home / ".claude" / "settings.json"),
            write_block(home / ".claude" / "hooks" / "post.sh")])])
        for detail in (False, True):
            payload = scan_payload(detail=detail, root=config_root)
            kinds = [g["kind"] for g in payload["standout"][0]["groups"]]
            assert kinds == ["global_configuration"]
            assert "claude_config" not in json.dumps(payload)

    def test_config_group_is_rendered_first_within_its_session(
        self, config_root, home
    ):
        source = git_repo(home, "repo-a")
        other = git_repo(home, "repo-b")
        install(config_root, [activity("s", cwd=str(source), tools=[
            write_block(other / "f.txt"),
            write_block(home / ".claude" / "settings.json")])])
        payload = scan_payload(detail=True, root=config_root)
        kinds = [g["kind"] for g in payload["standout"][0]["groups"]]
        assert kinds[0] == "global_configuration"


class TestNoAggregateCounts:
    """§4.5 / §2.5 — the payload refuses counts Reader does not hold."""

    def test_no_destination_project_count(self, config_root, home):
        payload = scan_payload(
            root=cross_corpus(config_root, home, dests=("repo-b", "repo-c")))
        groups = payload["standout"][0]["groups"]
        assert len(groups) == 2
        # Each destination stands on its own; nothing sums them into a
        # "wrote into N projects" claim, which `dest_root` would make
        # calculable but which Reader does not assert.
        for group in groups:
            assert set(group) == {"kind", "destination", "write_operations"}
        assert "projects" not in json.dumps(payload)

    def test_unidentifiable_group_is_volume_only(self, config_root, home):
        payload = scan_payload(root=mixed_corpus(config_root, home),
                               detail=True)
        group = payload["other_reviewed"][0]["groups"][0]
        assert set(group) == {"kind", "write_operations", "targets"}
        for banned in ("locations", "distinct", "trees", "destinations"):
            assert banned not in set(group)

    def test_summary_offers_representative_locations_not_a_count(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        # The carrier is not standout, so reach it through a corpus where
        # unidentifiable activity is the only finding class.
        source = git_repo(home, "repo-a")
        scratch = home / "scratch"
        scratch.mkdir(exist_ok=True)
        install(config_root, [activity("s", cwd=str(source), tools=[
            write_block(scratch / "a.md"), write_block(scratch / "b.md")])])
        payload = scan_payload(root=config_root)
        [group] = payload["standout"][0]["groups"]
        assert group["kind"] == "no_identifiable_project"
        assert group["write_operations"] == 2
        assert isinstance(group["representative"], list)
        assert "count" not in group


class TestFieldNameCollisionGuard:
    """§5.3 — `sessions_with_findings` holds STANDOUT ids in the shipped
    `--json` contract. Reusing that name here with the correct meaning would
    put two definitions of one field into the product at once."""

    def test_no_field_named_sessions_with_findings_at_any_depth(
        self, config_root, home
    ):
        for detail in (False, True):
            payload = scan_payload(detail=detail,
                                   root=mixed_corpus(config_root, home))
            assert "sessions_with_findings" not in all_keys(payload)

    def test_accounting_field_is_sessions_carrying_findings(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        assert payload["sessions_carrying_findings"] == 2

    def test_three_populations_stay_distinct(self, config_root, home):
        """Reviewed ≠ carrying findings ≠ standout, the property that made
        the shipped field name wrong in the first place."""
        root = mixed_corpus(config_root, home)
        summary = scan_payload(root=root)
        detail = scan_payload(detail=True, root=root)
        assert summary["reviewed"]["sessions"] == 5
        assert summary["sessions_carrying_findings"] == 2
        assert len(summary["standout"]) == 1
        # Detail accounts for the same three populations without the field.
        assert len(detail["standout"]) == 1
        assert len(detail["other_reviewed"]) == 1
        assert detail["sessions_without_findings"]["count"] == 3


class TestSinceValidation:
    """§5.2.1 — grounded in `retro._SINCE_CHOICES` and the shipped
    `declare_intent` error precedent, not a new error framework."""

    def test_accepted_values_are_exactly_the_cli_windows(self):
        assert retro._SINCE_CHOICES == ("7d", "30d", "all")

    @pytest.mark.parametrize("since", ["7d", "30d", "all"])
    def test_each_accepted_value_produces_a_scan(self, config_root, home, since):
        payload = scan_payload(
            since=since, root=cross_corpus(config_root, home))
        assert payload["status"] == "ok"

    @pytest.mark.parametrize("since", ["1d", "90d", "", "ALL", "7", None, 7])
    def test_invalid_value_returns_the_shipped_error_shape(self, since):
        payload = scan_payload(since=since)
        assert payload == {
            "status": "invalid_request",
            "detail": "since must be one of 7d, 30d, all",
        }

    def test_invalid_value_never_raises_to_the_client(self):
        # `_parse_since` raises on a malformed window; the tool must not.
        with pytest.raises(ValueError):
            retro._parse_since("nonsense")
        assert scan_payload(since="nonsense")["status"] == "invalid_request"

    def test_validation_is_stricter_than_the_parser_it_guards(self):
        """`_parse_since` accepts any `<digits>d`, so it is NOT the thing
        that restricts the window to three values — argparse `choices` does
        that for the CLI. The MCP tool has no argparse, so this check is what
        keeps the two surfaces on the same window vocabulary."""
        assert retro._parse_since("1d") is not None
        assert retro._parse_since("90d") is not None
        for accepted_by_parser in ("1d", "90d"):
            assert scan_payload(since=accepted_by_parser)["status"] == (
                "invalid_request")

    def test_invalid_value_performs_no_scan_at_all(self, monkeypatch):
        """Stronger than "nothing was written": nothing is read either,
        because the check precedes the call."""
        def explode(*args, **kwargs):
            raise AssertionError("retro.scan must not run on invalid input")

        monkeypatch.setattr(retro, "scan", explode)
        assert scan_payload(since="nonsense")["status"] == "invalid_request"


class TestZeroWrite:
    """§5.2 — read-only in every mode: no trace, no GovernanceEvent, no file."""

    def _tree(self, root: Path):
        return {p: p.stat().st_mtime_ns
                for p in sorted(root.rglob("*")) if p.is_file()}

    @pytest.mark.parametrize("since", ["all", "7d", "bogus"])
    @pytest.mark.parametrize("detail", [False, True])
    def test_nothing_is_created_or_modified(self, tmp_path, config_root, home,
                                            detail, since):
        cross_corpus(config_root, home)
        before = self._tree(tmp_path)
        scan_payload(detail=detail, since=since, root=config_root)
        assert self._tree(tmp_path) == before


class TestSemanticParity:
    """§7 — one Reader result, two access surfaces. Whatever differs between
    them is presentation; neither may find what the other cannot."""

    def _both(self, config_root, home):
        root = mixed_corpus(config_root, home)
        return (retro.scan(root), scan_payload(detail=True, root=root))

    def test_scan_period_and_session_counts_agree(self, config_root, home):
        result, payload = self._both(config_root, home)
        rendered = render_scan_detail(result)
        assert payload["reviewed"]["sessions"] == result["sessions"]
        assert "%d Claude Code sessions reviewed" % result["sessions"] in rendered
        assert payload["reviewed"]["period_start"] == result["period_start"][:10]
        assert payload["reviewed"]["period_end"] == result["period_end"][:10]

    def test_standout_selection_and_labels_agree(self, config_root, home):
        result, payload = self._both(config_root, home)
        rendered = render_scan_detail(result)
        assert len(payload["standout"]) == len(result["sessions_with_findings"])
        for block in payload["standout"]:
            assert block["session"] in rendered
        for block in payload["other_reviewed"]:
            assert block["session"] in rendered

    def test_session_ordering_agrees(self, config_root, home):
        result, payload = self._both(config_root, home)
        rendered = render_scan_detail(result)
        order = [block["session"] for block in
                 payload["standout"] + payload["other_reviewed"]]
        positions = [rendered.index(name) for name in order]
        assert positions == sorted(positions)

    def test_destinations_and_operation_counts_agree(self, config_root, home):
        result, payload = self._both(config_root, home)
        rendered = flat(render_scan_detail(result))
        for block in payload["standout"] + payload["other_reviewed"]:
            for group in block["groups"]:
                if group["kind"] == "another_project":
                    assert "%s — %d write operation" % (
                        group["destination"], group["write_operations"]
                    ) in rendered
                else:
                    assert "%d write operations targeting locations" % (
                        group["write_operations"]) in rendered

    def test_neither_surface_holds_a_target_the_other_lacks(
        self, config_root, home
    ):
        result, payload = self._both(config_root, home)
        rendered = render_scan_detail(result)
        mcp_paths = all_paths(payload)
        assert mcp_paths
        for path in mcp_paths:
            assert path in rendered
        # And the CLI shows no target beyond them: one row per finding.
        assert len(mcp_paths) == len(result["findings"])

    def test_omission_state_agrees(self, config_root, home):
        result, payload = self._both(config_root, home)
        assert payload["bounded"]["findings_omitted"] == result["findings_omitted"]
        assert payload["bounded"]["targets_omitted"] == result["targets_omitted"]

    @pytest.mark.parametrize("since", ["7d", "30d", "all"])
    def test_window_semantics_agree(self, config_root, home, since):
        """`sentience_scan(since=X)` and `sentience scan --since X` review the
        same sessions and produce the same findings."""
        root = mixed_corpus(config_root, home)
        now = retro.datetime.fromisoformat("2026-08-25T12:00:00+00:00")
        result = retro.scan(root, since=since, now=now)
        payload = scan_payload(detail=True, since=since, root=root, now=now)
        assert payload["reviewed"]["sessions"] == result["sessions"]
        assert len(all_paths(payload)) == len(result["findings"])
        assert (len(payload["standout"])
                == len(result["sessions_with_findings"]))

    def test_neither_surface_states_a_verdict(self, config_root, home):
        result, payload = self._both(config_root, home)
        surfaces = (json.dumps(payload).lower(),
                    render_scan_detail(result).lower())
        for surface in surfaces:
            for banned in ("severity", "risk", "score", "confidence",
                           "safe", "compliant", "violation"):
                assert banned not in surface


class TestToolRegistration:
    """Behind importorskip: the tool as a client actually sees it."""

    def _tool(self):
        pytest.importorskip("mcp")
        from sentience_governor.mcp_server.server import build_server
        tools = {t.name: t for t in build_server()._tool_manager.list_tools()}
        return tools

    def test_scan_is_registered_alongside_the_governance_tools(self):
        tools = self._tool()
        assert "sentience_scan" in tools
        # The shipped seven are untouched by this addition.
        assert {
            "sentience_explain", "sentience_profile_view", "sentience_pulse",
            "sentience_intent", "sentience_violations",
            "sentience_session_status", "sentience_declare_intent",
        } <= set(tools)

    def test_arguments_default_to_the_summary_over_all_history(self):
        schema = self._tool()["sentience_scan"].parameters
        properties = schema["properties"]
        assert properties["detail"]["default"] is False
        assert properties["since"]["default"] == "all"
        assert not schema.get("required")

    def test_description_states_read_only_and_the_continuation(self):
        description = flat(self._tool()["sentience_scan"].description)
        assert "read-only" in description.lower()
        assert "records nothing, and performs zero writes" in description
        assert "detail=True" in description
        assert "7d" in description and "30d" in description

    def test_description_claims_no_blocking_or_guarantee(self):
        description = flat(self._tool()["sentience_scan"].description).lower()
        for banned in ("block", "prevent", "approve", "guarantee", "enforce",
                       "safe", "secure", "protect", "stop"):
            assert banned not in description
        # And it says plainly what it cannot establish.
        assert "cannot establish what the user intended or authorized" in (
            description)


# ---------------------------------------------------------------------------
# v0.3.1.2 CP1 — per-session activity and the correlation key.
# ---------------------------------------------------------------------------


def bounded_corpus(config_root, home, *, findings=520):
    """A corpus that trips the display bound, with a quiet session beside it.

    `MAX_DISPLAY_FINDINGS` is 500, so `findings` distinct cross-project
    targets in one session leaves `findings_omitted > 0` while that session
    still holds retained findings. The quiet session holds none. Both states
    therefore occur in ONE scan, which is the case §4.2's ordering exists for.
    """
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    records = [
        activity("s-loud", cwd=str(source),
                 tools=[write_block(dest / ("f%04d.txt" % i))
                        for i in range(findings)]),
        activity("s-quiet", cwd=str(source), tools=[shell_block("echo hi")]),
    ]
    install(config_root, records)
    return config_root


def titled(session_id: str, title: str) -> dict:
    """The metadata record that gives a session a custom title."""
    return {"type": "custom-title", "sessionId": session_id,
            "customTitle": title}


def duplicate_title_corpus(config_root, home, title="release pass"):
    """Two sessions sharing one custom title (§3, §7.7).

    `retro.py` assigns customTitle → slug → session-id prefix with no dedupe
    step, so identical titles yield byte-identical labels. This is the case
    that makes a correlation key necessary rather than convenient.
    """
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    records = [
        titled("s-one", title),
        titled("s-two", title),
        activity("s-one", cwd=str(source), tools=[write_block(dest / "a.txt")]),
        activity("s-two", cwd=str(source), tools=[write_block(dest / "b.txt")]),
    ]
    install(config_root, records)
    return config_root


def rows_by_id(payload):
    return {row["session_id"]: row for row in payload["session_activity"]}


class TestSessionActivity:
    """§4.2 — every session in the window, not only the ones with findings.

    Nine of eleven sessions on the real corpus are named nowhere in either
    payload today. This array names them, reporting what Reader observed and
    asserting no relationship between activity and findings.
    """

    def test_every_reviewed_session_appears_exactly_once(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        rows = payload["session_activity"]
        assert len(rows) == payload["reviewed"]["sessions"] == 5
        ids = [row["session_id"] for row in rows]
        assert len(set(ids)) == len(ids)

    def test_sessions_without_findings_are_included(self, config_root, home):
        """The whole point: 3 of the 5 carry nothing and were previously
        collapsed into an integer."""
        payload = scan_payload(root=mixed_corpus(config_root, home))
        quiet = [row for row in payload["session_activity"]
                 if row["session_id"].startswith("s-quiet")]
        assert len(quiet) == 3
        for row in quiet:
            assert row["retrospective_findings"] == "absent"
            assert row["tool_calls"] == 1

    def test_session_id_is_the_full_identifier_not_a_derivative(
        self, config_root, home
    ):
        root = mixed_corpus(config_root, home)
        result = retro.scan(root)
        payload = scan_payload(root=root)
        assert set(rows_by_id(payload)) == set(result["by_session"])

    def test_counters_come_from_by_session_untransformed(
        self, config_root, home
    ):
        root = mixed_corpus(config_root, home)
        result = retro.scan(root)
        rows = rows_by_id(scan_payload(root=root))
        for session_id, counts in result["by_session"].items():
            row = rows[session_id]
            assert row["tool_calls"] == counts["tool_calls"]
            assert row["file_operations"] == counts["file_ops"]
            # Named for what Reader counts: shell TOOL INVOCATIONS. Equality
            # with no transformation is the contract (§4.2).
            assert row["shell_calls"] == counts["shell_calls"]

    def test_the_shell_counter_is_not_called_commands(self, config_root, home):
        """Reader does not interpret Bash, so it cannot count commands: one
        invocation can carry a pipeline or a whole script."""
        payload = scan_payload(root=mixed_corpus(config_root, home))
        for row in payload["session_activity"]:
            assert "shell_calls" in row
            assert not [key for key in row if "command" in key]

    def test_label_is_the_display_value_and_may_be_the_fallback(
        self, config_root, home
    ):
        root = mixed_corpus(config_root, home)
        result = retro.scan(root)
        rows = rows_by_id(scan_payload(root=root))
        for session_id, entry in result["session_labels"].items():
            assert rows[session_id]["session"] == entry["label"]

    def test_ordering_is_by_identifier_and_stable(self, config_root, home):
        """Position carries no meaning. Sorting by volume or finding count
        would be an importance ordering entering through the back door."""
        root = mixed_corpus(config_root, home)
        first = [row["session_id"]
                 for row in scan_payload(root=root)["session_activity"]]
        second = [row["session_id"]
                  for row in scan_payload(root=root)["session_activity"]]
        assert first == second == sorted(first)
        # The busiest session is not hoisted: `s-standout` sorts where its
        # identifier puts it, not where its three write operations would.
        rows = scan_payload(root=root)["session_activity"]
        assert rows[0]["session_id"] == "s-carrier"

    def test_summary_only(self, config_root, home):
        detail = scan_payload(detail=True, root=mixed_corpus(config_root, home))
        assert "session_activity" not in detail


class TestRetrospectiveFindingsEnum:
    """§4.2 — three states, and the ORDER of the tests that produce them."""

    def test_present_for_a_session_holding_a_retained_finding(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        rows = rows_by_id(payload)
        assert rows["s-standout"]["retrospective_findings"] == "present"
        assert rows["s-carrier"]["retrospective_findings"] == "present"

    def test_absent_requires_no_retained_finding_and_no_omission(
        self, config_root, home
    ):
        root = mixed_corpus(config_root, home)
        assert retro.scan(root)["findings_omitted"] == 0
        rows = rows_by_id(scan_payload(root=root))
        assert rows["s-quiet-0"]["retrospective_findings"] == "absent"

    def test_unknown_when_nothing_is_retained_under_a_bound(
        self, config_root, home
    ):
        root = bounded_corpus(config_root, home)
        assert retro.scan(root)["findings_omitted"] > 0
        rows = rows_by_id(scan_payload(root=root))
        assert rows["s-quiet"]["retrospective_findings"] == "unknown"

    def test_a_bound_never_erases_positive_knowledge(self, config_root, home):
        """The load-bearing case. Both branches in ONE scan: the bound clouds
        the negative and leaves the positive intact. Rev 3's acceptance
        criteria got this backwards (§7.8)."""
        root = bounded_corpus(config_root, home)
        payload = scan_payload(root=root)
        rows = rows_by_id(payload)
        assert retro.scan(root)["findings_omitted"] > 0
        assert rows["s-loud"]["retrospective_findings"] == "present"
        assert rows["s-quiet"]["retrospective_findings"] == "unknown"

    def test_the_field_is_a_string_enum_and_never_null(self, config_root, home):
        """A `has_`-prefixed nullable boolean would let a client treating
        JSON null as falsy turn 'cannot be established' into 'no findings'."""
        for root in (mixed_corpus(config_root, home),
                     bounded_corpus(config_root, home)):
            for row in scan_payload(root=root)["session_activity"]:
                state = row["retrospective_findings"]
                assert isinstance(state, str)
                assert state in ("present", "absent", "unknown")

    def test_present_count_agrees_with_the_shipped_accounting_field(
        self, config_root, home
    ):
        payload = scan_payload(root=mixed_corpus(config_root, home))
        present = [row for row in payload["session_activity"]
                   if row["retrospective_findings"] == "present"]
        assert len(present) == payload["sessions_carrying_findings"]


class TestCorrelationKey:
    """§4.2 — labels are not unique, so the array needs a join key."""

    def test_standout_entries_join_within_one_response(
        self, config_root, home
    ):
        """`other_reviewed[]` is not checked here because it exists only on
        detail, where `session_activity[]` is deliberately absent; that join
        is the cross-call case below."""
        payload = scan_payload(root=mixed_corpus(config_root, home))
        rows = rows_by_id(payload)
        for block in payload["standout"]:
            assert block["session_id"] in rows

    def test_the_key_is_on_every_session_bearing_object(
        self, config_root, home
    ):
        root = mixed_corpus(config_root, home)
        summary = scan_payload(root=root)
        detail = scan_payload(detail=True, root=root)
        for block in summary["standout"]:
            assert block["session_id"]
        for block in detail["standout"] + detail["other_reviewed"]:
            assert block["session_id"]

    def test_summary_and_detail_join_over_a_static_fixture(
        self, config_root, home
    ):
        """Two calls are two independent scans, so the fixture must be static
        and `now` pinned. This asserts the ONE thing the key guarantees:
        where a session occurs in both responses, it is the same session.

        It deliberately asserts nothing about presence, counter stability or
        ranking stability across the two calls, none of which the key
        guarantees (§4.2).
        """
        root = mixed_corpus(config_root, home)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        summary = scan_payload(root=root, now=now)
        detail = scan_payload(detail=True, root=root, now=now)
        rows = rows_by_id(summary)
        joined = [block for block in detail["standout"] + detail["other_reviewed"]
                  if block["session_id"] in rows]
        assert len(joined) == 2
        for block in joined:
            assert rows[block["session_id"]]["session"] == block["session"]

    def test_two_identically_titled_sessions_stay_distinguishable(
        self, config_root, home
    ):
        """§7.7, pinned as a test rather than left as an assumption: the
        labels collide by construction and the rows still join."""
        root = duplicate_title_corpus(config_root, home)
        payload = scan_payload(root=root)
        rows = payload["session_activity"]
        assert len(rows) == 2
        assert len({row["session"] for row in rows}) == 1
        assert {row["session_id"] for row in rows} == {"s-one", "s-two"}
        for block in payload["standout"]:
            matches = [row for row in rows
                       if row["session_id"] == block["session_id"]]
            assert len(matches) == 1


class TestDetailBoundary:
    """§4.4 rule 1 — the key is the ONLY thing this release puts on detail."""

    def test_no_detail_object_gains_a_counter_or_finding_state(
        self, config_root, home
    ):
        for root in (mixed_corpus(config_root, home),
                     bounded_corpus(config_root, home)):
            detail = scan_payload(detail=True, root=root)
            for block in detail["standout"] + detail["other_reviewed"]:
                for banned in ("tool_calls", "file_operations", "shell_calls",
                               "retrospective_findings", "session_activity"):
                    assert banned not in block

    def test_detail_session_blocks_carry_only_the_shipped_keys_plus_the_key(
        self, config_root, home
    ):
        detail = scan_payload(detail=True, root=mixed_corpus(config_root, home))
        for block in detail["standout"]:
            assert set(block) <= {"session_id", "session", "working_in",
                                  "groups"}


class TestCP1ClaimSweep:
    """§8 — the vocabulary guard, run under BOTH ordinary and flattened
    matching. Line wrapping produced a false negative three times while this
    plan was written, so a single-mode sweep is not evidence."""

    def _new_surface(self, config_root, home):
        """Everything this release adds, and nothing it inherits.

        The whole payload is deliberately NOT swept here: the shipped
        `_NO_FINDINGS_NOTE` says "not a statement that they were clean" and
        `_SCAN_LIMITS` says "whether an action complied with policy" — both
        negate the vocabulary rather than asserting it, and both are governed
        by the shipped `test_neither_surface_states_a_verdict`. Sweeping them
        again here would only prove the sweep can produce false positives.
        """
        parts = []
        for root in (mixed_corpus(config_root, home),
                     bounded_corpus(config_root, home)):
            summary = scan_payload(root=root)
            detail = scan_payload(detail=True, root=root)
            parts.append(json.dumps(summary["session_activity"]))
            parts.extend(json.dumps(block["session_id"])
                         for block in detail["standout"]
                         + detail["other_reviewed"])
        return [part.lower() for part in parts]

    def test_no_verdict_vocabulary_on_the_new_surface(self, config_root, home):
        for surface in self._new_surface(config_root, home):
            for mode in (surface, flat(surface)):
                for banned in ("severity", "risk", "score", "confidence",
                               "clean", "safe", "compliant", "violation",
                               "nothing happened"):
                    assert banned not in mode

    def test_the_new_vocabulary_stays_off_the_cli_screens(
        self, config_root, home
    ):
        """Both CLI screens are untouched by this release."""
        rendered = render_scan_detail(retro.scan(
            mixed_corpus(config_root, home)))
        for mode in (rendered, flat(rendered)):
            for absent in ("session_activity", "retrospective_findings",
                           "session_id"):
                assert absent not in mode
