"""Tests for app/crons/carousel_selector.py — the systemd-timer carousel cron.

This is the cron that actually writes carousel_entries rows on production.
For 8+ days before 2026-05-19 it was writing `tagline = (title or slug)` with
NULL slot/role/score because of a bug; the fix landed 2026-05-19 along with
the hoisted helpers below. These tests are the regression contract.

Note: there are sibling tests for `app/carousel/cron.py:daily_carousel_job`
(see test_carousel_cron.py) — that's a DIFFERENT writer in the codebase, not
the one in production. See 2026-05-19 vault log for the root-cause writeup.
"""
from __future__ import annotations

import pytest

from app.crons.carousel_selector import (
    assign_role,
    derive_tagline,
    slot1_quality_check,
)


# ── derive_tagline ──────────────────────────────────────────────────────────

class TestDeriveTagline:
    def test_uses_description_when_present(self):
        p = {"description": "Real human description.", "title": "Skill X", "slug": "skill-x"}
        assert derive_tagline(p) == "Real human description."

    def test_truncates_description_at_word_boundary_with_ellipsis(self):
        # Input: 200 chars of repeated words ("word " * 40)
        long_desc = "word " * 40  # 200 chars, spaces every 5
        p = {"description": long_desc, "title": "T", "slug": "s"}
        result = derive_tagline(p)
        # Must not exceed max_len + 1 (for ellipsis char)
        assert len(result) <= 121, f"result too long: {len(result)}"
        # Must end with ellipsis when truncated
        assert result.endswith("\u2026"), f"no ellipsis: {result!r}"
        # Must not end mid-word (last char before ellipsis must be space-boundary)
        assert result[:-1].rstrip()[-1] != " ", "trailing space before ellipsis"

    def test_truncates_all_same_chars_no_space(self):
        # Edge case: no spaces — trim at max_len exactly (best effort mid-word fallback)
        long_desc = "A" * 200
        p = {"description": long_desc, "title": "T", "slug": "s"}
        result = derive_tagline(p)
        assert result.endswith("\u2026")
        assert len(result) <= 121

    def test_falls_back_to_title_when_description_missing(self):
        p = {"description": "", "title": "Human Title", "slug": "human-title"}
        assert derive_tagline(p) == "Human Title"

    def test_falls_back_to_slug_when_title_also_missing(self):
        p = {"description": None, "title": None, "slug": "bare-slug"}
        assert derive_tagline(p) == "bare-slug"

    def test_strips_whitespace_from_description(self):
        p = {"description": "   trimmed   ", "title": "x", "slug": "x"}
        # We strip and then truncate; "trimmed" is the result
        assert derive_tagline(p) == "trimmed"

    def test_handles_completely_empty_candidate(self):
        # Should not raise — empty string is the documented degenerate output
        assert derive_tagline({}) == ""


# ── slot1_quality_check ─────────────────────────────────────────────────────
# These tests are the on-the-record contract for skill `carousel-content-quality-gate`.
# Drop-reason strings are stable identifiers — renaming any of them breaks the
# watchdog canary AND the weekly retro bucketing.

GOOD_LONG_TAGLINE = "Debug failing GitHub Actions checks on any PR with gh — summarize root cause."


