"""LoopSkill standalone: install-URL public origin must default to the
LoopSkill brand, be overridable via env, and route through ONE seam.

Regression guard for the brand-default bug: before this, 6 install-URL
builders independently fell back to ``https://recipes.wisechef.ai`` when no
env was set, so an operator self-hosting LoopSkill and pointing their own
agents at it would be handed install URLs for a *different, old-branded*
domain. This is the single highest-criticality defect for the multi-agent
self-host deploy story.
"""

import importlib
import os

import pytest


def _reload_config():
    """Reload app.config so a freshly-set env var is picked up by Settings."""
    import app.config

    importlib.reload(app.config)
    return app.config


@pytest.fixture
def clean_origin_env(monkeypatch):
    """Strip every public-origin env var so we test the true default."""
    for var in (
        "WR_PUBLIC_ORIGIN",
        "LOOPSKILL_PUBLIC_ORIGIN",
        "RECIPES_PUBLIC_ORIGIN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    _reload_config()


def test_public_origin_default_is_loopskill_not_old_brand(clean_origin_env):
    """With no env set, the default origin must be the LoopSkill brand,
    never the old recipes.wisechef.ai domain."""
    cfg = _reload_config()
    origin = cfg.public_origin()
    assert origin == "https://loopskill.io"
    assert "recipes.wisechef.ai" not in origin


def test_public_origin_primary_env_is_loopskill(clean_origin_env, monkeypatch):
    """LOOPSKILL_PUBLIC_ORIGIN is the primary override."""
    monkeypatch.setenv("LOOPSKILL_PUBLIC_ORIGIN", "https://skills.example.com")
    cfg = _reload_config()
    assert cfg.public_origin() == "https://skills.example.com"


def test_public_origin_wr_field_takes_precedence(clean_origin_env, monkeypatch):
    """The WR_-prefixed Settings field is the highest-priority source
    (matches the repo's pydantic-settings env_prefix convention)."""
    monkeypatch.setenv("WR_PUBLIC_ORIGIN", "https://primary.example.com")
    monkeypatch.setenv("LOOPSKILL_PUBLIC_ORIGIN", "https://secondary.example.com")
    cfg = _reload_config()
    assert cfg.public_origin() == "https://primary.example.com"


def test_public_origin_recipes_env_still_honored_as_compat(clean_origin_env, monkeypatch):
    """RECIPES_PUBLIC_ORIGIN stays readable as a backward-compat fallback so
    an existing deployment that set the old env var keeps working."""
    monkeypatch.setenv("RECIPES_PUBLIC_ORIGIN", "https://legacy.example.com")
    cfg = _reload_config()
    assert cfg.public_origin() == "https://legacy.example.com"


def test_public_origin_strips_trailing_slash(clean_origin_env, monkeypatch):
    monkeypatch.setenv("LOOPSKILL_PUBLIC_ORIGIN", "https://skills.example.com/")
    cfg = _reload_config()
    assert cfg.public_origin() == "https://skills.example.com"


def test_no_builder_falls_back_to_old_brand_default():
    """Structural guard: no install-URL builder may carry its own hardcoded
    recipes.wisechef.ai fallback string. They must all route through
    config.public_origin()."""
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # A builder fallback looks like: or "https://recipes.wisechef.ai"
        if 'or "https://recipes.wisechef.ai"' in text:
            offenders.append(str(py.relative_to(app_dir.parent)))
    assert offenders == [], (
        "These files still hardcode the old-brand install-URL fallback "
        "instead of calling config.public_origin(): " + ", ".join(offenders)
    )
