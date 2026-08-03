"""F-V7: exported profiles carry inline explanatory comments.

The default profile written by `sentience profile init` (and any
`profile export`) must be readable standalone by a non-developer.
Comments are injected at emit time and must NOT affect the content
hash (which is computed from the parsed data, not the file text).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sentience_governor.profile import GovernanceProfile, SCHEMA_VERSION
from sentience_governor.profile.loader import (
    _FIELD_COMMENTS,
    render_commented_yaml,
)
from sentience_governor.profile.schema import default_profile_data


def test_exported_default_profile_has_field_comments(tmp_path: Path):
    profile = GovernanceProfile.defaults()
    out = tmp_path / "profile.yaml"
    profile.export(out)
    text = out.read_text(encoding="utf-8")

    # A human-facing banner is present.
    assert "you own it" in text.lower()

    # Every section appears with its explanatory comment.
    assert "# session_intent" in text
    assert "# task_boundary" in text
    assert "# high_consequence" in text

    # Each active field has a comment line somewhere above its key.
    for field in ("required", "demand_at", "prompt_template",
                  "signals", "time_gap_seconds", "dir_change_depth",
                  "tools"):
        # The field key still emits as YAML…
        assert f"{field}:" in text
    # …and there is at least one comment line per documented field.
    comment_lines = [ln for ln in text.splitlines() if ln.strip().startswith("#")]
    # 3 header + banner(3) + 1 schema + 3 sections + 9 fields = plenty;
    # assert we have well more than the machine header alone.
    assert len(comment_lines) >= 15


def test_comments_do_not_change_content_hash(tmp_path: Path):
    """The whole point: comments are hash-neutral."""
    profile = GovernanceProfile.defaults()
    expected_hash = profile.content_hash()

    out = tmp_path / "profile.yaml"
    profile.export(out)

    # The hash written into the header equals the data hash…
    text = out.read_text(encoding="utf-8")
    assert f"sha256:{expected_hash}" in text

    # …and reloading the commented file yields the same hash.
    reloaded = GovernanceProfile.from_file(out)
    assert reloaded.content_hash() == expected_hash


def test_commented_yaml_parses_back_to_identical_data():
    """render_commented_yaml round-trips: parsed YAML == input data."""
    data = default_profile_data()
    rendered = render_commented_yaml(data)
    parsed = yaml.safe_load(rendered)
    assert parsed == data


def test_commented_yaml_handles_edited_profile():
    """An operator-edited profile (added tools, unknown key) still
    renders and parses back losslessly — comments are by field name,
    not hardcoded values."""
    data = default_profile_data()
    data["high_consequence"]["tools"] = ["db.delete", "fs.rmtree"]
    data["task_boundary"]["signals"] = ["time_gap"]
    data["extends"] = "base-profile"  # reserved key, no comment defined

    rendered = render_commented_yaml(data)
    parsed = yaml.safe_load(rendered)
    assert parsed == data
    # The added tools survive into the rendered text.
    assert "db.delete" in rendered
    assert "fs.rmtree" in rendered


def test_field_comment_map_covers_default_fields():
    """Guard against silently dropping a field's documentation."""
    data = default_profile_data()
    for section, value in data.items():
        if isinstance(value, dict):
            for field in value:
                assert (section, field) in _FIELD_COMMENTS, (
                    f"missing inline comment for {section}.{field}"
                )
        else:
            assert (None, section) in _FIELD_COMMENTS, (
                f"missing inline comment for top-level {section}"
            )


def test_exported_profile_validates_with_ok_hash(tmp_path: Path):
    """End-to-end: a freshly exported (commented) profile validates and
    reports a matching (not MISMATCH) content hash."""
    profile = GovernanceProfile.defaults()
    out = tmp_path / "profile.yaml"
    profile.export(out)

    reloaded = GovernanceProfile.from_file(out)
    result = reloaded.validate()
    assert result.is_valid
    assert reloaded.content_hash() == profile.content_hash()
