"""v0.3.2 CP4: semantic shell classifier, table-driven over the accepted corpus.

``tests/fixtures/shell_classification/cp4d_corpus.json`` is the redacted
public copy of the accepted 546-fixture design corpus (path text inside a
few real commands is replaced; executables, subcommands, effects and
derived fields are untouched). It is the oracle: every fixture is checked
for parsed segment order, normalized executable and subcommand, the exact
effect list and order, ``complete`` and top-level ``destructive``. The
corpus's ``target_system`` values are retained for the hook wiring
checkpoint and are not asserted here.

Beyond the corpus: determinism, closed vocabularies, and robustness on
malformed, empty, non-string and adversarial input (the classifier never
raises). The named matrix classes point at the fixture ids that pin each
locked test row so a reader can find them without grepping the corpus.
"""

from __future__ import annotations

import json
import random
import string
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from sentience_governor.schema.events import (
    OPERATION_ACTIONS,
    OPERATION_DOMAINS,
    OperationClassification,
)
from sentience_governor.wrapper import shell_classification as sc
from sentience_governor.wrapper.shell_classification import classify_shell_command, split_segments

CORPUS = Path(__file__).parent / "fixtures" / "shell_classification" / "cp4d_corpus.json"
_DATA = json.loads(CORPUS.read_text(encoding="utf-8"))
FIXTURES: List[dict] = _DATA["fixtures"]
BY_ID: Dict[str, dict] = {f["id"]: f for f in FIXTURES}

Effect = Tuple[str, str, object]


def _segments_of(obj: OperationClassification):
    return [
        (s.executable, s.subcommand, [(e.domain, e.action, e.destructive) for e in s.effects])
        for s in obj.segments
    ]


def _expected_segments(fixture: dict):
    return [
        (s["executable"], s["subcommand"], [(e["domain"], e["action"], e["destructive"]) for e in s["effects"]])
        for s in fixture["segments"]
    ]


def _ids(prefix: str) -> List[str]:
    return [f["id"] for f in FIXTURES if f["id"].startswith(prefix)]


# ---------------------------------------------------------------------------
# the corpus, every fixture
# ---------------------------------------------------------------------------


def test_corpus_is_the_accepted_size():
    assert _DATA["schema"] == "cp4d-fixtures/1"
    assert _DATA["classifier"] == sc.CLASSIFIER_NAME
    assert _DATA["classifier_version"] == sc.CLASSIFIER_VERSION
    assert len(FIXTURES) == 546
    assert len(BY_ID) == 546
    assert all(f["pending"] is None for f in FIXTURES)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_corpus_fixture(fixture: dict):
    obj = classify_shell_command(fixture["command"])
    assert obj.classifier == "shell_rules" and obj.classifier_version == 1
    assert split_segments(fixture["command"]) == fixture["raw_segments"], "parsed segment order"
    assert _segments_of(obj) == _expected_segments(fixture), "executable / subcommand / effects (exact order)"
    assert obj.complete is fixture["complete"], "complete"
    assert obj.destructive is fixture["destructive"], "top-level destructive"
    # invariants the corpus derives from (§3 of the design): every way of
    # being incomplete leaves an explicit unknown effect behind.
    effects = [e for s in obj.segments for e in s.effects]
    assert obj.complete == all(e.domain != "unknown" for e in effects)
    for e in effects:
        assert e.domain in OPERATION_DOMAINS and e.action in OPERATION_ACTIONS
        assert e.destructive in (True, False, None)
        if e.domain == "unknown":
            assert e.action == "unknown" and e.destructive is None


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_corpus_fixture_is_deterministic(fixture: dict):
    a = classify_shell_command(fixture["command"]).model_dump()
    b = classify_shell_command(fixture["command"]).model_dump()
    assert a == b


def test_corpus_serializes_without_none_subcommand():
    for f in FIXTURES:
        dumped = classify_shell_command(f["command"]).model_dump()
        for s in dumped["segments"]:
            if s.get("subcommand") is None:
                assert "subcommand" not in s


# ---------------------------------------------------------------------------
# the locked CP4 matrix, by fixture id (each id is also run above)
# ---------------------------------------------------------------------------


def _effects_of(fid: str) -> List[Effect]:
    return [e for s in _segments_of(classify_shell_command(BY_ID[fid]["command"])) for e in s[2]]


