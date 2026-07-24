"""v7 phase F — taxonomy migration tests.

Verifies the migration logic in alembic/versions/b3c4d5e6f701_v7_phase_f_taxonomy.py
without bringing up alembic — we exercise the same UPDATE statements against a
fresh SQLite DB so the assertions are about behavior, not framework.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Skill, User


def _load_migration():
    """`alembic/versions/` is a directory of scripts, not a package. Load the
    migration module directly so we can reuse its CATEGORY_MAP."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).parent.parent
        / "alembic" / "versions" / "b3c4d5e6f701_v7_phase_f_taxonomy.py"
    )
    spec = importlib.util.spec_from_file_location("v7_phase_f_taxonomy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mig = _load_migration()
CATEGORY_MAP = _mig.CATEGORY_MAP
CANONICAL_CATEGORIES = _mig.CANONICAL_CATEGORIES


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _run_upgrade(engine):
    """Replay the migration's UPDATE statements via raw SQL.

    Mirrors b3c4d5e6f701_v7_phase_f_taxonomy.upgrade(); kept in lockstep so
    the test exercises the actual production logic, not a paraphrase.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE skills SET tier='operator' WHERE tier='studio'"))
        conn.execute(text(
            "UPDATE users SET subscription_tier='operator' "
            "WHERE subscription_tier='studio'"
        ))
        for old, new in CATEGORY_MAP.items():
            conn.execute(
                text("UPDATE skills SET category=:n WHERE category=:o"),
                {"n": new, "o": old},
            )
        canonical_list = ", ".join(f"'{c}'" for c in sorted(CANONICAL_CATEGORIES))
        conn.execute(text(
            f"UPDATE skills SET category='productivity' "
            f"WHERE category IS NOT NULL AND category NOT IN ({canonical_list})"
        ))


def _mk(session, **kw):
    s = Skill(id=uuid4(), slug=f"slug-{uuid4().hex[:8]}", title="t", **kw)
    session.add(s)
    session.commit()
    return s


def test_studio_tier_is_aliased_to_operator(engine, session):
    _mk(session, tier="studio")
    _mk(session, tier="cook")
    _mk(session, tier="free")
    _mk(session, tier="operator")

    _run_upgrade(engine)

    session.expire_all()
    tiers = {row.tier for row in session.query(Skill).all()}
    assert "studio" not in tiers
    assert tiers == {"operator", "cook", "free"}


def test_user_subscription_tier_studio_is_aliased(engine, session):
    u = User(
        id=uuid4(),
        email="x@example.com",
        display_name="x",
        subscription_tier="studio",
    )
    session.add(u)
    session.commit()

    _run_upgrade(engine)

    session.expire_all()
    fetched = session.query(User).filter_by(email="x@example.com").one()
    assert fetched.subscription_tier == "operator"


def test_all_categories_land_in_canonical_set(engine, session):
    # Seed one skill per legacy category plus a junk fallback case.
    for legacy in CATEGORY_MAP.keys():
        _mk(session, category=legacy)
    _mk(session, category="some-bogus-future-bucket")
    _mk(session, category="research")  # already canonical
    _mk(session, category=None)        # NULL passes through

    _run_upgrade(engine)

    session.expire_all()
    seen = {row.category for row in session.query(Skill).all()}
    seen.discard(None)
    assert seen.issubset(CANONICAL_CATEGORIES), (
        f"Non-canonical categories survived: {seen - CANONICAL_CATEGORIES}"
    )


def test_mapping_table_matches_docs():
    """docs/taxonomy.md is the source of truth — assert its mapping rows are
    a subset of CATEGORY_MAP. If the doc lists a legacy → canonical row that
    the migration doesn't implement, fail loudly."""
    import re
    from pathlib import Path

    doc = Path(__file__).parent.parent / "docs" / "taxonomy.md"
    text_doc = doc.read_text()
    # Match table rows like "| `devops` | `ops` | ..."
    pattern = re.compile(r"\|\s*`([a-z0-9\-]+)`\s*\|\s*`([a-z0-9\-]+)`\s*\|")
    pairs = pattern.findall(text_doc)
    # The first table is tiers (free/cook/operator), so filter to category-mappable rows.
    tier_values = {"free", "cook", "operator", "studio"}
    mapping_rows = [(o, n) for o, n in pairs if o not in tier_values
                    and n not in tier_values]

    for old, new in mapping_rows:
        if old in CANONICAL_CATEGORIES:
            # Identity rows like ("content","content") may appear; skip.
            continue
        assert old in CATEGORY_MAP, f"docs lists `{old}` but migration doesn't map it"
        assert CATEGORY_MAP[old] == new, (
            f"docs maps {old}→{new} but migration maps {old}→{CATEGORY_MAP[old]}"
        )
