"""Tests for marketing bullet placeholder interpolation.

Regression guard for the stale "45 today" Pro-tier bullet (2026-05-28):
the bullet text in config/recipes-marketing.yaml is now a {pro_skills}
placeholder, interpolated by app.marketing_routes.marketing_snapshot against
the LIVE DB counts. This pins three contracts:

1. {pro_skills} in a tier bullet is replaced with the live Pro skill count.
2. An unknown {token} in copy is left verbatim (never raises KeyError).
3. The number rendered always equals counts.pro_skills (drift-proof).
"""
from __future__ import annotations

from app.marketing_routes import _SafeCountDict, marketing_snapshot
from tests.conftest import make_skill


def test_safe_count_dict_leaves_unknown_tokens_verbatim() -> None:
    fmt = _SafeCountDict({"pro_skills": 52})
    assert "{pro_skills} live, {mystery} kept".format_map(fmt) == "52 live, {mystery} kept"


def test_pro_bullet_interpolates_live_count(db_session) -> None:
    # Seed: 3 paid (cook→Pro) skills + 1 free. Live Pro count must be 3.
    make_skill(db_session, slug="paid-1", tier="cook")
    make_skill(db_session, slug="paid-2", tier="cook")
    make_skill(db_session, slug="paid-3", tier="cook")
    make_skill(db_session, slug="free-1", tier="free")
    db_session.flush()

    snap = marketing_snapshot(db_session)

    pro_count = snap["counts"]["pro_skills"]
    assert pro_count == 3

    pro_bullets = snap["tiers"]["pro"]["bullets"]
    catalog_bullet = next(b for b in pro_bullets if "paid skill in the catalog" in b)

    # The live count is rendered, and no placeholder brace survives.
    assert f"{pro_count} today" in catalog_bullet
    assert "{pro_skills}" not in catalog_bullet
    assert "{" not in catalog_bullet


def test_bullet_count_tracks_db_no_drift(db_session) -> None:
    """The rendered bullet number == counts.pro_skills, whatever it is."""
    for i in range(7):
        make_skill(db_session, slug=f"paid-{i}", tier="cook")
    db_session.flush()

    snap = marketing_snapshot(db_session)
    pro_count = snap["counts"]["pro_skills"]
    catalog_bullet = next(
        b for b in snap["tiers"]["pro"]["bullets"] if "paid skill in the catalog" in b
    )
    assert f"{pro_count} today" in catalog_bullet
