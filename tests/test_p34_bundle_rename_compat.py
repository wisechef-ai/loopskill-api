"""Phase 3+4 rename compat tests — cookbook→bundle, cbt_→bdl_, salt rename.

RED phase: these tests verify the NEW surface that does not yet exist.
After implementation they become GREEN and verify both new paths and
legacy compat paths still work.

All compat-specific assertions are tagged # compat-test.
"""

from __future__ import annotations

import pytest
from itsdangerous import URLSafeTimedSerializer


_SECRET = "test-signing-secret"
_NEW_SALT = "loopskill-install"
_OLD_SALT = "recipes-skill-install"  # compat-test


# ── Salt tests ──────────────────────────────────────────────────────────────


def test_new_salt_roundtrips() -> None:
    """New loopskill-install salt must sign+verify correctly (RED until implemented)."""
    s = URLSafeTimedSerializer(_SECRET, salt=_NEW_SALT)
    token = s.dumps({"slug": "test-skill", "version_id": "abc", "mode": "files"})
    payload = s.loads(token, max_age=3600)
    assert payload["slug"] == "test-skill"


def test_install_routes_uses_new_salt() -> None:
    """install_routes.py must contain the new loopskill-install salt literal."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app" / "install_routes.py").read_text()
    assert 'salt="loopskill-install"' in src, (
        "install_routes.py does not contain salt=\"loopskill-install\" (Phase 3+4 not implemented)"
    )


def test_install_routes_dual_salt_verify() -> None:
    """install_routes.py _download must accept BOTH salts (old tokens still fly).

    We verify by checking the source contains both the new primary salt and the
    old fall-back salt, so unexpired URLs signed with the old salt still work.
    """  # compat-test
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app" / "install_routes.py").read_text()
    assert 'salt="loopskill-install"' in src, "new salt not in install_routes"
    assert '"recipes-skill-install"' in src or "'recipes-skill-install'" in src, (
        "old fall-back salt not in install_routes — compat-test: unexpired URLs would break"
    )  # compat-test


def test_old_salt_token_still_verifies_via_fallback() -> None:
    """A token signed with the OLD salt must still load when the verifier tries both.

    This directly tests the dual-salt verify logic that must exist in
    install_routes._download_tarball.
    """  # compat-test
    from app.install_routes import _verify_signed_token  # type: ignore[attr-defined]

    old_signer = URLSafeTimedSerializer(_SECRET, salt=_OLD_SALT)
    old_token = old_signer.dumps(
        {"slug": "compat-skill", "version_id": "v1", "mode": "files"}
    )
    # Must not raise — fall-back to old salt
    payload = _verify_signed_token(old_token, secret=_SECRET, max_age=3600)  # compat-test
    assert payload["slug"] == "compat-skill"


# ── Model rename tests ────────────────────────────────────────────────────────


def test_bundle_model_importable() -> None:
    """Bundle, BundleSkill, BundleShareToken, BundleDeployment must be importable."""
    from app.models import Bundle, BundleDeployment, BundleShareToken, BundleSkill  # type: ignore[attr-defined]

    assert Bundle.__tablename__ == "bundles"
    assert BundleSkill.__tablename__ == "bundle_skills"
    assert BundleShareToken.__tablename__ == "bundle_share_tokens"
    assert BundleDeployment.__tablename__ == "bundle_deployments"


def test_bundle_model_fk_columns() -> None:
    """Bundle FK columns must use bundle_id naming."""
    from app.models import Bundle, BundleSkill, BundleShareToken

    # parent_bundle_id, synced_from_bundle_id on Bundle
    assert hasattr(Bundle, "parent_bundle_id")
    assert hasattr(Bundle, "synced_from_bundle_id")
    # bundle_id on BundleSkill
    assert hasattr(BundleSkill, "bundle_id")
    # bundle_id on BundleShareToken
    assert hasattr(BundleShareToken, "bundle_id")


def test_cookbook_compat_aliases() -> None:
    """Cookbook/CookbookSkill/etc. must still be importable as compat aliases."""  # compat-test
    from app.models import Cookbook, CookbookDeployment, CookbookShareToken, CookbookSkill  # compat-test

    # Same classes as the renamed ones
    from app.models import Bundle, BundleDeployment, BundleShareToken, BundleSkill

    assert Cookbook is Bundle  # compat-test
    assert CookbookSkill is BundleSkill  # compat-test
    assert CookbookShareToken is BundleShareToken  # compat-test
    assert CookbookDeployment is BundleDeployment  # compat-test


# ── Route tests ────────────────────────────────────────────────────────────


def _make_test_app():
    """Create a minimal test app with the bundle router mounted."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import get_db
    from app.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    app = FastAPI()

    def _override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    return app, TestClient(app, raise_server_exceptions=True)


