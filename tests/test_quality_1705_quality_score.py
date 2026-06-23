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
    # v2: 0 installs is now NEUTRAL (5.0), not 0.0. Rationale: at catalog
    # launch, 80%+ of skills have 0 installs by definition — penalising them
    # turns a fresh-launch problem into a quality problem.
    assert scorer._install_score(0, [0, 5, 10, 100]) == 5.0


def test_install_score_full_for_top_installer():
    s = scorer._install_score(100, [0, 5, 10, 100])
    assert s == 10.0


def test_install_score_above_neutral_for_some_installs():
    # v2: any installs > 0 score in 6..10 band (proven adoption bonus)
    s = scorer._install_score(5, [0, 5, 10, 100])
    assert 6.0 <= s <= 10.0


def test_install_score_neutral_when_whole_catalog_new():
    # v2: when no skill has installs, everyone gets neutral 5.0
    assert scorer._install_score(0, [0, 0, 0, 0]) == 5.0
    assert scorer._install_score(0, []) == 5.0


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


def test_freshness_score_none_returns_neutral():
    # v2: missing last_verified now scores neutral 5.0 (was 0.0).
    # We don't punish skills that haven't been verified yet.
    assert scorer._freshness_score(None, datetime.now(timezone.utc)) == 5.0


def test_description_score_with_outcome_verb():
    desc = "Generates a PDF report from GA4 and Stripe data, branded and white-label-ready, replacing 4h/week of manual work."
    assert scorer._description_score(desc) == 10.0


def test_description_score_long_without_verb():
    desc = (
        "An interesting tool that helps users do something useful with their "
        "data — useful for many scenarios in modern software development."
    )
    # v2: "An" is treated as a leading article — we look past it for an outcome
    # verb in words 2-5. None found ("interesting", "tool", "that", "helps")
    # so it falls into the "article + no outcome verb in next 4 words" bucket:
    # 6.0 if ≥100 chars (encourages a description but still below outcome-led 10.0).
    assert scorer._description_score(desc) == 6.0


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
    # v2: 1 entry = 4.0 (bumped from 3.0 — token attempt is worth something)
    assert scorer._unhappy_paths_score(rm) == 4.0


def test_unhappy_paths_score_nine_for_three_substantial_entries():
    """v2: 3 entries with ≥80-char avg → 9.0 (new bucket between 7 and 10)."""
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
    # v2: this hits the n≥3, avg≥80c bucket → 9.0 (was 7.0 in v1).
    # Each entry above is ~95-100 chars (condition + recovery).
    assert scorer._unhappy_paths_score(rm) == 9.0


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


def test_unhappy_paths_score_three_entries_too_short_falls_to_four():
    """3 entries but avg text too short to clear the 50c bar — drops to n≥1 bucket = 4.0."""
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
    # v2: n≥1 bucket bumped from 3.0 → 4.0
    assert scorer._unhappy_paths_score(rm) == 4.0


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
    # Only 1 valid entry survives → n≥1 bucket = 4.0 (was 3.0 in v1)
    assert scorer._unhappy_paths_score(rm) == 4.0


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


# -----------------------------------------------------------------------------
# v2 anchor calibration — Adam-named reference skills must score ≥8.0.
# These tests are the durable signal that any future scorer tweak hasn't
# regressed the calibration that landed 2026-05-17.
# -----------------------------------------------------------------------------


