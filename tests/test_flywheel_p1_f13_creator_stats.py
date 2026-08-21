"""Tests for flywheel Phase-1 F1.3 — GET /api/creators/me/stats.

Covers: auth (401 anonymous, 200 for JWT/x-api-key user), the internal
(is_test-key) exclusion rule, and the empty state for a caller who owns zero
bundles.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app
from app.models import APIKey, Bundle, InstallEvent, Skill, User


def _mk_user(db, email: str | None = None) -> User:
    email = email or f"u-{uuid4().hex[:8]}@example.com"
    user = User(id=uuid4(), display_name=email, email=email, subscription_tier="free")
    db.add(user)
    db.flush()
    return user


def _mk_key(db, user, *, is_test: bool = False) -> str:
    raw = f"rec_live_{uuid4().hex}"
    db.add(
        APIKey(
            id=uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="stats-key",
            is_active=True,
            is_test=is_test,
        )
    )
    db.flush()
    return raw


def _mk_bundle(db, owner, *, visibility="public", slug=None, name="Stats Bundle") -> Bundle:
    cb = Bundle(id=uuid4(), name=name, bundle_owner=owner.id, visibility=visibility, slug=slug)
    db.add(cb)
    db.flush()
    return cb


def _mk_skill(db, slug=None) -> Skill:
    s = Skill(id=uuid4(), slug=slug or f"stats-skill-{uuid4().hex[:8]}", title="S", is_public=True)
    db.add(s)
    db.flush()
    return s


def _mk_install(db, skill, bundle, *, api_key_id=None):
    ev = InstallEvent(
        id=uuid4(),
        skill_id=skill.id,
        skill_slug=skill.slug,
        api_key_id=api_key_id,
        bundle_id=bundle.id,
        created_at=datetime.now(UTC),
    )
    db.add(ev)
    db.flush()
    return ev


@pytest.fixture
def app_client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


class TestCreatorStatsAuth:
    def test_anonymous_401s(self, app_client):
        resp = app_client.get("/api/creators/me/stats")
        assert resp.status_code == 401

    def test_authenticated_user_via_api_key_200s(self, app_client, db_session):
        owner = _mk_user(db_session)
        raw_key = _mk_key(db_session, owner)
        db_session.commit()

        resp = app_client.get("/api/creators/me/stats", headers={"x-api-key": raw_key})
        assert resp.status_code == 200
        body = resp.json()
        assert body["bundles"] == []
        assert "generated_at" in body
        assert "internal_exclusion_rule" in body


class TestCreatorStatsEmptyState:
    def test_user_with_zero_bundles_gets_empty_list_not_error(self, app_client, db_session):
        owner = _mk_user(db_session)
        raw_key = _mk_key(db_session, owner)
        db_session.commit()

        resp = app_client.get("/api/creators/me/stats", headers={"x-api-key": raw_key})
        assert resp.status_code == 200
        assert resp.json()["bundles"] == []


class TestCreatorStatsExclusion:
    def test_installs_split_external_vs_internal_is_test_key(self, app_client, db_session):
        owner = _mk_user(db_session)
        raw_key = _mk_key(db_session, owner)
        cb = _mk_bundle(db_session, owner, visibility="public", slug=f"stats-{uuid4().hex[:8]}")
        skill = _mk_skill(db_session)

        real_installer = _mk_user(db_session)
        real_key_raw = _mk_key(db_session, real_installer, is_test=False)
        internal_key_raw = _mk_key(db_session, real_installer, is_test=True)
        db_session.commit()

        # Look the raw keys back up to real APIKey rows for api_key_id FKs.
        real_key_row = (
            db_session.query(APIKey)
            .filter(APIKey.key_hash == hashlib.sha256(real_key_raw.encode()).hexdigest())
            .one()
        )
        internal_key_row = (
            db_session.query(APIKey)
            .filter(APIKey.key_hash == hashlib.sha256(internal_key_raw.encode()).hexdigest())
            .one()
        )

        _mk_install(db_session, skill, cb, api_key_id=None)  # anonymous -> external
        _mk_install(db_session, skill, cb, api_key_id=real_key_row.id)  # organic key -> external
        _mk_install(db_session, skill, cb, api_key_id=internal_key_row.id)  # is_test -> excluded
        db_session.commit()

        resp = app_client.get("/api/creators/me/stats", headers={"x-api-key": raw_key})
        assert resp.status_code == 200
        bundles = resp.json()["bundles"]
        assert len(bundles) == 1
        row = bundles[0]
        assert row["slug"] == cb.slug
        assert row["visibility"] == "public"
        assert row["installs_total"] == 3, f"raw total must count every install; got {row!r}"
        assert row["installs_external"] == 2, (
            f"is_test-key install must be excluded from installs_external; got {row!r}"
        )

    def test_only_owned_bundles_are_returned(self, app_client, db_session):
        owner = _mk_user(db_session)
        stranger = _mk_user(db_session)
        raw_key = _mk_key(db_session, owner)
        _mk_bundle(db_session, owner, visibility="public", slug=f"mine-{uuid4().hex[:8]}")
        _mk_bundle(db_session, stranger, visibility="public", slug=f"not-mine-{uuid4().hex[:8]}")
        db_session.commit()

        resp = app_client.get("/api/creators/me/stats", headers={"x-api-key": raw_key})
        assert resp.status_code == 200
        slugs = {b["slug"] for b in resp.json()["bundles"]}
        assert len(slugs) == 1
        assert "not-mine" not in "".join(slugs)
