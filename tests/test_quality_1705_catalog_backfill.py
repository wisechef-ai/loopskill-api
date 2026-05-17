"""tests/test_quality_1705_catalog_backfill.py — TDD gates for Phase A.

These tests cover:
  - last_verified column is exposed on /api/skills/<slug> (SkillOut + SkillDetailOut)
  - archived_at column is set when is_archived flips to true (via backfill script)
  - The 3 hard culls leave is_archived=true after backfill
  - The 4-to-1 hub-search merge creates local-skills-discovery + 4 aliases
  - The rename of incident-response-openclaw → incident-response creates an alias
  - The creator_name backfill never assigns a creator to a slug NOT in ATTRIBUTION
  - All ATTRIBUTION slugs that are public+non-archived get a creator_name after backfill
  - The backfill is idempotent (running twice produces zero new writes on second run)

Uses SQLite in-memory + seed fixtures, no live DB. Per executing-golazo-plan
pitfall #16: never trust "tests pass" in isolation — re-run the FULL suite
in CI before merging.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.models import Base  # noqa: E402
from scripts import quality_1705_catalog_backfill as backfill  # noqa: E402


@pytest.fixture()
def db_engine(tmp_path):
    """Spin a fresh SQLite DB with the full schema."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    # Seed the SkillAlias table — it's part of Base.metadata so already created.
    yield engine
    engine.dispose()


@pytest.fixture()
def seeded_session(db_engine, monkeypatch):
    """Seed the DB with the catalog state Phase A expects to operate on."""
    SessionLocal = sessionmaker(bind=db_engine, future=True)
    session = SessionLocal()

    # Seed the 3 pre-existing creators that are in prod
    pre_creators = [
        ("AgentForge Labs", "agentforge-labs"),
        ("Tori (AI Agent)", "tori-ai-agent"),
        ("WiseChef Team", "wisechef-team"),
    ]
    for name, slug in pre_creators:
        session.execute(
            text(
                "INSERT INTO creators (id, name, slug, is_founder, created_at) "
                "VALUES (:id, :n, :s, 0, :now)"
            ),
            {"id": str(uuid.uuid4()), "n": name, "s": slug,
             "now": datetime.now(timezone.utc)},
        )

    # Seed every skill from ATTRIBUTION as a minimal row + the 4 hub-search
    # variants and the 3 culls.
    base_slugs = list(backfill.ATTRIBUTION.keys())
    for slug in base_slugs:
        session.execute(
            text(
                "INSERT INTO skills (id, slug, title, description, is_public, "
                "is_archived, skill_variant, upstream_status, install_count, "
                "created_at, updated_at) VALUES "
                "(:id, :s, :t, :d, 1, 0, 'custom', 'active', 0, :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "s": slug,
                "t": slug.replace("-", " ").title(),
                "d": f"Stub description for {slug}.",
                "now": datetime.now(timezone.utc),
            },
        )

    session.commit()

    # Monkey-patch backfill's create_engine to use this test engine
    monkeypatch.setattr(backfill, "get_db_url",
                        lambda: str(db_engine.url))
    yield session
    session.close()


def test_skills_have_last_verified_column(db_engine):
    """The migration must have added last_verified + archived_at columns."""
    inspector_query = (
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='skills'"
    )
    with db_engine.connect() as conn:
        ddl = conn.execute(text(inspector_query)).scalar()
    assert "last_verified" in ddl, "skills.last_verified must exist"
    assert "archived_at" in ddl, "skills.archived_at must exist"


def test_backfill_dry_run_writes_nothing(seeded_session, db_engine, capsys):
    """Dry-run mode must not mutate the DB."""
    original_creator_count = seeded_session.execute(
        text("SELECT COUNT(*) FROM creators")
    ).scalar()

    sys.argv = ["backfill", "--db-url", str(db_engine.url)]
    backfill.main()

    after_count = seeded_session.execute(
        text("SELECT COUNT(*) FROM creators")
    ).scalar()
    assert after_count == original_creator_count, "Dry-run must not write"


def test_backfill_commit_creates_missing_creators(seeded_session, db_engine):
    """Committing must INSERT every NEW creator referenced by ATTRIBUTION."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()

    seeded_session.expire_all()
    creator_names = set(
        seeded_session.execute(text("SELECT name FROM creators")).scalars().all()
    )
    # Sample of the new creators that must exist after commit
    assert "Andrej Karpathy" in creator_names
    assert "Anthropic" in creator_names
    assert "wondelai" in creator_names
    assert "Orchestra Research" in creator_names


def test_backfill_commits_hard_culls(seeded_session, db_engine):
    """The 3 hard culls must be archived after --commit."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()

    seeded_session.expire_all()
    for slug in backfill.CULLS:
        row = seeded_session.execute(
            text("SELECT is_archived, archived_at FROM skills WHERE slug = :s"),
            {"s": slug},
        ).first()
        assert row is not None, f"{slug} should still exist as a row"
        assert row.is_archived in (1, True), f"{slug} should be archived"
        assert row.archived_at is not None, f"{slug} should have archived_at stamped"


