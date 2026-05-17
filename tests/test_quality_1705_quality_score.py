"""tests/test_quality_1705_quality_score.py — Phase C scoring gates."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.models import Base, Skill  # noqa: E402
from scripts import quality_1705_compute_quality_score as scorer  # noqa: E402


@pytest.fixture()
def db_engine(tmp_path):
    db_path = tmp_path / "qs.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, future=True)


def test_quality_score_column_exists(db_engine):
    with db_engine.connect() as conn:
        ddl = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='skills'"
        )).scalar()
    assert "quality_score" in ddl


def test_install_score_zero_for_no_installs():
    assert scorer._install_score(0, [0, 5, 10, 100]) == 0.0


def test_install_score_full_for_top_installer():
    s = scorer._install_score(100, [0, 5, 10, 100])
    assert s == 10.0


def test_freshness_score_full_for_recent():
    now = datetime.now(timezone.utc)
    assert scorer._freshness_score(now, now) == 10.0
    assert scorer._freshness_score(now - timedelta(days=29), now) == 10.0


def test_freshness_score_zero_for_year_old():
    now = datetime.now(timezone.utc)
    assert scorer._freshness_score(now - timedelta(days=365), now) == 0.0


def test_freshness_score_decays_mid_range():
    now = datetime.now(timezone.utc)
    s = scorer._freshness_score(now - timedelta(days=180), now)
    assert 3 < s < 7  # roughly midway


def test_freshness_score_none_returns_zero():
    assert scorer._freshness_score(None, datetime.now(timezone.utc)) == 0.0


def test_description_score_with_outcome_verb():
    desc = "Generates a PDF report from GA4 and Stripe data, branded and white-label-ready, replacing 4h/week of manual work."
    assert scorer._description_score(desc) == 10.0


def test_description_score_long_without_verb():
    desc = (
        "An interesting tool that helps users do something useful with their "
        "data — useful for many scenarios in modern software development."
    )
    # Does not start with an outcome verb
    assert scorer._description_score(desc) == 5.0


def test_description_score_too_short():
    assert scorer._description_score("short desc") == 0.0


def test_description_score_none():
    assert scorer._description_score(None) == 0.0


def test_compute_score_new_skill_capped_at_8_5():
    """F8 mitigation: skills < 14 days old cap at 8.5."""
    now = datetime.now(timezone.utc)
    score = scorer.compute_score(
        install_count=100,
        last_verified=now,
        description="Generates excellent things, with care, for everyone who uses it.",
        created_at=now - timedelta(days=3),  # brand new
        all_install_counts=[0, 0, 100],
        now=now,
    )
    assert score <= 8.5


def test_compute_score_mature_skill_can_reach_high():
    """A mature skill with all signals strong (including unhappy_paths) should score > 8.0."""
    now = datetime.now(timezone.utc)
    readme_5 = """---
unhappy_paths:
  - condition: GA4 service account loses property-level Viewer access when team rotates roles
    recovery: Re-add the service-account email under Property Access Management with Viewer role
  - condition: Meta Ads token returns 190 when the long-lived token approaches its 60-day window
    recovery: Generate a System User token in Meta Business Suite for a non-expiring credential
  - condition: weasyprint OSError on missing libgobject system dependency in fresh container
    recovery: apt install libpango-1.0-0 libpangoft2-1.0-0 libglib2.0-0 in the deploy image
  - condition: TikTok Ads API 429 when iterating 8 clients in tight loop without sleep
    recovery: Add time.sleep(2) between TikTok report calls and batch into hourly windows
  - condition: PDF font fallback rendering breaks branded client deliverables when LaTeX absent
    recovery: Embed brand font with @font-face base64 in template CSS, drop LaTeX dependency
---
body
"""
    score = scorer.compute_score(
        install_count=500,
        last_verified=now,
        description="Generates a comprehensive PDF report from analytics data, branded and white-label-ready, saving hours per week.",
        created_at=now - timedelta(days=60),
        all_install_counts=[0, 10, 50, 500],
        now=now,
        readme=readme_5,
    )
    assert score >= 8.0


def test_main_dry_run_writes_nothing(session_factory, monkeypatch, capsys):
    """Dry-run must not mutate."""
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    sys.argv = ["scorer", "--db-url", db_url]
    scorer.main()

    SessionLocal = sessionmaker(bind=session.bind.engine, future=True)
    with SessionLocal() as s:
        score = s.execute(text("SELECT quality_score FROM skills WHERE slug='test-skill'")).scalar()
    assert score is None, "Dry-run should not write quality_score"


def test_main_commit_writes_scores(session_factory):
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes that really matters.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    scorer.main()

    SessionLocal = sessionmaker(bind=session.bind.engine, future=True)
    with SessionLocal() as s:
        score = s.execute(text("SELECT quality_score FROM skills WHERE slug='test-skill'")).scalar()
    assert score is not None
    assert 0 <= score <= 10


def test_main_is_idempotent(session_factory):
    """Running --commit twice produces zero updates on the second run."""
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes that really matters.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    import io
    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    scorer.main()

    # Second run: capture stdout, parse updated_count
    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scorer.main()
    out = buf.getvalue()
    import json
    payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert payload["updated_count"] == 0, "Idempotent: second --commit produces zero diffs"


# ─────────────────────────────────────────────────────────────────────────
# Phase C-content: unhappy_paths scoring
# ─────────────────────────────────────────────────────────────────────────


def test_unhappy_paths_score_zero_for_no_readme():
    assert scorer._unhappy_paths_score(None) == 0.0
    assert scorer._unhappy_paths_score("") == 0.0


def test_unhappy_paths_score_zero_for_no_frontmatter():
    assert scorer._unhappy_paths_score("Hello world, no frontmatter here") == 0.0


def test_unhappy_paths_score_zero_for_malformed_frontmatter():
    bad = "---\nunhappy_paths: [this: is: not: valid: yaml\n---\nbody"
    assert scorer._unhappy_paths_score(bad) == 0.0


def test_unhappy_paths_score_zero_when_key_missing():
    rm = "---\ntitle: Foo\ndescription: bar\n---\nbody"
    assert scorer._unhappy_paths_score(rm) == 0.0


def test_unhappy_paths_score_low_for_one_entry():
    rm = (
        "---\nunhappy_paths:\n"
        "  - condition: x rate limit\n"
        "    recovery: backoff and retry\n"
        "---\nbody"
    )
    assert scorer._unhappy_paths_score(rm) == 3.0


def test_unhappy_paths_score_seven_for_three_substantial_entries():
    rm = """---