def test_bundle_route_prefix_exists() -> None:
    """bundle_routes.py must expose /api/bundles prefix routes."""
    from app.bundle_routes import router  # type: ignore[attr-defined]

    paths = [r.path for r in router.routes]
    bundle_paths = [p for p in paths if "/bundles" in p]
    assert bundle_paths, (
        f"No /bundles paths in cookbook router — found: {paths}"
    )


def test_bundle_deploy_route_prefix_exists() -> None:
    """bundle_deployment_routes.py must expose /api/bundle-deploy prefix routes."""
    from app.bundle_deployment_routes import router  # type: ignore[attr-defined]

    paths = [r.path for r in router.routes]
    bundle_paths = [p for p in paths if "bundle-deploy" in p]
    assert bundle_paths, (
        f"No /bundle-deploy paths in deployment router — found: {paths}"
    )


def test_old_cookbooks_route_still_in_router() -> None:
    """Old /api/cookbooks paths must still be present as compat-alias routes."""  # compat-test
    from app.bundle_routes import router  # type: ignore[attr-defined]

    paths = [r.path for r in router.routes]
    compat_paths = [p for p in paths if "/cookbooks" in p]
    assert compat_paths, (
        "compat-test: /api/cookbooks paths disappeared — old clients would break"
    )  # compat-test


# ── Auth scope tests ─────────────────────────────────────────────────────────


def test_scope_literal_includes_bdl_token() -> None:
    """Scope type must include bdl_token for new share tokens."""
    import typing

    from app.auth_ctx import Scope

    args = typing.get_args(Scope)
    assert "bdl_token" in args, f"bdl_token not in Scope literal: {args}"


def test_scope_literal_keeps_cbt_token() -> None:
    """Scope type must still include cbt_token for backward compat."""  # compat-test
    import typing

    from app.auth_ctx import Scope

    args = typing.get_args(Scope)
    assert "cbt_token" in args, "cbt_token removed from Scope — existing tokens would break"  # compat-test


# ── MCP tool name tests ──────────────────────────────────────────────────────


def test_mcp_registry_has_bundle_list() -> None:
    """MCP registry must include the new neutral-verb bundle list tool."""
    from app.mcp.registry import _tool_definitions

    names = {t.name for t in _tool_definitions()}
    assert "bundle_list" in names, f"bundle_list not in MCP tools: {sorted(names)}"


def test_mcp_registry_has_bundle_install() -> None:
    """MCP registry must include the new bundle_install tool."""
    from app.mcp.registry import _tool_definitions

    names = {t.name for t in _tool_definitions()}
    assert "bundle_install" in names, f"bundle_install not in MCP tools: {sorted(names)}"


def test_mcp_old_tool_names_kept_as_compat() -> None:
    """Old recipes_* MCP tool names must still be registered as compat aliases."""  # compat-test
    from app.mcp.registry import _tool_definitions

    names = {t.name for t in _tool_definitions()}
    compat_names = ["recipes_install", "recipes_search", "recipes_list_cookbook"]
    for name in compat_names:
        assert name in names, f"compat-test: {name} removed from MCP — existing agents would break"  # compat-test
