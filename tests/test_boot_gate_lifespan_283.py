"""Issue #283 — boot gate fires on CONSTRUCTION/SERVE, never on import.

Guarantees:
1. Importing app modules bare (no env stubs) succeeds side-effect free, and
   the lazy ``settings`` singleton is NOT constructed by the import itself.
2. Serving the app in production mode with default change-me secrets fails
   loudly: the lifespan hook runs ``run_production_boot_checks()`` which
   constructs Settings (gate lives in Settings.__init__ — secfix_1905
   contract) and lets the RuntimeError propagate out of startup.
"""

import asyncio
import os
import sys

import pytest


def test_bare_import_succeeds_without_env_stubs(monkeypatch):
    """Importing app.config + a leaf service module with NO env stubs must not
    raise, and must not construct the settings singleton (issue #283)."""
    for var in list(os.environ):
        if var.startswith("WR_"):
            monkeypatch.delenv(var, raising=False)

    import importlib

    import app.config as config

    config = importlib.reload(config)
    # Import a transitive consumer of app.database -> app.config (the exact
    # reproduction from the issue body).
    from app.services.external_install_resolver import validate_external_slug  # noqa: F401

    # PEP 562 __getattr__ only fires on cache miss: if import had constructed
    # the singleton, 'settings' would be a real entry in __dict__.
    assert "settings" not in vars(config), "settings singleton was constructed at import time"
    # The lazy accessors exist.
    assert callable(config.get_settings)
    assert callable(config.run_production_boot_checks)


def test_gate_fires_on_serve_in_prod_mode_with_bad_secrets(monkeypatch):
    """Lifespan must raise loudly when serving with default secrets in prod.

    The gate lives in Settings.__init__ (secfix_1905); the lifespan calls
    run_production_boot_checks() which constructs Settings on first use.
    With a prod DATABASE_URL and change-me secrets, startup must abort.
    """
    import app.config as config
    import app.main as main

    monkeypatch.setenv("WR_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "true")
    # Drop any ambient WR_* secrets so the gate sees change-me defaults.
    for var in ("WR_API_KEY", "WR_SIGNING_SECRET", "WR_JWT_SECRET", "WR_HEARTBEAT_PEPPER"):
        monkeypatch.delenv(var, raising=False)
    # Reset the lazy cache so run_production_boot_checks() constructs fresh
    # Settings under the patched env (conftest may have cached a sqlite one).
    config._get_settings_cached.cache_clear()

    from fastapi import FastAPI

    app = FastAPI(lifespan=main.lifespan)

    async def _serve():
        async with app.router.lifespan_context(app):
            pass

    try:
        asyncio.run(_serve())
    except RuntimeError:
        pass
    else:
        pytest.fail("lifespan did not raise RuntimeError with default secrets")
    finally:
        config._get_settings_cached.cache_clear()
        # Drop stale imported modules so later tests re-import cleanly.
        sys.modules.pop("app.main", None)