unhappy_paths:
  - condition: Stripe webhook signature mismatch on rotated secret
    recovery: Pull live secret via stripe CLI and redeploy
  - condition: 429 rate limit on batch API calls in tight loop
    recovery: Add exponential backoff with jitter, max 5 retries
  - condition: webhook timestamp drift exceeds 300s on slow workers
    recovery: Bump tolerance parameter on construct_event to 600s
---
body
"""
    assert scorer._unhappy_paths_score(rm) == 7.0


def test_unhappy_paths_score_ten_for_five_meaty_entries():
    rm = """---
unhappy_paths:
  - condition: Stripe webhook signature mismatch when STRIPE_WEBHOOK_SECRET rotates outside the deploy pipeline
    recovery: Pull live secret via stripe webhook_endpoints retrieve we_xxx and redeploy with dashboard value
  - condition: 429 rate limit on batch API calls in tight loop without backoff causes cascading failures
    recovery: Add exponential backoff with jitter, max 5 retries, 2s base, document on every call site
  - condition: webhook timestamp drift exceeds 300s on slow workers under load spike during cron windows
    recovery: Bump tolerance parameter on construct_event() to 600s and add NTP sync check at deploy
  - condition: idempotency-key collision causes duplicate charges across retries when SDK retries internally
    recovery: Hash request body + customer_id, use as idempotency key, store in Redis 24h for replay protection
  - condition: SDK version drift breaks Event.data.object access pattern after auto-update merged untested
    recovery: Pin stripe SDK to 15.x in requirements.txt and run scripts/stripe_compat_check.py in CI
---
body
"""
    assert scorer._unhappy_paths_score(rm) == 10.0


def test_unhappy_paths_score_three_entries_too_short_falls_to_three():
    """3 entries but average text is too short — falls through to the 1-entry bucket."""
    rm = """---
unhappy_paths:
  - condition: x
    recovery: y
  - condition: a
    recovery: b
  - condition: m
    recovery: n
---
body
"""
    assert scorer._unhappy_paths_score(rm) == 3.0


def test_unhappy_paths_skipped_on_empty_strings():
    """Entries with empty condition or recovery are ignored."""
    rm = """---
unhappy_paths:
  - condition: ""
    recovery: real recovery action
  - condition: real condition
    recovery: ""
  - condition: actually substantial condition text here
    recovery: actually substantial recovery text here
---
body
"""
    # Only 1 valid entry survives
    assert scorer._unhappy_paths_score(rm) == 3.0


def test_compute_score_with_unhappy_paths_lifts_score():
    """Adding unhappy_paths to a readme should bump score by >=1.0 over no-readme baseline."""
    now = datetime.now(timezone.utc)
    base = scorer.compute_score(
        install_count=10,
        last_verified=now,
        description="Generates good things for testing purposes that really matters.",
        created_at=now - timedelta(days=60),
        all_install_counts=[0, 10, 50],
        now=now,
        readme=None,
    )
    rm = """---
unhappy_paths:
  - condition: Stripe webhook signature mismatch when STRIPE_WEBHOOK_SECRET rotates outside the deploy pipeline
    recovery: Pull live secret via stripe webhook_endpoints retrieve we_xxx and redeploy with dashboard value
  - condition: 429 rate limit on batch API calls in tight loop without backoff causes cascading failures
    recovery: Add exponential backoff with jitter, max 5 retries, 2s base, document on every call site
  - condition: webhook timestamp drift exceeds 300s on slow workers under load spike during cron windows
    recovery: Bump tolerance parameter on construct_event() to 600s and add NTP sync check at deploy
  - condition: idempotency-key collision causes duplicate charges across retries when SDK retries internally
    recovery: Hash request body + customer_id, use as idempotency key, store in Redis 24h replay protection
  - condition: SDK version drift breaks Event.data.object access pattern after auto-update merged untested
    recovery: Pin stripe SDK to 15.x in requirements.txt and run scripts/stripe_compat_check.py in CI
---
body
"""
    with_unhappy = scorer.compute_score(
        install_count=10,
        last_verified=now,
        description="Generates good things for testing purposes that really matters.",
        created_at=now - timedelta(days=60),
        all_install_counts=[0, 10, 50],
        now=now,
        readme=rm,
    )
    # +0.20 weight × 10.0 vs +0.20 weight × 0.0 = +2.0 boost expected
    assert with_unhappy - base >= 1.5
