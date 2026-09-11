"""Issue #298 — Settings() singleton (`app.config._get_settings_cached`, an
lru_cache(maxsize=1)) must not leak its cached instance across tests.

If test A constructs the singleton under one WR_* env and test B runs under
a DIFFERENT WR_* env, without a per-test isolation fixture, B silently
observes A's stale cached Settings object instead of one built from its own
env. This is the exact mechanism behind the flaky postgres-leg failure in
test_secfix_1905_d_search_skills_n_plus_1.py: whichever test in the xdist
worker happens to touch `app.config.settings` first via a lazy import
(e.g. install_integrity.internal_network_ips) wins, and the DB-URL/
COOKIES_SECURE pair observed by a later test can be internally
inconsistent, tripping the issue-#11 production gate.

These two tests MUST run in this order (module top-to-bottom, no
randomization) to reproduce/prove the fix:
  - test_a_poison_cache_with_insecure_sqlite: builds + caches the singleton
    under sqlite + COOKIES_SECURE=false.
  - test_b_next_test_must_see_its_own_env: sets a DIFFERENT
    (postgres + COOKIES_SECURE=true) env and expects `app.config.settings`
    to reflect ITS OWN env, not test A's leftover cached instance.

RED-PROOF: on main (no isolation fixture applied to this module), test_b_*
fails because `config.settings` is still test_a's cached sqlite/insecure
instance — proving the leak. After the fix (this module opts into the
`settings_isolation` marker, which conftest.py's `_isolate_settings_singleton`
fixture uses to scope `_get_settings_cached.cache_clear()` to only marked
modules — NOT a repo-wide autouse fixture; see conftest.py and issue #298/
#299 review history for why repo-wide breaks 28 unrelated tests), both
tests pass independent of run order.
"""

from __future__ import annotations

import pytest

from app import config

pytestmark = pytest.mark.settings_isolation


def test_a_poison_cache_with_insecure_sqlite(monkeypatch):
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    s = config.settings
    assert not s.COOKIES_SECURE
    assert "sqlite" in s.DATABASE_URL


def test_b_next_test_must_see_its_own_env(monkeypatch):
    monkeypatch.setenv("WR_DATABASE_URL", "postgresql://wisechef@localhost/wiserecipes_test")
    monkeypatch.setenv("WR_COOKIES_SECURE", "true")
    monkeypatch.setenv("WR_API_KEY", "rec_prod_test_key_1234567890abcdef")
    monkeypatch.setenv("WR_SIGNING_SECRET", "wr-tarball-signing-prod-safe-value")
    monkeypatch.setenv("WR_JWT_SECRET", "wr-jwt-prod-safe-value-12345678")
    monkeypatch.setenv("WR_HEARTBEAT_PEPPER", "wr-fleet-pepper-prod-safe-value")
    monkeypatch.setenv("WR_OAUTH_REDIRECT_BASE", "https://recipes.wisechef.ai")
    monkeypatch.setenv("WR_SERVER_PUBLIC_IP", "203.0.113.10")
    monkeypatch.setenv("WR_STRIPE_PRICE_PRO", "price_test_pro")
    s = config.settings
    assert s.COOKIES_SECURE, (
        "app.config.settings leaked test_a's cached (insecure) Settings "
        "instance into this test — the lru_cache(1) singleton was not "
        "reset between tests (issue #298)."
    )
    assert "postgresql" in s.DATABASE_URL
