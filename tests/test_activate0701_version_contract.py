"""Phase 0 (loopskill_activate_0701): single-sourced version contract.

RED-first tests: the app version must come from ``app.version.__version__``
everywhere the APP version is surfaced (FastAPI metadata, ``/`` root,
``/healthz``, ``/api/healthz``), must exceed the seed-fixture era 0.5.0, and
no module may carry its own hardcoded APP-version literal that can drift.

The conftest ``client`` fixture builds a minimal router-subset app, so these
tests assemble their own app from the real meta/health routers.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.version import __version__

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# Version literals that are NOT the app version (distinct versioned concepts):
#   skill_quality_gate.py — the quality-gate SCHEMA version
#   mcp/server.py         — the MCP protocol SERVER_VERSION handshake value
_DISTINCT_VERSION_CONCEPTS = {"skill_quality_gate.py", "server.py"}


def _meta_client() -> TestClient:
    """App with the real meta/health surface (no DB dependency needed)."""
    from app.health_routes import router as health_router

    app = FastAPI(version=__version__)
    app.include_router(health_router)

    @app.get("/")
    def root() -> dict[str, str]:
        # Mirrors app.main.create_app's root route, which reads __version__.
        return {"name": "WiseRecipes API", "version": __version__, "docs": "/docs"}

    return TestClient(app)


def _semver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def test_version_constant_is_semver_and_past_seed_era() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert _semver_tuple(__version__) > (0, 5, 0)


def test_health_router_reports_the_constant() -> None:
    client = _meta_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


def test_main_module_surfaces_use_the_constant() -> None:
    """main.py's FastAPI(version=...) and root route must read __version__."""
    src = (APP_DIR / "main.py").read_text()
    assert "from app.version import __version__" in src
    assert 'version="0.5.0"' not in src
    assert re.search(r"version=__version__", src)
    assert re.search(r'"version":\s*__version__', src)


def test_health_and_core_routes_import_the_constant() -> None:
    src = (APP_DIR / "health_routes.py").read_text()
    assert "from app.version import __version__ as VERSION" in src
    assert 'VERSION = "0.5.0"' not in src
    # core_routes.py keeps VERSION as a backward-compat re-export of the
    # constant (app/routes.py + several test modules import it from there).
    core_src = (APP_DIR / "core_routes.py").read_text()
    assert "from app.version import __version__ as VERSION" in core_src
    assert 'VERSION = "0.5.0"' not in core_src


def test_no_hardcoded_app_version_literals_outside_version_py() -> None:
    """No module besides app/version.py may define its own APP version string."""
    offenders: list[str] = []
    pattern = re.compile(r"""(?:VERSION\s*=|version=)\s*["']\d+\.\d+\.\d+["']""")
    for py in APP_DIR.rglob("*.py"):
        if py.name == "version.py" or py.name in _DISTINCT_VERSION_CONCEPTS:
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.relative_to(APP_DIR.parent)}:{i}: {line.strip()}")
    assert not offenders, "hardcoded version literals (use app.version.__version__):\n" + "\n".join(
        offenders
    )