class TestLockedRows:
    def test_row16_reads_across_domains(self):
        for fid, dom in [("VC-01", "version_control"), ("CL-01", "cloud_infrastructure"), ("CL-74", "cloud_infrastructure"),
                         ("CL-48", "cloud_infrastructure"), ("FS-02", "filesystem"), ("PK-01", "packages"), ("NW-01", "network")]:
            assert _effects_of(fid) == [(dom, "read", False)], fid

    def test_row17_creates_and_touch(self):
        for fid, dom in [("VC-41", None), ("FS-23", "filesystem"), ("FS-24", "filesystem"), ("CL-07", "cloud_infrastructure"),
                         ("CL-81", "cloud_infrastructure"), ("CL-102", "cloud_infrastructure")]:
            effects = _effects_of(fid)
            if fid == "VC-41":
                continue
            assert ("%s" % dom, "create", False) in effects, fid
        assert _effects_of("VC-32") == [("version_control", "create", False)]  # git commit
        assert _effects_of("FS-25") == [("filesystem", "modify", False)]  # touch, Amendment 1

    def test_row18_deletes(self):
        for fid in ("FS-48", "VC-63", "VC-62", "PK-54", "CL-13", "CL-64", "CL-95", "NW-16"):
            assert any(e[1] == "delete" and e[2] is True for e in _effects_of(fid)), fid

    def test_row19_modify_true(self):
        for fid in ("VC-26", "VC-60", "VC-58", "FS-44", "FS-46", "PK-50", "PK-51", "CL-94", "CL-107", "CL-28"):
            assert any(e[1] == "modify" and e[2] is True for e in _effects_of(fid)), fid

    def test_row20_destructive_null_on_state_dependent_effects(self):
        for fid in ("FS-26", "NW-04", "FS-29", "CL-61", "CL-84", "CL-103", "CL-19", "MN-14"):
            obj = classify_shell_command(BY_ID[fid]["command"])
            assert obj.destructive is None, fid
            assert any(e.destructive is None for s in obj.segments for e in s.effects), fid

    def test_row21_curl_o(self):
        obj = classify_shell_command("curl -o x https://example.com")
        assert _segments_of(obj) == [("curl", None, [("network", "read", False), ("filesystem", "modify", None)])]
        assert obj.complete is True and obj.destructive is None

    def test_row22_git_clone_url_vs_local(self):
        assert _effects_of("VC-18") == [("network", "read", False), ("version_control", "create", False)]
        assert _effects_of("VC-20") == [("version_control", "create", False)]

    def test_row23_network_presence_exactly_per_table(self):
        assert _effects_of("PK-19") == [("network", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("PK-20") == [("network", "read", False), ("packages", "modify", False)]
        assert _effects_of("PK-21") == [("packages", "modify", False)]
        assert _effects_of("VC-21") == [("network", "read", False), ("version_control", "modify", False)]
        assert _effects_of("VC-25") == [("network", "modify", False), ("version_control", "modify", False)]
        assert _effects_of("CL-59") == [("network", "read", False), ("cloud_infrastructure", "modify", False)]
        assert _effects_of("CL-106") == [("network", "read", False), ("packages", "modify", False)]

    def test_row24_no_synthetic_cross_effect(self):
        obj = classify_shell_command("rm -rf /tmp && aws ec2 describe-instances")
        assert _segments_of(obj) == [
            ("rm", None, [("filesystem", "delete", True)]),
            ("aws", "ec2 describe-instances", [("cloud_infrastructure", "read", False)]),
        ]
        assert obj.complete is True and obj.destructive is True
        effects = [e for s in _segments_of(obj) for e in s[2]]
        assert not any(e[0] == "cloud_infrastructure" and (e[1] == "delete" or e[2] is True) for e in effects)

    def test_git_push_force_puts_true_on_version_control_only(self):
        assert _effects_of("VC-26") == [("network", "modify", False), ("version_control", "modify", True)]

    def test_row29_row30_unknown_forms(self):
        obj = classify_shell_command("git status && ./deploy.sh")
        assert _segments_of(obj) == [("git", "status", [("version_control", "read", False)]), ("./deploy.sh", None, [("unknown", "unknown", None)])]
        assert obj.complete is False and obj.destructive is None
        for fid in ("PR-15", "PR-17", "PR-14", "CL-121"):
            obj = classify_shell_command(BY_ID[fid]["command"])
            assert obj.complete is False
            assert all(e == ("unknown", "unknown", None) for s in _segments_of(obj) for e in s[2]), fid
        assert _segments_of(classify_shell_command("docker build -t app ."))[0][0] == "docker"

    def test_rows53_56_redirections(self):
        assert _segments_of(classify_shell_command("echo x > f")) == [("echo", None, [("filesystem", "modify", None)])]
        assert _segments_of(classify_shell_command("echo x >> f")) == [("echo", None, [("filesystem", "modify", False)])]
        assert _segments_of(classify_shell_command("echo x 2>err.log")) == [("echo", None, [("filesystem", "modify", None)])]
        assert _segments_of(classify_shell_command("echo x 2>&1")) == []
        assert _effects_of("RD-01") == [("version_control", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("RD-02") == [("version_control", "read", False), ("filesystem", "modify", False)]
        assert _effects_of("RD-20") == [("network", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("PR-25") == [("process", "execute", None), ("filesystem", "modify", None)]
        assert _effects_of("CL-123") == [("cloud_infrastructure", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("RD-18") == [("filesystem", "read", False), ("filesystem", "modify", None)]

    def test_rows57_59_nesting(self):
        for cmd in ("echo $(aws ec2 describe-instances)", "echo `git status`"):
            obj = classify_shell_command(cmd)
            assert _segments_of(obj) == [("echo", None, [("unknown", "unknown", None)])], cmd
            assert obj.complete is False
        assert _effects_of("NS-04") == [("process", "execute", None), ("unknown", "unknown", None)]
        assert _effects_of("RD-26") == [("version_control", "read", False), ("filesystem", "modify", None), ("unknown", "unknown", None)]
        assert _segments_of(classify_shell_command("( git status )")) == [("(", None, [("unknown", "unknown", None)])]
        assert _segments_of(classify_shell_command("{ git status; }")) == [("{", None, [("unknown", "unknown", None)])]
        assert _effects_of("NS-16") == [("filesystem", "read", False), ("unknown", "unknown", None)]
        obj = classify_shell_command("echo '$(not run)'")
        assert obj.segments == [] and obj.complete is True

    def test_row60_material_network_boundary(self):
        for fid in ("MN-05", "MN-06", "MN-07", "MN-08", "MN-09", "MN-10", "MN-11", "MN-18"):
            assert not any(e[0] == "network" for e in _effects_of(fid)), fid
        for fid in ("MN-01", "MN-02", "MN-03", "MN-04", "MN-13", "MN-14", "MN-15", "MN-16", "MN-17"):
            assert any(e[0] == "network" for e in _effects_of(fid)), fid

    def test_amendment2_dev_null_boundary(self):
        assert _effects_of("RD-22") == [("filesystem", "read", False)]
        assert _effects_of("RD-23") == [("unknown", "unknown", None)]
        assert _effects_of("RD-33") == [("process", "execute", None)]
        assert _effects_of("RD-34") == [("filesystem", "read", False)]
        assert _effects_of("NW-41") == [("network", "read", False)]
        assert _effects_of("SG-02") == [("filesystem", "read", False)]
        # not generalised
        assert _effects_of("RD-35") == [("filesystem", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("RD-36") == [("filesystem", "read", False), ("filesystem", "modify", None)]
        assert _effects_of("RD-37") == [("filesystem", "modify", None), ("filesystem", "read", False)]
        for cmd in ("cat a > /dev/stderr", "cat a > /dev/tty", "wget -O /dev/stdout https://x"):
            assert ("filesystem", "modify", None) in [e for s in _segments_of(classify_shell_command(cmd)) for e in s[2]], cmd
        assert _segments_of(classify_shell_command("wget -O /dev/null https://x")) == [("wget", None, [("network", "read", False)])]

    def test_g6c_rejected_no_hyphen_word_inference(self):
        obj = classify_shell_command("aws ecr batch-delete-image --repository-name r --image-ids imageTag=old")
        assert _segments_of(obj) == [("aws", "ecr batch-delete-image", [("unknown", "unknown", None)])]
        assert obj.complete is False
        assert _effects_of("CL-124") == [("cloud_infrastructure", "read", False)]
        for cmd in ("aws ec2 batch-get-thing", "aws s3 ls s3://b/", "aws ec2 wait instance-running --instance-ids i-1"):
            assert _effects_of_cmd(cmd) == [("unknown", "unknown", None)], cmd

    def test_gaps_stay_unknown(self):
        for fid in ("VC-69", "VC-81", "VC-82", "VC-86", "CL-10", "CL-22", "CL-23", "FS-32", "PK-53", "PK-68", "PR-19", "PR-31", "SG-16", "SG-18"):
            assert any(e == ("unknown", "unknown", None) for e in _effects_of(fid)), fid
        assert _effects_of("PR-13") == [("process", "execute", None)]  # python -m pip is an interpreter execution


def _effects_of_cmd(cmd: str) -> List[Effect]:
    return [e for s in _segments_of(classify_shell_command(cmd)) for e in s[2]]


# ---------------------------------------------------------------------------
# normalization, quotes, neutral, opaque, multi-effect (beyond the corpus text)
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_path_strip_only_for_known_executables(self):
        assert _segments_of(classify_shell_command("/usr/bin/git status"))[0][0] == "git"
        assert _segments_of(classify_shell_command(".venv/bin/python x.py"))[0][0] == "python"
        assert _segments_of(classify_shell_command("./deploy.sh"))[0][0] == "./deploy.sh"
        assert _segments_of(classify_shell_command("/opt/tools/mystery --flag"))[0][0] == "/opt/tools/mystery"

    def test_versioned_interpreters_and_pip_variants(self):
        assert _effects_of_cmd("python3.12 app.py") == [("process", "execute", None)]
        assert _effects_of_cmd("pip3 install requests") == [("network", "read", False), ("packages", "modify", False)]

    def test_assignments_and_wrappers(self):
        assert _segments_of(classify_shell_command("A=1 B=2 sudo env C=3 nohup make")) == [("make", None, [("process", "execute", None)])]
        assert classify_shell_command("A=1").segments == []
        assert _segments_of(classify_shell_command("A=$(x)")) == [("A=", None, [("unknown", "unknown", None)])]

    @pytest.mark.parametrize("cmd,wrapper", [
        ("sudo -u root make", "sudo"),
        ("env -u FOO make", "env"),
        ("nice -n 10 make", "nice"),
        ("time -p make", "time"),
        ("nohup -- make", "nohup"),
        ("command -v make", "command"),
        ("sudo --preserve-env=PATH rm -rf /tmp/x", "sudo"),
        ("A=1 sudo -n env B=2 nohup make", "sudo"),
        ("sudo env -i make", "env"),
    ])
    def test_wrapper_option_syntax_is_unknown_never_an_operand(self, cmd, wrapper):
        """F-6: a transparent wrapper followed by option syntax is not parsed.
        The segment is an explicit unknown named for the wrapper; no option
        operand (root, FOO, 10, -p, make after an option) is ever reported."""
        obj = classify_shell_command(cmd)
        assert _segments_of(obj) == [(wrapper, None, [("unknown", "unknown", None)])], cmd
        assert obj.complete is False and obj.destructive is None
        assert not any(s[0] in ("root", "FOO", "10", "-p", "-u", "-n", "--", "-v", "-i", "make", "rm") for s in _segments_of(obj))

    def test_bare_wrappers_still_stripped(self):
        for cmd in ("sudo make", "env make", "time make", "nohup make", "nice make", "command make", "sudo env FOO=1 time nice nohup command make"):
            assert _segments_of(classify_shell_command(cmd)) == [("make", None, [("process", "execute", None)])], cmd
        assert _segments_of(classify_shell_command("sudo -u root make > out.log")) == [("sudo", None, [("unknown", "unknown", None), ("filesystem", "modify", None)])]

    def test_git_global_options_with_values(self):
        assert _segments_of(classify_shell_command("git -c core.pager=cat --no-pager -C /tmp status"))[0] == ("git", "status", [("version_control", "read", False)])

    def test_aws_global_options(self):
        seg = _segments_of(classify_shell_command("aws --profile p --output=json --no-cli-pager ec2 describe-instances"))[0]
        assert seg == ("aws", "ec2 describe-instances", [("cloud_infrastructure", "read", False)])

    def test_kubectl_leading_options(self):
        assert _segments_of(classify_shell_command("kubectl --context prod -n ns delete pod x"))[0][2] == [("cloud_infrastructure", "delete", True)]

    def test_curl_clusters_and_methods(self):
        assert _effects_of_cmd("curl -fsSLo out https://x") == [("network", "read", False), ("filesystem", "modify", None)]
        assert _effects_of_cmd("curl -sSL https://x") == [("network", "read", False)]
        assert _effects_of_cmd("curl -XPOST https://x") == [("network", "modify", None)]
        assert _effects_of_cmd("curl --request=DELETE https://x") == [("network", "delete", True)]
        assert _effects_of_cmd("curl -X get https://x") == [("network", "read", False)]

    def test_pip_local_targets(self):
        assert _effects_of_cmd("pip install -e . -r dev.txt") == [("network", "read", False), ("packages", "modify", False)]
        assert _effects_of_cmd("pip install -e ./pkg") == [("packages", "modify", False)]
        assert _effects_of_cmd("pip install ./a.whl ./b.whl") == [("packages", "modify", False)]


class TestQuotesAndSeparators:
    @pytest.mark.parametrize("cmd,expected", [
        ('echo "a && b"', []),
        ("echo 'x; y' && ls", ["echo 'x; y'", "ls"]),
        ('git commit -m "fix && ship"', ['git commit -m "fix && ship"']),
        ("a | b |& c & d; e || f && g\nh", ["a", "b", "c", "d", "e", "f", "g", "h"]),
        ("cmd arg \\\n  --flag", ["cmd arg    --flag"]),
        ("git status >| f", ["git status >| f"]),
        ("(cd x && make) && ls", ["(cd x && make)", "ls"]),
        ("{ a; b; } > f; c", ["{ a; b; } > f", "c"]),
        ("echo $(a; b) | c", ["echo $(a; b)", "c"]),
        ("cat <<EOF\nline; one\nEOF\nls", ["cat <<EOF", "ls"]),
    ])
    def test_split(self, cmd, expected):
        if expected == []:
            assert split_segments(cmd) == [cmd]
            assert classify_shell_command(cmd).segments == []
        else:
            assert split_segments(cmd) == expected

    def test_quoted_operators_are_data(self):
        assert classify_shell_command('echo ">"').segments == []
        assert _effects_of_cmd('grep "|" f') == [("filesystem", "read", False)]
        assert _effects_of_cmd("grep '2>' f") == [("filesystem", "read", False)]

    def test_constructs_detected_in_double_not_single_quotes(self):
        assert _effects_of_cmd('echo "$(x)"') == [("unknown", "unknown", None)]
        assert _effects_of_cmd("echo '$(x)'") == []
        assert _effects_of_cmd('echo "\\$(x)"') == []
        assert _effects_of_cmd('echo "`x`"') == [("unknown", "unknown", None)]
        assert _effects_of_cmd("echo '`x`'") == []
        assert _effects_of_cmd('echo "${HOME:-x}" $VAR') == []

    def test_one_unknown_per_segment(self):
        assert _effects_of_cmd("echo $(a) $(b) `c` <(d)") == [("unknown", "unknown", None)]
        assert _effects_of_cmd("./x $(a) > f") == [("unknown", "unknown", None), ("filesystem", "modify", None)]


class TestNeutralAndOpaque:
    @pytest.mark.parametrize("cmd", ["cd /tmp", "export A=1", "set -e", "unset X", "echo hi", "printf x", "true", "false", ":", "sleep 2", "alias l='ls'", "pushd x", "popd", ""])
    def test_neutral_without_redirection_emits_no_segment(self, cmd):
        obj = classify_shell_command(cmd)
        assert obj.segments == [] and obj.complete is True and obj.destructive is False

    def test_neutral_with_redirection(self):
        assert _segments_of(classify_shell_command(": > f")) == [(":", None, [("filesystem", "modify", None)])]
        assert _segments_of(classify_shell_command("read -r l < in")) == [("read", None, [("filesystem", "read", False)])]
        assert _segments_of(classify_shell_command("printf x >> f")) == [("printf", None, [("filesystem", "modify", False)])]

    @pytest.mark.parametrize("cmd", ["bash -c 'rm -rf /'", "sh -c x", "python -c 'import os'", "node -e 1", "ruby -e 1", "eval x", "xargs rm", "source x.sh", ". x.sh", "python3 - <<'PY'\nprint(1)\nPY", "zsh script.zsh"])
    def test_opaque_forms(self, cmd):
        obj = classify_shell_command(cmd)
        assert obj.complete is False
        assert [e for s in _segments_of(obj) for e in s[2]] == [("unknown", "unknown", None)]

    def test_opaque_wrapper_never_leaks_inner_words(self):
        effects = _effects_of_cmd("bash -c 'aws ec2 terminate-instances --instance-ids i-1 && rm -rf /'")
        assert effects == [("unknown", "unknown", None)]


# ---------------------------------------------------------------------------
# robustness: never raises, closed vocabularies, no side effects
# ---------------------------------------------------------------------------


class TestRobustness:
    @pytest.mark.parametrize("value", [None, 0, 1.5, b"ls", ["ls"], {"cmd": "ls"}, object()])
    def test_non_string_input_is_unknown_not_an_exception(self, value):
        obj = classify_shell_command(value)
        assert isinstance(obj, OperationClassification)
        assert obj.complete is False
        assert _segments_of(obj) == [("", None, [("unknown", "unknown", None)])]

    @pytest.mark.parametrize("cmd", [
        "", " ", "\n\n", "'", '"', "\"unterminated", "'unterminated", "(", ")", "{", "}", "((", "$(", "`", ">", ">>", "<", "2>", "&>", "|", "||", "&&", ";;", "& &", "cmd >", "cmd 2>", "cmd <<", "cmd <<<", "cmd >&", "cmd <&",
        "cmd <>", "cmd >&-", "cmd 3>&", "\\", "cmd \\", "a\\\nb", "\x00", "ls\x00-la", "cmd $((", "cmd ${", "cmd \"$(", "cmd '$(",
        "git", "git -C", "aws", "aws --profile", "kubectl -n", "gh", "gh pr", "terraform", "helm repo", "pulumi stack", "pip", "npm", "curl", "curl -o", "curl -X", "wget -O", "scp x", "rsync", "tar", "tar --", "sed", "tee", "python", "for", "if", "done", "}" * 50, "(" * 50, "$(" * 50, "a" * 100000,
    ])
    def test_malformed_input_never_raises(self, cmd):
        obj = classify_shell_command(cmd)
        assert isinstance(obj, OperationClassification)
        for s in obj.segments:
            for e in s.effects:
                assert e.domain in OPERATION_DOMAINS and e.action in OPERATION_ACTIONS
        assert obj.complete == all(e.domain != "unknown" for s in obj.segments for e in s.effects)

    def test_random_garbage_never_raises_and_is_deterministic(self):
        rng = random.Random(3202)
        alphabet = string.ascii_letters + string.digits + " \t\n'\"`$(){}|&;<>\\-=/.:~*?[]!#%^,"
        for _ in range(1500):
            cmd = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
            a = classify_shell_command(cmd)
            b = classify_shell_command(cmd)
            assert isinstance(a, OperationClassification)
            assert a.model_dump() == b.model_dump()
            for s in a.segments:
                for e in s.effects:
                    assert e.domain in OPERATION_DOMAINS and e.action in OPERATION_ACTIONS

    def test_internal_defect_degrades_to_unknown(self, monkeypatch):
        def boom(_raw):
            raise RuntimeError("defect")
        monkeypatch.setattr(sc, "_classify_segment", boom)
        obj = classify_shell_command("git status")
        assert obj.complete is False
        assert _segments_of(obj) == [("git", None, [("unknown", "unknown", None)])]

    def test_pure_no_io(self, monkeypatch, tmp_path):
        """The classifier must not touch the filesystem, run processes or open sockets."""
        import builtins
        import os
        import socket
        import subprocess

        def deny(*a, **k):
            raise AssertionError("side effect attempted")

        monkeypatch.setattr(builtins, "open", deny)
        monkeypatch.setattr(os, "system", deny)
        monkeypatch.setattr(subprocess, "run", deny)
        monkeypatch.setattr(subprocess, "Popen", deny)
        monkeypatch.setattr(socket, "socket", deny)
        for f in FIXTURES[::7]:
            classify_shell_command(f["command"])
        classify_shell_command(f"rm -rf {tmp_path} && cat {tmp_path}/x > /dev/null")
        assert tmp_path.exists()


class TestVocabularies:
    def test_declared_vocabularies_match_the_design(self):
        assert set(OPERATION_DOMAINS) == {"filesystem", "version_control", "packages", "network", "cloud_infrastructure", "process", "unknown"}
        assert set(OPERATION_ACTIONS) == {"read", "create", "modify", "delete", "execute", "unknown"}

    def test_every_corpus_effect_is_in_vocabulary(self):
        for f in FIXTURES:
            for s in f["segments"]:
                for e in s["effects"]:
                    assert e["domain"] in OPERATION_DOMAINS and e["action"] in OPERATION_ACTIONS