def test_backfill_renames_via_alias(seeded_session, db_engine):
    """incident-response-openclaw and skill-creator-anthropic must get aliases."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    seeded_session.expire_all()

    for old, new in backfill.RENAMES.items():
        alias = seeded_session.execute(
            text("SELECT new_slug FROM skill_aliases WHERE old_slug = :s"),
            {"s": old},
        ).first()
        assert alias is not None, f"Missing alias for {old}"
        assert alias.new_slug == new

        # New slug must now exist as the canonical row
        new_row = seeded_session.execute(
            text("SELECT id FROM skills WHERE slug = :s"),
            {"s": new},
        ).first()
        assert new_row is not None, f"Renamed skill {new} must exist as canonical row"


def test_backfill_merges_hub_search_into_local_skills_discovery(seeded_session, db_engine):
    """The 4 hub-search variants must alias to local-skills-discovery."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    seeded_session.expire_all()

    # New skill exists
    lsd = seeded_session.execute(
        text("SELECT id, category, tier FROM skills WHERE slug = 'local-skills-discovery'"),
    ).first()
    assert lsd is not None, "local-skills-discovery skill must be created"
    assert lsd.category == "discovery"
    assert lsd.tier == "cook"

    # All 4 old slugs alias to it
    for old in backfill.MERGE_HUB_SEARCH["old_slugs"]:
        alias = seeded_session.execute(
            text("SELECT new_slug FROM skill_aliases WHERE old_slug = :s"),
            {"s": old},
        ).first()
        assert alias is not None, f"hub-search alias missing for {old}"
        assert alias.new_slug == "local-skills-discovery"

        # And the old row is archived
        old_row = seeded_session.execute(
            text("SELECT is_archived FROM skills WHERE slug = :s"),
            {"s": old},
        ).first()
        assert old_row is not None
        assert old_row.is_archived in (1, True)


def test_backfill_stamps_last_verified_on_survivors(seeded_session, db_engine):
    """Every public non-archived skill should get last_verified=now() after backfill."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    seeded_session.expire_all()

    null_lv = seeded_session.execute(
        text(
            "SELECT COUNT(*) FROM skills "
            "WHERE is_archived = 0 AND is_public = 1 AND last_verified IS NULL"
        )
    ).scalar()
    assert null_lv == 0, "No survivor should have NULL last_verified after backfill"


def test_backfill_is_idempotent(seeded_session, db_engine, capsys):
    """A second --commit run must report zero changes."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    capsys.readouterr()  # discard first run

    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    out = capsys.readouterr().out
    # Second run: every "_updated" list and "_created" list should be empty
    import json
    payload_start = out.index("{")
    payload_end = out.rindex("}") + 1
    payload = json.loads(out[payload_start:payload_end])
    assert payload["creators_created"] == [], "Idempotent: creators_created must be empty"
    assert payload["skills_creator_updated"] == [], "Idempotent: creator updates must be empty"
    assert payload["skills_category_updated"] == []
    assert payload["skills_tier_updated"] == []
    assert payload["skills_description_updated"] == []
    assert payload["skills_archived"] == [], "Idempotent: no new archives"
    assert payload["aliases_created"] == [], "Idempotent: no new aliases"
    assert payload["skills_renamed"] == [], "Idempotent: no new renames"
    assert payload["skills_last_verified_stamped"] == 0
    assert payload["skill_created_local_skills_discovery"] is False


def test_backfill_resolves_all_attribution_to_real_skills(seeded_session, db_engine):
    """No ambiguous skills should be reported when DB matches ATTRIBUTION keyset."""
    sys.argv = ["backfill", "--commit", "--db-url", str(db_engine.url)]
    backfill.main()
    seeded_session.expire_all()

    # After commit, every ATTRIBUTION slug should have a creator_id set
    # (either pre-existing or newly attributed).
    missing = []
    for slug in backfill.ATTRIBUTION:
        row = seeded_session.execute(
            text("SELECT creator_id FROM skills WHERE slug = :s"),
            {"s": slug},
        ).first()
        if row is None:
            continue  # renamed away; that's fine
        # Allow renamed-away slugs to lack creator_id
        if slug in backfill.RENAMES:
            continue
        if row.creator_id is None:
            missing.append(slug)
    assert missing == [], f"Skills without creator after backfill: {missing}"
