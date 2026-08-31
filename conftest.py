"""Root-level conftest.py — runs before ANY test module is imported.

Sets WR_DATABASE_URL to sqlite so that:
  - the global `settings = Settings()` in app/config.py does not trigger the
    production-secrets RuntimeError (secfix_1905 Issue #1 gate)
  - the in-memory SQLite test engine in tests/conftest.py continues to work

This must live at the repo root (not inside tests/) so it is executed BEFORE
pytest begins collecting or importing test modules.
"""

import platform
import os

import pytest

# Must be set before any app.* import so Settings() picks it up.
os.environ.setdefault("WR_DATABASE_URL", "sqlite:///./test_dev.db")
# COOKIES_SECURE defaults to True; in sqlite test env we allow False.
os.environ.setdefault("WR_COOKIES_SECURE", "false")

# Stripe price IDs for the test environment. config/tiers.yaml maps the
# `pro` / `pro_plus` tiers to WR_STRIPE_PRICE_PRO / WR_STRIPE_PRICE_PRO_PLUS;
# subscription_service._load_tier_price_ids() reads these at import time and
# builds TIER_PRICE_IDS. Without them TIER_PRICE_IDS is empty {}, and every
# checkout / tier test fails with `invalid_tier:... Valid: []`.
# These are dummy IDs — no test ever calls the real Stripe API (all Stripe
# calls are patched). Tests that exercise the canonical/legacy env-var
# fallback (TestEnvVarRenameLegacyFallback) override settings directly via
# _reload_with_settings, so they remain independent of these defaults.
os.environ.setdefault("WR_STRIPE_PRICE_PRO", "price_test_pro")
os.environ.setdefault("WR_STRIPE_PRICE_PRO_PLUS", "price_test_pro_plus")


def pytest_collection_modifyitems(config, items):
    """Skip sandbox_linux_only tests on macOS (darwin).

    The sandbox depends on firejail / bubblewrap, which are Linux-only tools.
    Running sandbox tests on macOS would either silently pass-through (lying
    about test coverage) or raise SandboxBackendUnavailable.  Skip them with
    an explicit reason so CI stays green on macOS dev machines without hiding
    the gap.
    """
    if platform.system().lower() != "darwin":
        return  # Linux (and other platforms) run the tests normally

    skip_marker = pytest.mark.skip(
        reason="sandbox_linux_only: firejail/bwrap are Linux-only; sandbox tests do not run on macOS"
    )
    for item in items:
        if item.get_closest_marker("sandbox_linux_only"):
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _isolate_settings_singleton():
    """Reset the ``app.config`` Settings lru_cache singleton around every test.

    Issue #298: ``app.config._get_settings_cached`` is an
    ``lru_cache(maxsize=1)``. Whichever test first touches
    ``app.config.settings`` (directly, or transitively via a lazy import
    such as ``install_integrity.internal_network_ips``) constructs and
    caches it under THAT test's env. Any later test that mutates
    ``WR_DATABASE_URL`` / ``WR_COOKIES_SECURE`` via ``monkeypatch.setenv``
    or ``patch.dict`` then silently observes the stale cached instance
    instead of one built from its own env — an inconsistent DB-URL /
    COOKIES_SECURE pair can trip the issue-#11 production gate on tests
    that never touched that env var themselves. This was flaky and
    shard-order-dependent under ``pytest -n auto --dist loadfile``
    (test_secfix_1905_d_search_skills_n_plus_1.py on the postgres CI leg).

    Clearing the cache before AND after each test guarantees every test
    (and the next one) constructs its own Settings from its own env,
    regardless of xdist worker/shard distribution or execution order.
    """
    from app.config import _get_settings_cached

    _get_settings_cached.cache_clear()
    yield
    _get_settings_cached.cache_clear()


@pytest.fixture(autouse=True)
def _block_outbound_network(request, monkeypatch):
    """Fail fast on any non-loopback network call made from a test.

    See tests/net_guard.py for the full incident write-up. Short version: the
    ``/api/skills/external`` tests were silently reaching clawhub.ai once per
    result row, so when that upstream slowed down the CI job hung for hours
    instead of failing. A hermetic suite cannot depend on third-party uptime.

    Loopback is still allowed -- some tests bind a real uvicorn server on
    127.0.0.1. Opt out of the guard with ``@pytest.mark.network``.
    """
    if request.node.get_closest_marker("network"):
        return
    from tests import net_guard

    net_guard.install(monkeypatch)
