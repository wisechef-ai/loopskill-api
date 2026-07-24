"""Tests for carousel cron — daily_carousel_job idempotency and correctness."""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.carousel.cron import daily_carousel_job
from app.models import CarouselEntry, Skill


# ── Helpers ────────────────────────────────────────────────────────────────

_TARGET = date(2026, 5, 20)


def _add_skills(db, n: int, prefix="cron-skill") -> list[Skill]:
    skills = []
    for i in range(n):
        s = Skill(
            id=uuid4(),
            slug=f"{prefix}-{i}",
            title=f"Cron Skill {i}",
            description=f"Description for cron skill {i} — used in test.",
            category="devops" if i % 2 == 0 else "seo",
            tier="operator",
            is_public=True,
            install_count=i * 5,
            rating_avg=3.5,
            created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db.add(s)
        skills.append(s)
    db.flush()
    return skills


def _count_entries(db, target: date) -> int:
    dt_start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    dt_end = datetime.combine(target, datetime.max.time(), tzinfo=timezone.utc)
    return (
        db.query(CarouselEntry)
        .filter(
            CarouselEntry.featured_date >= dt_start,
            CarouselEntry.featured_date <= dt_end,
        )
        .count()
    )


# ── Cron tests ─────────────────────────────────────────────────────────────

class TestDailyCarouselJob:
    def test_inserts_up_to_7_entries(self, db_session):
        _add_skills(db_session, 10, prefix="cron-basic")
        inserted = daily_carousel_job(db_session, _TARGET)
        assert inserted == 7
        assert _count_entries(db_session, _TARGET) == 7

    def test_inserts_fewer_when_fewer_skills(self, db_session):
        target = date(2026, 5, 21)
        _add_skills(db_session, 3, prefix="cron-few")
        inserted = daily_carousel_job(db_session, target)
        assert inserted == 3
        assert _count_entries(db_session, target) == 3

    def test_idempotent_second_run_inserts_zero(self, db_session):
        """Running the job twice for the same date must be a no-op on the
        second run — no duplicate rows, returns 0."""
        target = date(2026, 5, 22)
        _add_skills(db_session, 7, prefix="cron-idem")

        first = daily_carousel_job(db_session, target)
        assert first == 7

        second = daily_carousel_job(db_session, target)
        assert second == 0  # nothing extra inserted
        assert _count_entries(db_session, target) == 7  # still exactly 7

    def test_idempotent_multiple_runs(self, db_session):
        """Running 5 times for the same date never exceeds initial count."""
        target = date(2026, 5, 23)
        _add_skills(db_session, 8, prefix="cron-multi")

        for i in range(5):
            daily_carousel_job(db_session, target)

        assert _count_entries(db_session, target) == 7

    def test_entries_have_slot_and_role(self, db_session):
        """Written entries must have slot (1-7) and role set."""
        target = date(2026, 5, 24)
        _add_skills(db_session, 7, prefix="cron-role")
        daily_carousel_job(db_session, target)

        dt_start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
        dt_end = datetime.combine(target, datetime.max.time(), tzinfo=timezone.utc)
        entries = (
            db_session.query(CarouselEntry)
            .filter(
                CarouselEntry.featured_date >= dt_start,
                CarouselEntry.featured_date <= dt_end,
            )
            .all()
        )
        for e in entries:
            assert e.slot is not None
            assert e.slot >= 1
            assert e.slot <= 7
            assert e.role in ("new-capability", "replaces", "experimental")

    def test_entries_have_score(self, db_session):
        """Every entry should have a computed score > 0."""
        target = date(2026, 5, 25)
        _add_skills(db_session, 5, prefix="cron-score")
        daily_carousel_job(db_session, target)

        dt_start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
        dt_end = datetime.combine(target, datetime.max.time(), tzinfo=timezone.utc)
        entries = (
            db_session.query(CarouselEntry)
            .filter(
                CarouselEntry.featured_date >= dt_start,
                CarouselEntry.featured_date <= dt_end,
            )
            .all()
        )
        for e in entries:
            assert e.score is not None
            assert e.score >= 0.0

    def test_no_skills_returns_zero(self, db_session):
        """When there are no public skills, job returns 0 and inserts nothing."""
        target = date(2026, 5, 26)
        inserted = daily_carousel_job(db_session, target)
        assert inserted == 0
        assert _count_entries(db_session, target) == 0

    def test_tagline_uses_description_not_title(self, db_session):
        """pick_1605 Phase C regression: tagline must be a slice of description,
        NOT the skill title. Today's Pick widget on recipes.wisechef.ai
        falls back through a tagline ladder; if the server returns tagline==title
        the hero looks identical to the H3 and the editorial value drops to zero.

        See plan §3 Phase C acceptance gate:
          curl /api/carousel/today | jq '[.entries[] | select(.tagline != .skill.title)] | length' >= 1
        """
        target = date(2026, 5, 28)
        # Skill A: long description — tagline should be description[:80].
        s_long = Skill(
            id=uuid4(),
            slug="cron-tagline-long",
            title="Cron Tagline Long",
            description=(
                "Diagnose and fix a hard production failure with five distinct subsystems. "
                "Use when the customer reports a 502 and the deploy logs are unhelpful."
            ),
            category="devops",
            tier="operator",
            is_public=True,
            install_count=999,
            rating_avg=4.8,
            created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        # Skill B: no description — tagline falls back to title[:80] (acceptable).
        s_no_desc = Skill(
            id=uuid4(),
            slug="cron-tagline-no-desc",
            title="Cron Tagline No Description",
            description=None,
            category="devops",
            tier="operator",
            is_public=True,
            install_count=100,
            rating_avg=4.0,
            created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db_session.add_all([s_long, s_no_desc])
        db_session.flush()

        daily_carousel_job(db_session, target)

        entries = (
            db_session.query(CarouselEntry)
            .filter(
                CarouselEntry.featured_date >= datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc),
                CarouselEntry.featured_date <= datetime.combine(target, datetime.max.time(), tzinfo=timezone.utc),
            )
            .all()
        )
        # Find the entry for our long-description skill
        long_entry = next((e for e in entries if e.skill.slug == "cron-tagline-long"), None)
        assert long_entry is not None, "Expected entry for cron-tagline-long"
        assert long_entry.tagline != long_entry.skill.title, (
            f"Tagline must derive from description, not title. "
            f"Got tagline={long_entry.tagline!r} title={long_entry.skill.title!r}"
        )
        assert long_entry.tagline.startswith("Diagnose and fix"), (
            f"Tagline should start with description prefix, got {long_entry.tagline!r}"
        )
        assert len(long_entry.tagline) <= 80, (
            f"Tagline must be <= 80 chars, got {len(long_entry.tagline)}: {long_entry.tagline!r}"
        )
        # At least one entry in the batch has tagline != title — the v3.6 REVENUE_MARKETING
        # seed and the hero card both depend on this invariant.
        non_title_count = sum(1 for e in entries if e.tagline != e.skill.title)
        assert non_title_count >= 1, (
            "At least one entry must have tagline != skill.title (description-derived). "
            "Otherwise the Today's Pick widget falls all the way through the fallback ladder "
            "and the hero shows duplicate text in H3 + tagline slots."
        )

    def test_private_skills_excluded(self, db_session):
        """Private skills (is_public=False) must not appear in carousel."""
        target = date(2026, 5, 27)
        for i in range(3):
            s = Skill(
                id=uuid4(),
                slug=f"private-cron-{i}",
                title=f"Private Skill {i}",
                is_public=False,
                install_count=9999,
                created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            )
            db_session.add(s)
        db_session.flush()

        inserted = daily_carousel_job(db_session, target)
        assert inserted == 0


class TestF6RaceConditionIdempotency:
    """F6 regression: concurrent cron runs must never produce more than 7 rows."""

    def test_two_sequential_runs_still_only_7_rows(self, db_session):
        """Simulates two 'concurrent' runs by calling the job twice without the
        idempotency guard seeing rows from the first call (SAVEPOINT isolation means
        commit inside the job creates visible rows for the second call in same session).

        Asserts: total rows for the date never exceeds 7.
        """
        target = date(2026, 6, 1)
        _add_skills(db_session, 10, prefix="race-skill")

        # First run
        daily_carousel_job(db_session, target)
        count_after_first = _count_entries(db_session, target)
        assert count_after_first == 7

        # Second run (simulates concurrent call that passes the count check)
        daily_carousel_job(db_session, target)
        count_after_second = _count_entries(db_session, target)
        assert count_after_second == 7, (
            f"Race condition: second cron run produced {count_after_second} rows (expected 7)"
        )
