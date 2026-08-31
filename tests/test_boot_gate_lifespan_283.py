"""Issue #283 — boot gate fires on CONSTRUCTION/SERVE, never on import.

Guarantees:
1. Importing app modules bare (no env stubs) succeeds side-effect free, and
   the lazy ``settings`` singleton is NOT constructed by the import itself.
2. Serving the app in production mode with default change-me secrets fails
   loudly (lifespan -> run_production_boot_checks -> Settings.__init__ gate).

Test 2 runs in a SUBPROCESS: the gate test must construct Settings under a
poisoned prod environment, and the lru_cache'd singleton would otherwise leak
that construction into every later test in this pytest-xdist worker.
"""

import os
import subprocess
import sys

# Serve probe: import app.main, run the lifespan, expect RuntimeError.
# NOTE: app.main is the SERVE entry — its module scope wires middleware via
# get_settings(), so the gate may fire at import OR at lifespan. Both count.
PROBE = """
import asyncio, sys
try:
    from fastapi import FastAPI
    import app.main as main
    app = FastAPI(lifespan=main.lifespan)

    async def _serve():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_serve())
except RuntimeError as e:
    print("GATE_FIRED:", str(e)[:80])
    sys.exit(0)
print("NO GATE")
sys.exit(3)
"""


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
    assert callable(config.get_settings)
    assert callable(config.run_production_boot_checks)


def test_gate_fires_on_serve_in_prod_mode_with_bad_secrets(tmp_path):
    """Serving with default change-me secrets in prod mode must abort startup.

    Runs in a clean subprocess (hermetic env: no WR_* secrets, prod DB URL,
    secure cookies) so the poisoned construction can never leak into this
    worker's cached settings singleton.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "WR_DATABASE_URL": "postgresql://u:p@localhost/db",
        "WR_COOKIES_SECURE": "true",
        # No WR_API_KEY / SIGNING_SECRET / JWT_SECRET / HEARTBEAT_PEPPER:
        # the gate must see the change-me defaults and refuse.
    }
    r = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0 and "GATE_FIRED" in r.stdout, (
        f"gate did not fire on serve: rc={r.returncode} out={r.stdout[-200:]} err={r.stderr[-400:]}"
    )
    assert "change-me secret" in r.stdout, r.stdout