def _mk_anchor_skill(slug, install, has_outcome_verb=True, n_unhappy=3,
                      uhappy_avg=80, fresh_days=0):
    """Build the input args compute_score expects for an anchor-skill test."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Fake a description that starts with an outcome verb (well over 100c)
    if has_outcome_verb:
        desc = f"Generates {slug} outputs that save the operator at least four hours per week by automating the manual workflow end to end."
    else:
        desc = f"A {slug} skill that does the thing — runs as a CLI and produces output."
    # Build a synthetic readme with n_unhappy entries each with ~uhappy_avg chars
    base = "x" * max(20, uhappy_avg // 2)
    entries = "\n".join(
        f"  - condition: {base} cond {i} {base}\n    recovery: {base} reco {i} {base}"
        for i in range(n_unhappy)
    )
    readme = f"---\nunhappy_paths:\n{entries}\n---\nbody"
    last_verified = now - timedelta(days=fresh_days)
    # Created long ago so age cap doesn't kick in
    created = now - timedelta(days=60)
    return dict(
        install_count=install, last_verified=last_verified,
        description=desc, created_at=created,
        all_install_counts=[install, 0, 0, 5, 10],
        now=now, readme=readme,
    )


def test_v2_anchor_well_documented_skill_with_some_installs_scores_at_least_8():
    """A skill with outcome-led desc, 3 substantial unhappy_paths, and some
    installs should score ≥8.0 under v2. This is the floor that Adam-named
    references (larry/chef/plan-for-goal/ruthless-mentor/brainstorming) hit."""
    from scripts.quality_1705_compute_quality_score import compute_score
    args = _mk_anchor_skill("chef", install=2)
    assert compute_score(**args) >= 8.0


def test_v2_anchor_zero_install_well_documented_skill_scores_at_least_8():
    """The critical fix in v2: a brand-new, zero-install skill that IS well
    documented (outcome desc + 3 unhappy_paths) must NOT be penalised for
    being new. This was the v1 bug — zero installs dropped 2 points off
    every new skill, making 8.0+ unreachable until adoption proved itself."""
    from scripts.quality_1705_compute_quality_score import compute_score
    args = _mk_anchor_skill("plan-for-goal", install=0)
    assert compute_score(**args) >= 8.0, (
        "Zero-install but well-documented skill should clear 8.0 — "
        "v2's job is to stop penalising newness."
    )


def test_v2_anchor_thin_description_skill_scores_below_8():
    """Reverse check: skills with thin/non-outcome descriptions stay below 8.0
    even with good unhappy_paths. The formula correctly surfaces content debt."""
    from scripts.quality_1705_compute_quality_score import compute_score
    args = _mk_anchor_skill("data-pipeline", install=0, has_outcome_verb=False)
    # has_outcome_verb=False produces "A data-pipeline skill that does the thing..."
    # which is short (<100c) AND article-led → desc_s should be modest
    assert compute_score(**args) < 8.0, (
        "A skill with weak description should NOT reach the 8.0 anchor "
        "threshold — formula must still discriminate content quality."
    )


def test_v2_install_weight_is_only_10_percent():
    """Confirm the install signal has been demoted to weight 0.10 (was 0.20).
    This is the structural fix that lets new skills clear 8.0."""
    from scripts.quality_1705_compute_quality_score import compute_score
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    base_kwargs = dict(
        last_verified=now,
        description="Generates a real outcome with substantial description text "
                   "for over 100 characters of useful explanation here.",
        created_at=now - timedelta(days=60),
        all_install_counts=[0, 0, 100],
        now=now,
        readme="---\nunhappy_paths:\n"
               "  - condition: " + "x"*80 + "\n    recovery: " + "y"*80 + "\n"
               "  - condition: " + "x"*80 + "\n    recovery: " + "y"*80 + "\n"
               "  - condition: " + "x"*80 + "\n    recovery: " + "y"*80 + "\n"
               "---\nbody",
    )
    # Same skill, install=0 vs install=100 — score diff should be ≤ 0.5 (i.e.
    # the install signal accounts for at most ~0.5 points on the 10-point scale)
    s0 = compute_score(install_count=0, **base_kwargs)
    s100 = compute_score(install_count=100, **base_kwargs)
    delta = s100 - s0
    assert delta <= 0.5, (
        f"install signal too heavy: scoring diff between 0 and 100 installs "
        f"= {delta:.2f}. v2 demoted install to weight 0.10; signal range "
        f"5.0 (neutral) → 10.0 (top) is 5 points × 0.10 = max 0.5 score diff."
    )
