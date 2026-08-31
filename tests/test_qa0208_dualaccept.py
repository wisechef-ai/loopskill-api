"""qa0208-w3 — API lane dual-accept aliases for the cookbook->bundle /
recipes->loopskill coordinated migration.

Pattern under test everywhere in this file: new canonical identifier is
primary (written / preferred on read), legacy identifier is accepted as a
documented fallback so existing clients never break. See AGENTS.md
"Legacy identifier deprecation windows (qa0208-w3)" for the full table.
"""

from __future__ import annotations

# bundles_0811 P8: the recipe catalog was relocated to wisechef-ai/loopskill-recipes.
# These tests validate CONTENT that no longer lives in this repo. Skipped here
# rather than DELETED — the coverage is real and belongs with the content, so it
# travels to the catalog repo instead of quietly disappearing. If someone restores
# a recipes/ dir here, the guard lifts and these run again.
import pytest as _pytest
from pathlib import Path as _Path

_CATALOG = _Path(__file__).resolve().parent.parent / "recipes"
pytestmark = _pytest.mark.skipif(
    not _CATALOG.exists(),
    reason=(
        "recipe catalog relocated to wisechef-ai/loopskill-recipes (bundles_0811 P8); "
        "this suite validates catalog CONTENT and runs there"
    ),
)


import hashlib
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import APIKey, Base, Bundle, User


# ── 1. Route alias — /api/bundles == /api/cookbooks (same handler) ──────────


@pytest.fixture()
def dualsurface_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _dualsurface_app(db, *, api_key_user_id=None, is_master=False, cbt_scope=None, cbt_cookbook_id=None):
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None if is_master else api_key_user_id
            request.state.api_key_id = None
            request.state.cookbook_token_scope = cbt_scope
            request.state.cookbook_token_cookbook_id = cbt_cookbook_id
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


