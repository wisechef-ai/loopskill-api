"""bundles_0811 Phase P2.5 — API key caps move to the config/tiers.yaml SSOT.

Verified defect (app/api_key_routes.py, pre-fix): a Python dict literal
``KEY_CAP`` held ``{"free": 1, "pro": 1, ...}`` — a $9.95/mo Pro customer had
the SAME single-key cap as a Free user. Every workflow the product advertises
(one key per client, per machine, per agent) was impossible at cap 1.

Locked decision (Adam 2026-08-10, plan §0 lock #11 / §3 Phase P2.5):
  free      : 1   <- UNCHANGED, deliberately — the cap is a Pro differentiator
  pro       : 10  <- the fix (was 1)
  cook      : follows pro (legacy alias) -> 10
  pro_plus  : 20  <- unchanged
  operator  : follows pro_plus (legacy alias) -> 20
  studio    : follows pro_plus (legacy alias) -> 20
  DEFAULT_CAP (unknown/null tier): 1 <- unchanged, never inherits a raise

This suite covers the acceptance gates from the task brief:
  1. Free user mints 1 key; 2nd -> 403 key_cap_exceeded
  2. Pro user mints 10 keys; 11th -> 403
  3. Pro+ still gets 20
  4. RED-PROOF: unknown/null tier still capped at 1 — written so it would
     FAIL if DEFAULT_API_KEY_CAP were raised (asserts the exact fallback
     value used by the enforcement path, not just "some cap thnat happens to
     be 1 today")
  5. SSOT test: no bare int cap literal remains in api_key_routes.py, AND
     patching config/tiers.yaml changes the enforced cap live
"""

from __future__ import annotations

import ast
import hashlib
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_key_routes import router as api_key_router
from app.auth_routes import get_current_user_optional
from app.database import get_db
from app.models import APIKey, Base, User
from app.tier_labels import DEFAULT_API_KEY_CAP, TIERS_YAML
from app.tier_labels import api_key_cap as tier_api_key_cap

REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY_ROUTES_PATH = REPO_ROOT / "app" / "api_key_routes.py"


# ── In-memory DB fixture ───────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=e)
    yield e
    Base.metadata.drop_all(bind=e)


@pytest.fixture()
def db(engine) -> Session:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ── Helper factories ───────────────────────────────────────────────────────


def _make_user(db: Session, tier: str | None = "free", status: str = "active") -> User:
    u = User(
        id=uuid4(),
        display_name=f"Test {tier}",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_active_key(db: Session, user: User) -> APIKey:
    """Insert a pre-hashed active key for a user (does NOT go through the endpoint)."""
    body = secrets.token_urlsafe(32)
    plaintext = f"rec_live_{body}"
    prefix = plaintext[:12]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    k = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix=prefix,
        key_hash=key_hash,
        name="test-key",
        label="test-key",
        is_active=True,
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    return k


def _make_test_app(db: Session, authed_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(api_key_router)

    def override_db():
        yield db

    def override_user():
        return authed_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_optional] = override_user
    return TestClient(app, raise_server_exceptions=True)


# ── 1. Free user: 1 key allowed, 2nd blocked ────────────────────────────────


class TestFreeStaysAtOne:
    def test_free_first_key_succeeds(self, db):
        user = _make_user(db, tier="free")
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "first"})
        assert r.status_code == 200, r.text

    def test_free_second_key_blocked_with_key_cap_exceeded(self, db):
        user = _make_user(db, tier="free")
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "second"})
        assert r.status_code == 403, r.text
        assert "key_cap_exceeded" in r.json()["detail"]


# ── 2. Pro user: 10 keys allowed, 11th blocked ──────────────────────────────