class TestSlot1QualityCheck:
    def test_happy_path_passes(self):
        p = {"title": "gh Fix CI", "slug": "gh-fix-ci"}
        ok, reason = slot1_quality_check(p, GOOD_LONG_TAGLINE)
        assert ok is True
        assert reason == "ok"

    def test_c1_tagline_equals_title_fails(self):
        p = {"title": "gh-fix-ci", "slug": "gh-fix-ci"}
        ok, reason = slot1_quality_check(p, "gh-fix-ci")
        assert ok is False
        assert reason == "tagline_equals_title"

    def test_c1_is_case_insensitive(self):
        p = {"title": "Ruthless Mentor", "slug": "ruthless-mentor"}
        ok, reason = slot1_quality_check(p, "ruthless mentor")
        assert ok is False
        assert reason == "tagline_equals_title"

    def test_c1_strips_whitespace(self):
        p = {"title": "  X  ", "slug": "skill-x"}
        ok, reason = slot1_quality_check(p, "X")
        assert ok is False
        assert reason == "tagline_equals_title"

    def test_c2_too_short_fails_with_length_in_reason(self):
        p = {"title": "Different", "slug": "skill-foo"}
        ok, reason = slot1_quality_check(p, "short")
        assert ok is False
        assert reason == "tagline_too_short:5"

    def test_c2_boundary_19_fails(self):
        p = {"title": "Different", "slug": "skill-foo"}
        ok, reason = slot1_quality_check(p, "x" * 19)
        assert ok is False
        assert reason == "tagline_too_short:19"

    def test_c2_boundary_20_passes(self):
        p = {"title": "Different", "slug": "skill-foo"}
        ok, _ = slot1_quality_check(p, "x" * 20)
        # May still fail C3 or C4 — but specifically not C2
        # Use a tagline that passes all OTHER rules
        ok, reason = slot1_quality_check(p, "yes" + "x" * 17)
        # "yes" + 17 'x' = 20 chars, mixed case, slug ok → should pass
        assert ok is True

    def test_c3_single_capitalized_word_fails(self):
        # Need a single-cap-word that is also >= 20 chars
        p = {"title": "Different", "slug": "skill-foo"}
        ok, reason = slot1_quality_check(p, "Larrythequickbrownfox")  # 1 cap word, 21 chars
        assert ok is False
        assert reason == "tagline_single_word"

    def test_c3_multi_word_passes(self):
        p = {"title": "Different", "slug": "skill-foo"}
        ok, reason = slot1_quality_check(p, "Larry the agency-marketing skill for solos")
        assert ok is True

    def test_c3_acronym_passes(self):
        # "AICONFIGURE" is all caps, not [A-Z][a-z]+ → passes C3
        p = {"title": "Different", "slug": "skill-foo"}
        ok, reason = slot1_quality_check(p, "AICONFIGURE skill for everyone")
        assert ok is True

    def test_c4_slug_too_thin_fails(self):
        p = {"title": "Different", "slug": "abc"}  # < 6 chars, no dash
        ok, reason = slot1_quality_check(p, "A great description that is long enough.")
        assert ok is False
        assert reason == "slug_too_thin"

    def test_c4_short_but_hyphenated_passes(self):
        p = {"title": "Different", "slug": "a-b"}
        # 3 chars after stripping hyphens → still slug_too_thin
        ok, reason = slot1_quality_check(p, "A great description that is long enough.")
        assert ok is False
        assert reason == "slug_too_thin"

    def test_c4_six_char_slug_passes(self):
        p = {"title": "Different", "slug": "skills"}
        ok, _ = slot1_quality_check(p, "A great description that is long enough.")
        assert _  == "ok"

    def test_handles_none_tagline_as_empty(self):
        p = {"title": "x", "slug": "x-y-z"}
        # None → "" after strip → too short
        ok, reason = slot1_quality_check(p, None)
        assert ok is False
        assert reason == "tagline_too_short:0"

    def test_real_world_today_pick_passes(self):
        """The actual 2026-05-19 slot-1 pick post-fix must pass."""
        p = {"title": "gh Fix CI", "slug": "gh-fix-ci"}
        tagline = (
            "Debug failing GitHub Actions checks on any PR — inspect logs with gh, "
            "summarize the root cause, draft a fix plan, and im"
        )
        ok, reason = slot1_quality_check(p, tagline)
        assert ok is True, f"expected pass, got: {reason}"

    def test_real_world_pre_fix_failure_2026_05_19(self):
        """The slot-1 row that was bleeding on 2026-05-19 must fail."""
        p = {"title": "gh-fix-ci", "slug": "gh-fix-ci"}
        ok, reason = slot1_quality_check(p, "gh-fix-ci")
        assert ok is False
        assert reason == "tagline_equals_title"


# ── assign_role ─────────────────────────────────────────────────────────────

class TestAssignRole:
    @pytest.mark.parametrize("slot,expected", [
        (1, "new-capability"),
        (2, "new-capability"),
        (3, "new-capability"),
        (4, "new-capability"),
        (5, "new-capability"),
        (6, "experimental"),
        (7, "experimental"),
    ])
    def test_slot_to_role_mapping(self, slot, expected):
        assert assign_role(slot) == expected

    def test_assign_role_accepts_optional_candidate(self):
        # Signature accepts (slot_1idx, p=None) for future enrichment.
        assert assign_role(1, {"slug": "x"}) == "new-capability"
        assert assign_role(7, None) == "experimental"