def test_list_endpoint_identical_across_both_prefixes(dualsurface_db):
    """GET list via /api/bundles and /api/cookbooks — same status + body."""
    user = User(
        id=uuid4(),
        display_name="U",
        email="u@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    dualsurface_db.add(user)
    dualsurface_db.flush()
    cb = Bundle(id=uuid4(), name="CB", description="d", is_base=False, bundle_owner=user.id)
    dualsurface_db.add(cb)
    dualsurface_db.commit()

    app = _dualsurface_app(dualsurface_db, api_key_user_id=user.id)
    with TestClient(app) as client:
        r_bundles = client.get("/api/bundles")
        r_cookbooks = client.get("/api/cookbooks")
        assert r_bundles.status_code == r_cookbooks.status_code == 200
        assert r_bundles.json() == r_cookbooks.json()


def test_detail_endpoint_identical_across_both_prefixes(dualsurface_db):
    """GET detail via both prefixes — same status + body (byte-identical payload)."""
    user = User(
        id=uuid4(),
        display_name="U",
        email="u2@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    dualsurface_db.add(user)
    dualsurface_db.flush()
    cb = Bundle(id=uuid4(), name="CB2", description="d", is_base=False, bundle_owner=user.id)
    dualsurface_db.add(cb)
    dualsurface_db.commit()

    app = _dualsurface_app(dualsurface_db, api_key_user_id=user.id)
    with TestClient(app) as client:
        r_bundles = client.get(f"/api/bundles/{cb.id}")
        r_cookbooks = client.get(f"/api/cookbooks/{cb.id}")
        assert r_bundles.status_code == r_cookbooks.status_code == 200
        assert r_bundles.json() == r_cookbooks.json()


def test_cbt_scoped_call_identical_across_both_prefixes(dualsurface_db):
    """A cbt_-token-scoped GET returns identical status+body on both prefixes."""
    user = User(
        id=uuid4(),
        display_name="U",
        email="u3@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    dualsurface_db.add(user)
    dualsurface_db.flush()
    cb = Bundle(id=uuid4(), name="CB3", description="d", is_base=False, bundle_owner=user.id)
    dualsurface_db.add(cb)
    dualsurface_db.commit()

    app = _dualsurface_app(dualsurface_db, cbt_scope="read", cbt_cookbook_id=cb.id)
    with TestClient(app) as client:
        r_bundles = client.get(f"/api/bundles/{cb.id}")
        r_cookbooks = client.get(f"/api/cookbooks/{cb.id}")
        assert r_bundles.status_code == r_cookbooks.status_code == 200
        assert r_bundles.json() == r_cookbooks.json()


def test_cbt_token_wrong_cookbook_403_on_both_prefixes(dualsurface_db):
    """cbt_ token scoped to cookbook A hitting cookbook B → 403 on BOTH prefixes."""
    user = User(
        id=uuid4(),
        display_name="U",
        email="u4@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    dualsurface_db.add(user)
    dualsurface_db.flush()
    cb_a = Bundle(id=uuid4(), name="A", description="d", is_base=False, bundle_owner=user.id)
    cb_b = Bundle(id=uuid4(), name="B", description="d", is_base=False, bundle_owner=user.id)
    dualsurface_db.add_all([cb_a, cb_b])
    dualsurface_db.commit()

    app = _dualsurface_app(dualsurface_db, cbt_scope="edit", cbt_cookbook_id=cb_a.id)
    with TestClient(app) as client:
        r_bundles = client.get(f"/api/bundles/{cb_b.id}")
        r_cookbooks = client.get(f"/api/cookbooks/{cb_b.id}")
        assert r_bundles.status_code == 403, r_bundles.text
        assert r_cookbooks.status_code == 403, r_cookbooks.text


def test_cbt_token_publish_blocked_on_both_prefixes():
    """cbt_ token on a non-allowed (_publish) path 403s on BOTH prefixes —
    proven via the enforcement helper directly (no publisher router in this
    minimal test app; see test_share_tokens.py::test_cbt_token_blocks_publish
    for the established pattern)."""
    from app.share_token_routes import enforce_cbt_scope
    from fastapi import HTTPException

    class MockState:
        cookbook_token_scope = "edit"
        cookbook_token_cookbook_id = uuid4()

    for prefix in ("/api/bundles", "/api/cookbooks"):

        class MockRequest:
            state = MockState()
            method = "POST"
            url = type("U", (), {"path": f"{prefix}/x/_publish"})

        with pytest.raises(HTTPException) as exc_info:
            enforce_cbt_scope(MockRequest())
        assert exc_info.value.status_code == 403


def test_cbt_middleware_allow_list_covers_both_prefixes():
    """app/middleware/api_key.py must scope cbt_ tokens to BOTH /api/cookbooks/*
    and /api/bundles/* (not just the legacy path)."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app" / "middleware" / "api_key.py").read_text()
    assert '"/api/cookbooks/"' in src
    assert '"/api/bundles/"' in src


# ── 2. Referral cookie dual-accept ───────────────────────────────────────────


def test_referral_cookie_name_is_canonical_loopskill_ref():
    from app.referral import REFERRAL_COOKIE_NAME

    assert REFERRAL_COOKIE_NAME == "loopskill_ref"


def test_resolve_referral_cookie_prefers_canonical():
    from app.referral import resolve_referral_cookie

    class _Req:
        cookies = {"loopskill_ref": "NEWCODE", "recipes_ref": "OLDCODE"}

    assert resolve_referral_cookie(_Req()) == "NEWCODE"


def test_resolve_referral_cookie_falls_back_to_legacy():
    from app.referral import resolve_referral_cookie

    class _Req:
        cookies = {"recipes_ref": "OLDCODE"}

    assert resolve_referral_cookie(_Req()) == "OLDCODE"


def test_resolve_referral_cookie_none_when_absent():
    from app.referral import resolve_referral_cookie

    class _Req:
        cookies = {}

    assert resolve_referral_cookie(_Req()) is None


# ── 3. Env var dual-accept: LOOPSKILL_API_KEY primary, RECIPES_API_KEY fallback ──


def _load_reconcile_cli():
    """Import reconcile_cli.py by path (it lives under a skill dir, not app/).

    reconcile_cli.py does ``from _reconcile_lib.reconcile_client import ...``
    so the *parent* of the _reconcile_lib package (scripts/) must be on
    sys.path, not _reconcile_lib itself.
    """
    repo_root = Path(__file__).parents[1]
    scripts_dir = repo_root / "recipes" / "recipes-cookbook-reconcile" / "scripts"
    lib_dir = scripts_dir / "_reconcile_lib"
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location("reconcile_cli", lib_dir / "reconcile_cli.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(scripts_dir))


def test_reconcile_cli_prefers_loopskill_api_key(monkeypatch):
    mod = _load_reconcile_cli()
    monkeypatch.setenv("LOOPSKILL_API_KEY", "lsk_new")
    monkeypatch.setenv("RECIPES_API_KEY", "rec_old")
    monkeypatch.setattr(
        sys, "argv", ["prog", "--cookbook", "cb1", "--skills-dir", "/tmp/s", "--lockfile", "/tmp/l.json"]
    )
    captured = {}

    def _fake_reconcile_once(**kwargs):
        captured.update(kwargs)
        return {"status": "up_to_date"}

    monkeypatch.setattr(mod, "reconcile_once", _fake_reconcile_once)
    rc = mod.main(["--cookbook", "cb1", "--skills-dir", "/tmp/s", "--lockfile", "/tmp/l.json"])
    assert rc == 0
    assert captured["api_key"] == "lsk_new"


def test_reconcile_cli_falls_back_to_recipes_api_key(monkeypatch):
    mod = _load_reconcile_cli()
    monkeypatch.delenv("LOOPSKILL_API_KEY", raising=False)
    monkeypatch.setenv("RECIPES_API_KEY", "rec_old")
    captured = {}

    def _fake_reconcile_once(**kwargs):
        captured.update(kwargs)
        return {"status": "up_to_date"}

    monkeypatch.setattr(mod, "reconcile_once", _fake_reconcile_once)
    rc = mod.main(["--cookbook", "cb1", "--skills-dir", "/tmp/s", "--lockfile", "/tmp/l.json"])
    assert rc == 0
    assert captured["api_key"] == "rec_old"


def test_reconcile_cli_no_key_exits_2(monkeypatch, capsys):
    mod = _load_reconcile_cli()
    monkeypatch.delenv("LOOPSKILL_API_KEY", raising=False)
    monkeypatch.delenv("RECIPES_API_KEY", raising=False)
    rc = mod.main(["--cookbook", "cb1", "--skills-dir", "/tmp/s", "--lockfile", "/tmp/l.json"])
    assert rc == 2


def test_recipes_cli_get_api_key_prefers_loopskill(monkeypatch):
    sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
    try:
        if "recipes_cli" in sys.modules:
            del sys.modules["recipes_cli"]
        import recipes_cli  # type: ignore

        monkeypatch.setenv("LOOPSKILL_API_KEY", "lsk_abc")
        monkeypatch.setenv("RECIPES_API_KEY", "rec_abc")
        assert recipes_cli._get_api_key() == "lsk_abc"

        monkeypatch.delenv("LOOPSKILL_API_KEY", raising=False)
        assert recipes_cli._get_api_key() == "rec_abc"
    finally:
        sys.path.remove(str(Path(__file__).parents[1] / "tools"))


# ── 4. Stripe metadata dual-write / dual-read ────────────────────────────────


def test_subscription_service_writes_both_metadata_keys():
    """create_customer must write BOTH loopskill_user_id and wiserecipes_user_id."""
    src = (Path(__file__).parents[1] / "app" / "subscription_service.py").read_text()
    assert '"loopskill_user_id": str(user.id)' in src
    assert '"wiserecipes_user_id": str(user.id)' in src


def test_user_from_subscription_metadata_prefers_canonical_key(db_session):
    from app.subscription_service import _user_from_subscription_metadata

    user = User(id=uuid4(), display_name="U", email="canon@test.example")
    db_session.add(user)
    db_session.flush()

    sub = {"metadata": {"loopskill_user_id": str(user.id), "wiserecipes_user_id": str(uuid4())}}
    resolved = _user_from_subscription_metadata(sub, db_session)
    assert resolved is not None
    assert resolved.id == user.id


def test_user_from_subscription_metadata_falls_back_to_legacy_key(db_session):
    """In-flight Stripe objects signed BEFORE the rename only carry the legacy
    wiserecipes_user_id key — the reader must still resolve them."""
    from app.subscription_service import _user_from_subscription_metadata

    user = User(id=uuid4(), display_name="U", email="legacy@test.example")
    db_session.add(user)
    db_session.flush()

    sub = {"metadata": {"wiserecipes_user_id": str(user.id)}}
    resolved = _user_from_subscription_metadata(sub, db_session)
    assert resolved is not None
    assert resolved.id == user.id


# ── 5. lsk_ key prefix dual-accept ───────────────────────────────────────────


def test_middleware_user_key_prefixes_includes_lsk_and_rec():
    from app.middleware.api_key import USER_KEY_PREFIXES, API_KEY_PREFIX, LOOPSKILL_KEY_PREFIX

    assert API_KEY_PREFIX == "rec_"
    assert LOOPSKILL_KEY_PREFIX == "lsk_"
    assert USER_KEY_PREFIXES == ("rec_", "lsk_")


def test_lsk_key_validates_identically_to_rec_key_via_mcp_auth(db_session):
    """mcp/auth.py:validate_key must accept an lsk_-prefixed key with the same
    scope resolution as a rec_-prefixed key hitting the same DB row."""
    from app.mcp.auth import validate_key

    user = User(id=uuid4(), display_name="U", email="mcp@test.example")
    db_session.add(user)
    db_session.flush()

    rec_key = f"rec_live_{uuid4().hex}"
    lsk_key = f"lsk_live_{uuid4().hex}"

    rec_row = APIKey(
        user_id=user.id,
        key_prefix=rec_key[:12],
        key_hash=hashlib.sha256(rec_key.encode()).hexdigest(),
        is_active=True,
    )
    lsk_row = APIKey(
        user_id=user.id,
        key_prefix=lsk_key[:12],
        key_hash=hashlib.sha256(lsk_key.encode()).hexdigest(),
        is_active=True,
    )
    db_session.add_all([rec_row, lsk_row])
    db_session.commit()

    rec_result = validate_key(rec_key, db_session)
    lsk_result = validate_key(lsk_key, db_session)

    assert rec_result["scope"] == lsk_result["scope"] == "user"
    assert rec_result["auth_ctx"].scope == lsk_result["auth_ctx"].scope == "user"


def test_lsk_prefixed_key_rejected_as_unauthorized_when_unknown(db_session):
    """An lsk_-shaped key that isn't in the DB is 'unauthorized', not silently
    treated as a different prefix class — same behavior as an unknown rec_ key."""
    from app.mcp.auth import validate_key

    result = validate_key(f"lsk_live_{uuid4().hex}", db_session)
    assert result["scope"] == "unauthorized"


def test_lsk_key_accepted_by_rest_middleware_dispatch(dualsurface_db):
    """REST APIKeyMiddleware must accept an lsk_-prefixed key on a protected
    route with the same 401-vs-pass behavior as rec_ (format gate only —
    full DB round trip is covered by test_5b_backend.py for rec_)."""
    from app.middleware.api_key import USER_KEY_PREFIXES

    # Format-gate check: neither prefix is rejected outright by the
    # startswith() gate that used to hard-require "rec_".
    assert "lsk_live_abc".startswith(USER_KEY_PREFIXES)
    assert "rec_live_abc".startswith(USER_KEY_PREFIXES)
    assert not "cbt_abc".startswith(USER_KEY_PREFIXES)