class TestProGetsTen:
    def test_pro_mints_ten_keys(self, db):
        user = _make_user(db, tier="pro")
        client = _make_test_app(db, user)
        for i in range(10):
            r = client.post("/api/api-keys", json={"label": f"key{i}"})
            assert r.status_code == 200, f"key {i} should succeed: {r.text}"

    def test_pro_eleventh_key_blocked(self, db):
        user = _make_user(db, tier="pro")
        for _ in range(10):
            _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "eleventh"})
        assert r.status_code == 403, r.text
        assert "key_cap_exceeded" in r.json()["detail"]

    def test_legacy_cook_alias_follows_pro_cap_of_ten(self, db):
        """Legacy 'cook' slug must resolve to Pro's cap (10), not Free's (1)."""
        user = _make_user(db, tier="cook")
        for _ in range(9):
            _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "tenth"})
        assert r.status_code == 200, r.text
        # 11th still blocked
        r2 = client.post("/api/api-keys", json={"label": "eleventh"})
        assert r2.status_code == 403, r2.text


# ── 3. Pro+ still gets 20 ────────────────────────────────────────────────────


class TestProPlusStillTwenty:
    def test_pro_plus_allows_20_keys(self, db):
        user = _make_user(db, tier="pro_plus")
        client = _make_test_app(db, user)
        for i in range(19):
            _make_active_key(db, user)
        r = client.post("/api/api-keys", json={"label": "key20"})
        assert r.status_code == 200, r.text

    def test_pro_plus_21st_key_blocked(self, db):
        user = _make_user(db, tier="pro_plus")
        for _ in range(20):
            _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 403, r.text
        assert "key_cap_exceeded" in r.json()["detail"]

    def test_legacy_operator_and_studio_follow_pro_plus_cap(self, db):
        for alias in ("operator", "studio"):
            user = _make_user(db, tier=alias)
            for _ in range(19):
                _make_active_key(db, user)
            client = _make_test_app(db, user)
            r = client.post("/api/api-keys", json={"label": "key20"})
            assert r.status_code == 200, f"{alias}: {r.text}"


# ── 4. RED-PROOF: unknown/null tier stays capped at 1 ───────────────────────


class TestUnknownAndNullTierRedProof:
    """These tests are written to FAIL if DEFAULT_API_KEY_CAP were ever raised.

    They assert against the module-level ``DEFAULT_API_KEY_CAP`` constant
    directly (not a bare literal ``1``), so bumping that constant — the exact
    mistake this phase is guarding against — flips these tests red
    immediately, before any enforcement-path test would even need to run.
    """

    def test_default_cap_constant_is_one(self):
        """RED-PROOF anchor: if DEFAULT_API_KEY_CAP is ever raised, this fails first."""
        assert DEFAULT_API_KEY_CAP == 1, (
            "DEFAULT_API_KEY_CAP must stay 1 — an unknown/null tier is the "
            "MOST restrictive case and must never inherit a raise (plan §0 "
            "lock #11)."
        )

    def test_unknown_tier_capped_at_default(self, db):
        user = _make_user(db, tier="enterprise_made_up")
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 403, r.text
        assert f"max {DEFAULT_API_KEY_CAP} active key" in r.json()["detail"]

    def test_null_tier_capped_at_default(self, db):
        user = _make_user(db, tier=None)
        user.subscription_tier = None
        db.commit()
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 403, r.text
        assert f"max {DEFAULT_API_KEY_CAP} active key" in r.json()["detail"]

    def test_unknown_tier_helper_returns_default_not_free_yaml_value(self):
        """tier_labels.api_key_cap() for an unknown tier returns the HARDCODED
        DEFAULT_API_KEY_CAP fallback, not a read of free's YAML value — so a
        future edit that raises free.api_key_cap can never silently raise the
        unknown/null-tier cap too (they are independent by construction).
        """
        assert tier_api_key_cap("totally_unknown_tier") == DEFAULT_API_KEY_CAP
        assert tier_api_key_cap(None) == DEFAULT_API_KEY_CAP


# ── 5. SSOT: caps come from config/tiers.yaml, not a Python literal ────────


