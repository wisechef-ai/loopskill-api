"""Search recall widening (category + readme) + trending window transparency.

Two measured-on-live gaps in the discovery surface (2026-06-17 atomic-habits):

1. Literal `/api/skills/search` matched title + description ONLY. A query that
   is really a *category* term returned near-zero recall — measured live:
   `q=ops` returned 1 result while 12 skills are category=ops; `q=devops`
   returned 0 while 2 are category=devops. The fix widens the literal ILIKE to
   also match `category` + `readme`, and adds `category` to the BM25 scorer so
   hybrid recall ranks it too.

2. `/api/skills/trending` silently widens day→week→month→all-time (RCP-11) but
   never told the caller, so the UI couldn't relabel "Trending" → "Most
   installed" when it fell back to all-time. The response now carries a
   ``window`` field reporting the window the results came from.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models import Skill, TelemetryEvent


def _mk_skill(
    db, slug, *, title="A Skill", description="A description.", category="test", readme=None, tier="pro"
):
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        description=description,
        category=category,
        readme=readme,
        tier=tier,
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


class TestSearchRecallWidening:
    def test_category_term_matches_when_absent_from_title_and_description(self, client, db_session):
        """A query that only appears as the skill's CATEGORY must still match.

        Regression for the measured q=devops → 0 results gap.
        """
        _mk_skill(
            db_session,
            "infra-deployer",
            title="Infra Deployer",
            description="Provision and roll out cloud infrastructure.",
            category="devops",
        )
        r = client.get("/api/skills/search?q=devops")
        assert r.status_code == 200
        slugs = [x["slug"] for x in r.json()["results"]]
        assert "infra-deployer" in slugs, "category-only term 'devops' should match a category=devops skill"

    def test_readme_body_term_matches_when_absent_from_title_and_description(self, client, db_session):
        """A query appearing only in the README body must match."""
        _mk_skill(
            db_session,
            "quiet-skill",
            title="Quiet Skill",
            description="Short blurb.",
            category="misc",
            readme="This skill walks you through kubernetes pod autoscaling in depth.",
        )
        r = client.get("/api/skills/search?q=kubernetes")
        assert r.status_code == 200
        slugs = [x["slug"] for x in r.json()["results"]]
        assert "quiet-skill" in slugs, "readme-body term 'kubernetes' should match"

    def test_title_and_description_matches_still_work(self, client, db_session):
        """Widening must not regress the existing title/description matching."""
        _mk_skill(
            db_session,
            "memory-keeper",
            title="Memory Keeper",
            description="Persist agent memory.",
            category="ops",
        )
        r = client.get("/api/skills/search?q=memory")
        assert r.status_code == 200
        slugs = [x["slug"] for x in r.json()["results"]]
        assert "memory-keeper" in slugs

    def test_widening_does_not_match_unrelated_skills(self, client, db_session):
        """A term in no field must NOT surface a skill (no over-broad recall)."""
        _mk_skill(
            db_session,
            "alpha",
            title="Alpha",
            description="Beta gamma.",
            category="cat-a",
            readme="delta epsilon",
        )
        r = client.get("/api/skills/search?q=zzzznomatch")
        assert r.status_code == 200
        assert "alpha" not in [x["slug"] for x in r.json()["results"]]


class TestTrendingWindowTransparency:
    def test_trending_reports_all_time_window_on_widen(self, client, db_session):
        """When no recent installs exist, trending widens to all-time AND says so."""
        s = _mk_skill(db_session, "old-installer", title="Old Installer", description="x", category="ops")
        # An install event 200 days ago — outside day/week/month windows.
        db_session.add(
            TelemetryEvent(
                id=uuid4(),
                event_type="install",
                skill_slug=s.slug,
                created_at=datetime.now(timezone.utc) - timedelta(days=200),
            )
        )
        db_session.flush()

        r = client.get("/api/skills/trending?period=week")
        assert r.status_code == 200
        body = r.json()
        assert (
            body["window"] == "all"
        ), f"trending should report window=all after widening, got {body.get('window')}"
        assert "old-installer" in [x["slug"] for x in body["results"]]

    def test_trending_reports_requested_window_when_fresh(self, client, db_session):
        """When the requested window has installs, window echoes the request."""
        s = _mk_skill(db_session, "fresh-installer", title="Fresh Installer", description="x", category="ops")
        db_session.add(
            TelemetryEvent(
                id=uuid4(),
                event_type="install",
                skill_slug=s.slug,
                created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        db_session.flush()

        r = client.get("/api/skills/trending?period=week")
        assert r.status_code == 200
        assert r.json()["window"] == "week"

    def test_trending_empty_telemetry_reports_all_window(self, client, db_session):
        """No install telemetry anywhere → empty results, window=all (not null)."""
        _mk_skill(db_session, "never-installed", title="Never Installed", description="x", category="ops")
        r = client.get("/api/skills/trending?period=day")
        assert r.status_code == 200
        body = r.json()
        assert body["results"] == []
        assert body["window"] == "all"
