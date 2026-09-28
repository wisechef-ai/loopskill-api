"""pricing0928 (t_7f5808d2, option D): a signed-in Free key is never capped below anonymous.

Before this fix ``TIER_INSTALL_LIMITS`` read ``None: 5, "free": 5``, but the
anonymous 5 never applied: ``_count_today_installs`` counts per
``api_key_id`` and returns 0 for a caller without one. Result: anonymous
callers were unlimited while a Free key got a 429 ("Upgrade to Pro+") on its
6th install of the day. /pricing promises that every install is identical on
Free and Pro, and that Free is not a trial.

These tests exercise the REAL route through ``APIKeyMiddleware`` with a
seeded install history, so they fail if either the table or the counting
logic re-creates the asymmetry.
"""

from __future__ import annotations

import pytest

from app.access_routes import TIER_INSTALL_LIMITS, TIER_RANK
from tests.test_tier_semantics_flip import (  # noqa: F401 — fixtures re-exported
    _make_install_event,
    _make_skill,
    _make_user_with_key,
    db,
    engine_fixture,
    patched_client,
)


def _effective(limit: int | None) -> float:
    return float("inf") if limit is None else float(limit)


def test_no_tier_has_a_lower_install_limit_than_anonymous():
    anon = _effective(TIER_INSTALL_LIMITS[None])
    for tier in TIER_RANK:
        assert _effective(TIER_INSTALL_LIMITS.get(tier, 5)) >= anon, (
            f"tier {tier!r} install limit {TIER_INSTALL_LIMITS.get(tier)} is below "
            f"anonymous {TIER_INSTALL_LIMITS[None]}"
        )


def test_install_limit_is_monotonic_in_tier_rank():
    """A higher tier never gets fewer installs than a lower one."""
    ordered = sorted(TIER_RANK.items(), key=lambda kv: kv[1])
    for (lo, lo_rank), (hi, hi_rank) in zip(ordered, ordered[1:]):
        if hi_rank > lo_rank:
            assert _effective(TIER_INSTALL_LIMITS.get(hi, 5)) >= _effective(TIER_INSTALL_LIMITS.get(lo, 5)), (
                f"{hi!r} (rank {hi_rank}) is capped below {lo!r} (rank {lo_rank})"
            )


@pytest.mark.parametrize("prior_installs_today", [5, 25])
def test_free_key_installs_whenever_anonymous_does(patched_client, db, prior_installs_today):  # noqa: F811
    """Same history, same skill: if anonymous gets a 200, the Free key does too."""
    skills = [
        _make_skill(db, slug=f"par-{prior_installs_today}-{i}", title=f"P{i}", tier="free")
        for i in range(prior_installs_today + 1)
    ]
    free_key, ak_id = _make_user_with_key(db, tier="free")
    for i in range(prior_installs_today):
        _make_install_event(db, skills[i].id, api_key_id=ak_id)
    db.commit()
    target = skills[-1].slug

    anon = patched_client.get(f"/api/skills/install?slug={target}")
    keyed = patched_client.get(f"/api/skills/install?slug={target}", headers={"x-api-key": free_key})

    assert anon.status_code == 200, anon.text
    assert keyed.status_code == 200, (
        f"Free key capped below anonymous after {prior_installs_today} installs: "
        f"{keyed.status_code} {keyed.text}"
    )
    assert "Upgrade" not in keyed.text