class TestCapsComeFromConfigSSOT:
    def test_tiers_yaml_carries_every_cap(self):
        with open(TIERS_YAML) as f:
            tiers = yaml.safe_load(f)["tiers"]
        assert tiers["free"]["api_key_cap"] == 1
        assert tiers["pro"]["api_key_cap"] == 10
        assert tiers["pro_plus"]["api_key_cap"] == 20

    def test_api_key_routes_has_no_bare_int_cap_dict(self):
        """No KEY_CAP-shaped dict[str, int] literal remains in api_key_routes.py.

        Parses the module with `ast` (not a regex) so this can't be fooled by
        comment wording — it walks every module-level assignment and fails if
        any target is a dict literal whose values are all int/None (the exact
        shape the old ``KEY_CAP`` dict had).
        """
        source = API_KEY_ROUTES_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(API_KEY_ROUTES_PATH))

        offending: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            if not node.value.values:
                continue
            all_int_like = all(
                isinstance(v, ast.Constant) and (isinstance(v.value, int) or v.value is None)
                for v in node.value.values
            )
            if all_int_like:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        offending.append(target.id)

        assert offending == [], (
            f"api_key_routes.py still holds a dict[str, int] cap literal: {offending} "
            "— caps must be read via app.tier_labels.api_key_cap(), never a "
            "module-level dict."
        )

    def test_source_imports_the_ssot_helper(self):
        source = API_KEY_ROUTES_PATH.read_text(encoding="utf-8")
        assert "from app.tier_labels import" in source and "api_key_cap" in source

    def test_patching_config_changes_the_enforced_cap(self, db, monkeypatch, tmp_path):
        """Point tier_labels at a patched copy of tiers.yaml with pro raised to
        3, reload the cached loader, and prove the enforcement path in
        api_key_routes.py picks up the new number live — i.e. it is genuinely
        reading through the SSOT helper, not a frozen import-time copy.
        """
        import importlib
        import sys

        with open(TIERS_YAML) as f:
            data = yaml.safe_load(f)
        data["tiers"]["pro"]["api_key_cap"] = 3
        patched = tmp_path / "tiers.yaml"
        patched.write_text(yaml.safe_dump(data), encoding="utf-8")

        # Reload tier_labels with TIERS_YAML monkeypatched, then reload
        # api_key_routes so its `from app.tier_labels import api_key_cap`
        # binds to the patched module's function.
        for mod in list(sys.modules.keys()):
            if mod in ("app.tier_labels", "app.api_key_routes"):
                del sys.modules[mod]

        import app.tier_labels as tl

        monkeypatch.setattr(tl, "TIERS_YAML", patched)
        tl._tiers.cache_clear()
        assert tl.api_key_cap("pro") == 3, "tier_labels did not pick up the patched config"

        import app.api_key_routes as akr

        importlib.reload(akr)

        try:
            app = FastAPI()
            app.include_router(akr.router)
            user = _make_user(db, tier="pro")
            for _ in range(2):
                _make_active_key(db, user)

            def override_db():
                yield db

            def override_user():
                return user

            app.dependency_overrides[get_db] = override_db
            app.dependency_overrides[get_current_user_optional] = override_user
            client = TestClient(app, raise_server_exceptions=True)

            # 3rd key (2 pre-existing + this one = 3) succeeds under the patched cap of 3
            r = client.post("/api/api-keys", json={"label": "third"})
            assert r.status_code == 200, r.text
            # 4th is blocked
            r2 = client.post("/api/api-keys", json={"label": "fourth"})
            assert r2.status_code == 403, r2.text
        finally:
            # Restore real modules for any tests that run after this one in
            # the same process.
            for mod in list(sys.modules.keys()):
                if mod in ("app.tier_labels", "app.api_key_routes"):
                    del sys.modules[mod]
            import app.tier_labels  # noqa: F401
            import app.api_key_routes  # noqa: F401
