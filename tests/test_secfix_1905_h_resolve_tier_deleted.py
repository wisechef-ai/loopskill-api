"""Issue #25 — _resolve_caller_tier deleted; auth_ctx.tier is source of truth.

Tests:
1. _resolve_caller_tier does NOT exist in _skill_helpers.py.
2. app/ codebase has no callers of _resolve_caller_tier (grep check).
3. APIKeyMiddleware stamps tier on auth_ctx from User.subscription_tier.
4. skill_routes uses auth_ctx.tier for the paywall (not the deleted function).
5. access_routes uses auth_ctx.tier (not the deleted function).
"""

from __future__ import annotations

from pathlib import Path


# ── 1. Function deleted from _skill_helpers ───────────────────────────────────

def test_resolve_caller_tier_not_in_skill_helpers() -> None:
    """_resolve_caller_tier must be absent from app/_skill_helpers.py."""
    src = (Path(__file__).parents[1] / "app" / "_skill_helpers.py").read_text()
    assert "def _resolve_caller_tier(" not in src, (
        "_resolve_caller_tier still exists in _skill_helpers.py — issue #25 not completed"
    )


# ── 2. No callers in app/ ─────────────────────────────────────────────────────

def test_no_callers_of_resolve_caller_tier_in_app() -> None:
    """No file in app/ should call _resolve_caller_tier("""
    app_dir = Path(__file__).parents[1] / "app"
    found = []
    for py_file in app_dir.rglob("*.py"):
        content = py_file.read_text()
        # Allow the comment reference in middleware.py (extract from deleted_resolve_caller_tier)
        # but reject any actual call site or import
        for i, line in enumerate(content.splitlines(), 1):
            if "_resolve_caller_tier(" in line and "def _resolve_caller_tier(" not in line:
                # Skip comment lines and the _for_install variant
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "_resolve_caller_tier_for_install" in line:
                    continue
                found.append(f"{py_file.relative_to(app_dir.parent)}:{i}: {line.strip()}")
    assert not found, (
        "_resolve_caller_tier() called in app/ — issue #25 not fully fixed:\n"
        + "\n".join(found)
    )


# ── 3. Middleware stamps tier from User.subscription_tier ─────────────────────

def test_middleware_stamps_tier_in_auth_ctx() -> None:
    """APIKeyMiddleware must populate auth_ctx.tier when a valid api-key is used.

    We verify by grepping the middleware source for the tier= kwarg in the
    AuthContext(...) constructor call on the api-key path.
    """
    src = (Path(__file__).parents[1] / "app" / "middleware.py").read_text()
    assert "tier=_tier" in src, (
        "middleware.py does not stamp tier on AuthContext in the api-key path — "
        "issue #25 requires auth_ctx.tier to be the source of truth"
    )


# ── 4. skill_routes uses auth_ctx.tier ───────────────────────────────────────

def test_skill_routes_uses_auth_ctx_tier() -> None:
    """skill_routes.py must read the paywall tier from auth_ctx, not _resolve_caller_tier."""
    src = (Path(__file__).parents[1] / "app" / "skill_routes.py").read_text()
    assert "auth_ctx.tier" in src or "auth_ctx\", None), \"tier\"" in src or "\"auth_ctx\", None)" in src, (
        "skill_routes.py does not use auth_ctx.tier for the paywall — issue #25 incomplete"
    )
    assert "_resolve_caller_tier(" not in src, (
        "skill_routes.py still calls _resolve_caller_tier() — issue #25 not fully applied"
    )


# ── 5. access_routes uses auth_ctx.tier ──────────────────────────────────────

def test_access_routes_uses_auth_ctx_tier() -> None:
    """access_routes.py must read the tier from auth_ctx, not _resolve_caller_tier."""
    src = (Path(__file__).parents[1] / "app" / "access_routes.py").read_text()
    assert "auth_ctx" in src, (
        "access_routes.py does not reference auth_ctx — issue #25 incomplete"
    )
    assert "_resolve_caller_tier(" not in src, (
        "access_routes.py still calls _resolve_caller_tier() — issue #25 not fully applied"
    )
