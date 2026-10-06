"""Tests for marketing bullet placeholder interpolation.

Regression guard for stale numbers in tier bullets. The bullet text in
config/recipes-marketing.yaml carries {placeholders}, interpolated by
app.marketing_routes.marketing_snapshot against LIVE values. Pins:

1. An unknown {token} in copy is left verbatim (never raises KeyError).
2. The Pro bundle bullet renders the live private-bundle caps from
   config/tiers.yaml, and no placeholder brace survives.
3. The rendered number always equals counts.pro_private_bundles (drift-proof).

History: until claimgate_1006 the Pro card advertised "Every paid skill in the
catalog ({pro_skills} today)" and "Up to {pro_cookbooks} cookbooks". The
catalog has no paid skills and cookbooks are bundles now, so both bullets were
replaced with the live /pricing card's wording; the interpolation contract is
pinned against the bundle bullet instead.
"""

from __future__ import annotations

from app.marketing_routes import _SafeCountDict, marketing_snapshot
from app.tier_labels import bundle_limit


def test_safe_count_dict_leaves_unknown_tokens_verbatim() -> None:
    fmt = _SafeCountDict({"pro_skills": 52})
    assert "{pro_skills} live, {mystery} kept".format_map(fmt) == "52 live, {mystery} kept"


def _bundle_bullet(snap: dict) -> str:
    return next(b for b in snap["tiers"]["pro"]["bullets"] if "private bundles" in b)


def test_pro_bullet_interpolates_live_bundle_caps(db_session) -> None:
    snap = marketing_snapshot(db_session)
    bullet = _bundle_bullet(snap)
    assert bullet.startswith(f"{bundle_limit('pro')} private bundles")
    assert f"Free gives you {bundle_limit('free')}" in bullet
    assert "{" not in bullet


def test_bullet_count_tracks_snapshot_counts_no_drift(db_session) -> None:
    snap = marketing_snapshot(db_session)
    assert snap["counts"]["pro_private_bundles"] == bundle_limit("pro")
    assert snap["counts"]["free_private_bundles"] == bundle_limit("free")
    assert _bundle_bullet(snap).startswith(f"{snap['counts']['pro_private_bundles']} ")


def test_retired_catalog_bullets_are_gone(db_session) -> None:
    """The two bullets replaced in claimgate_1006 must not come back."""
    bullets = " ".join(marketing_snapshot(db_session)["tiers"]["pro"]["bullets"])
    assert "paid skill in the catalog" not in bullets
    assert "cookbook" not in bullets.lower()
    assert "pro_plus_cookbooks" not in marketing_snapshot(db_session)["counts"]
