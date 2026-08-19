"""Line-ending handling in the 3-way merge.

Regression cover for the bug that made the updater unusable on Windows: base and
remote arrive from the upstream payload with LF terminators, local is the file on
the user's disk with CRLF, and the merge compares whole lines *including* the
terminator. Every line mismatched, SequenceMatcher anchored nothing, and an
untouched file came back as one whole-file conflict.

The invariant these tests protect: a local file whose only difference from base
is its line-ending convention has NO local changes, and must merge clean.
"""
import pytest

from gitplucker.merge import merge_text, annotate_three_way_text
from gitplucker.merge.three_way import _dominant_eol, _to_lf, _restore_eol, CR, LF, CRLF

SRC = "import os\nimport sys\n\ndef main():\n    return 0\n"
UPSTREAM = SRC.replace("return 0", "return 1")


def crlf(t):
    return t.replace(LF, CRLF)


# ── the reported bug ────────────────────────────────────────────────────────

def test_crlf_local_with_no_edits_merges_clean():
    res = merge_text(SRC, crlf(SRC), UPSTREAM)
    assert res.conflicts == 0
    assert res.clean
    assert res.text.replace(CRLF, LF) == UPSTREAM


def test_crlf_local_review_shows_no_conflict_tags():
    tags = {t for t, _ in annotate_three_way_text(SRC, crlf(SRC), UPSTREAM)}
    assert not any(t.startswith("conflict") for t in tags)
    assert "update_add" in tags


def test_crlf_local_is_not_reported_as_local_edits():
    """Line endings are not authorship. A CRLF working copy must not have every
    line attributed to the user, or the review pane is meaningless."""
    tags = {t for t, _ in annotate_three_way_text(SRC, crlf(SRC), UPSTREAM)}
    assert "local_add" not in tags and "local_del" not in tags


# ── the merged output must keep the user's own convention ───────────────────

def test_merged_output_preserves_crlf():
    res = merge_text(SRC, crlf(SRC), UPSTREAM)
    assert CRLF in res.text
    assert CR not in res.text.replace(CRLF, "")   # no lone CR left behind


def test_merged_output_preserves_lf():
    res = merge_text(SRC, SRC, UPSTREAM)
    assert CR not in res.text


def test_output_convention_follows_local_not_remote():
    """The file being rewritten is the LOCAL one; adopting remote's convention
    would rewrite every line and show as a whole-file diff on the next check."""
    assert CRLF in merge_text(SRC, crlf(SRC), UPSTREAM).text
    assert CR not in merge_text(crlf(SRC), SRC, crlf(UPSTREAM)).text


# ── real conflicts must still be found across mixed endings ─────────────────

def test_genuine_conflict_still_detected_across_endings():
    local = crlf(SRC.replace("return 0", "return 99"))
    res = merge_text(SRC, local, UPSTREAM)
    assert res.conflicts == 1
    assert not res.clean


def test_local_only_edit_survives_across_endings():
    local = crlf(SRC.replace("import sys", "import sys\nimport json"))
    res = merge_text(SRC, local, SRC)
    assert res.clean
    assert "import json" in res.text


# ── helpers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("a\r\nb\r\n", CRLF),
    ("a\nb\n", LF),
    ("a\rb\r", CR),
    ("", LF),
    ("no trailing newline", LF),
    ("a\r\nb\r\nc\n", CRLF),      # mixed -> dominant wins
])
def test_dominant_eol(text, expect):
    assert _dominant_eol(text) == expect


def test_to_lf_collapses_every_convention():
    assert _to_lf("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_restore_eol_roundtrips():
    for eol in (LF, CR, CRLF):
        assert _to_lf(_restore_eol("a\nb\nc\n", eol)) == "a\nb\nc\n"


def test_mixed_ending_local_does_not_explode_into_conflict():
    local = "import os\r\nimport sys\r\n\ndef main():\r\n    return 0\n"
    res = merge_text(SRC, local, UPSTREAM)
    assert res.conflicts == 0
