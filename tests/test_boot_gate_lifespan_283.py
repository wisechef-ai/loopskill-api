"""Issue #283 — boot gate runs at SERVE time, not import time.

Two guarantees:
1. Importing the app bare (no env stubs at all) must succeed side-effect free.
2. Serving the app in production mode with default change-me secrets must
   fail loudly via the FastAPI lifespan hook.
"""

import asyncio
import os

import pytest


def test_bare_import_succeeds_without_env_stubs(monkeypatch):
    """Importing app.main with NO env stubs must not raise (issue #283)."""
    # Ensure no production-style env overrides are set — bare environment.
    for var in list(os.environ):
        if var.startswith("WR_"):
            monkeypatch.delenv(var, raising=False)
    import importlib

    import app.config as config
    import app.main as main

    importlib.reload(config)
    importlib.reload(main)
    # Gate function exists and is callable but was NOT run at import time.
    assert callable(config.run_production_boot_checks)


def test_gate_fires_on_serve_in_prod_mode_with_bad_secrets(monkeypatch):
    """Lifespan must raise loudly when serving with default secrets in prod."""
    import app.config as config
    import app.main as main

    # Prod mode: postgres DB + secure cookies, secrets left at change-me
    # defaults. Rebind the singleton the lifespan reads (conftest pins the
    # real one to a sqlite test DB, which the gate exempts).
    monkeypatch.setenv("WR_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "true")
    monkeypatch.setattr(config, "settings", config.Settings())

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