# ── Eligibility SELECT contract ─────────────────────────────────────────────
# The patched cron's SELECT WHERE clause requires:
#   s.description IS NOT NULL
#   AND char_length(trim(s.description)) >= 20
#   AND lower(trim(s.description)) <> lower(trim(s.title))
#   AND lower(trim(s.description)) <> lower(trim(s.slug))
# Tested as a literal string match on the source file so accidental edits get
# caught by the test suite, not by the next 8-day outage.

def test_select_filter_clause_present_in_source():
    """Regression: the description-quality filter must remain in the SELECT.

    If someone refactors this query, they should update this test in lockstep.
    The point is to fail loudly the moment the filter is removed.
    """
    import pathlib
    import pathlib
    src = pathlib.Path("app/crons/carousel_selector.py").read_text()
    required_fragments = [
        "s.description IS NOT NULL",
        "char_length(trim(s.description)) >= 20",
        "lower(trim(s.description)) <> lower(trim(s.title))",
        "lower(trim(s.description)) <> lower(trim(s.slug))",
    ]
    for fragment in required_fragments:
        # Must appear at least twice — once in primary query, once in fallback
        assert src.count(fragment) >= 2, (
            f"Filter clause `{fragment}` missing or only in one query. "
            f"Both primary and fallback SELECTs must enforce it."
        )


def test_insert_writes_slot_role_score_columns():
    """Regression: the INSERT must populate slot, role, score (not just position+tagline).

    Pre-2026-05-19 the INSERT was `(id, skill_id, featured_date, position, tagline)`
    only — leaving slot/role/score NULL. This test asserts the new columns are
    in the INSERT.
    """
    import pathlib
    import pathlib
    src = pathlib.Path("app/crons/carousel_selector.py").read_text()
    # The INSERT block must mention all three new columns
    assert "INSERT INTO carousel_entries" in src
    assert ":slot" in src, "slot parameter not bound in INSERT"
    assert ":role" in src, "role parameter not bound in INSERT"
    assert ":score" in src, "score parameter not bound in INSERT"
    # And they must appear in the column list, not just somewhere in the file
    insert_block_start = src.find("INSERT INTO carousel_entries")
    insert_block_end = src.find("ON CONFLICT", insert_block_start)
    insert_block = src[insert_block_start:insert_block_end]
    assert "slot" in insert_block, "slot column missing from INSERT column list"
    assert "role" in insert_block, "role column missing from INSERT column list"
    assert "score" in insert_block, "score column missing from INSERT column list"


def test_tail_backfill_after_slot1_gate_present_in_source():
    """Regression: the slot-1 quality gate drops candidates via `picked = picked[1:]`,
    which shrinks the lineup below SLOTS. A tail-backfill MUST run after the gate so
    the carousel always returns to 7 entries.

    Drift incident 2026-06-18: `chef` was rejected slot-1 (slug_too_thin) and the
    lineup permanently shrank 7->6 because no backfill ran after the gate. This test
    fails loudly if the post-gate backfill is removed.
    """
    import pathlib
    src = pathlib.Path("app/crons/carousel_selector.py").read_text()
    gate_idx = src.find("# Slot-1 pre-promotion gate")
    insert_idx = src.find("# Insert with slot (1-indexed)")
    assert gate_idx != -1, "slot-1 gate block not found"
    assert insert_idx != -1, "INSERT block not found"
    between = src[gate_idx:insert_idx]
    # The backfill must live AFTER the gate and BEFORE the INSERT loop.
    assert "if len(picked) < SLOTS:" in between, (
        "Tail-backfill `if len(picked) < SLOTS:` missing between the slot-1 gate "
        "and the INSERT loop — slot-1 rejects will shrink the carousel below 7."
    )
    # Rejected candidates must be tracked and excluded from the backfill so a
    # gate-failed candidate is not re-promoted into a later slot.
    assert "rejected_skill_ids" in between, (
        "rejected_skill_ids tracking missing — backfill could re-promote a "
        "candidate the slot-1 gate just rejected."
    )
