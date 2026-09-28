"""paywall_0925 — every tier gate records exactly one paywall hit when it refuses
and none when it allows, and the refusal body is byte-for-byte unchanged.

One test class per gate site. HTTP gates run through the production-wired app
(``tests._app_factory.build_test_app``: real APIKeyMiddleware + every router)
with real API keys, except the two routes that authenticate via the session
cookie (deploy, api-keys), which override ``get_current_user_optional``
exactly as their existing suites do. MCP gates call the tool function.

The response-body assertions pin the external contract (``pro_tier_limit``,
``pro_tier_required:*``, ``tier_key_cap_exceeded``, ``needs_tier``, ...):
instrumentation must never change what a client sees.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.models import APIKey, Bundle, BundleSkill, Fleet, PaywallHit, Personality, Skill, SkillVersion, User
from app.services import paywall_hits as ph

# ── seed helpers ────────────────────────────────────────────────────────────


def _user(db, tier, status="active"):
    u = User(
        id=uuid.uuid4(),
        display_name=f"pw-{tier}",
        email=f"pw-{uuid.uuid4().hex[:10]}@example.org",
        subscription_tier=tier,
        subscription_status=status if tier else None,
    )
    db.add(u)
    db.flush()
    return u


def _key(db, user):
    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="paywall-test",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _bundles(db, owner, n, visibility="private"):
    out = []
    for _ in range(n):
        b = Bundle(
            id=uuid.uuid4(),
            name=f"pw-{uuid.uuid4().hex[:8]}",
            slug=f"pw-{uuid.uuid4().hex[:12]}",
            bundle_owner=owner.id,
            visibility=visibility,
        )
        db.add(b)
        out.append(b)
    db.flush()
    return out


def _skill(db, slug, tier="free", *, tarball_path=None):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="paywall test skill",
        tier=tier,
        is_public=True,
        created_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    db.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=s.id,
            semver="1.0.0",
            tarball_size_bytes=1024,
            checksum_sha256="ab" * 32,
            tarball_path=tarball_path,
            created_at=datetime.now(UTC),
        )
    )
    db.flush()
    return s


def _hits(db, gate, user=None):
    q = db.query(PaywallHit).filter(PaywallHit.gate == gate)
    if user is not None:
        q = q.filter(PaywallHit.user_id == user.id)
    return q.all()


def _assert_one_hit(db, gate, user, status):
    rows = _hits(db, gate, user)
    assert len(rows) == 1, f"expected exactly one {gate} hit, got {len(rows)}"
    assert rows[0].http_status == status
    assert rows[0].hit_count == 1
    assert rows[0].classification in ("stranger", "fleet", "unknown")


@pytest.fixture
def app_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _cookie_user_client(db_session, monkeypatch, user):
    """App whose cookie-auth dependency returns ``user`` (deploy, api-keys)."""
    from app import auth_routes
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch, with_middleware=False)
    app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: user
    return TestClient(app)


# ── 1. private-bundle cap: POST /api/bundles → 403 pro_tier_limit ─────────


class TestBundlePrivateCap:
    def test_refusal_records_one_hit_body_unchanged(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _bundles(db_session, u, 2)
        db_session.commit()
        r = app_client.post("/api/bundles", json={"name": "third"}, headers={"x-api-key": k})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["reason"] == "pro_tier_limit"
        assert r.json()["detail"]["max_cookbooks"] == 2
        _assert_one_hit(db_session, ph.GATE_BUNDLE_PRIVATE_CAP, u, 403)

    def test_allowed_create_records_nothing(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        db_session.commit()
        r = app_client.post("/api/bundles", json={"name": "first"}, headers={"x-api-key": k})
        assert r.status_code == 201, r.text
        assert _hits(db_session, ph.GATE_BUNDLE_PRIVATE_CAP, u) == []


# ── 2. fork claim over the private cap → 403 pro_tier_limit ───────────────


class TestForkClaimPrivateCap:
    def _token(self, app_client, db_session):
        owner = _user(db_session, "pro")
        (src,) = _bundles(db_session, owner, 1, visibility="public")
        db_session.add(
            BundleSkill(
                bundle_id=src.id,
                skill_id=_skill(db_session, f"fc-{uuid.uuid4().hex[:6]}").id,
                source="custom-added",
            )
        )
        db_session.commit()
        r = app_client.post(f"/api/bundles/public/{src.slug}/fork/preview")
        assert r.status_code == 200, r.text
        return r.json()["claim_token"]

    def test_refusal_records_one_hit(self, app_client, db_session):
        token = self._token(app_client, db_session)
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _bundles(db_session, u, 2)
        db_session.commit()
        r = app_client.post("/api/bundles/fork/claim", json={"claim_token": token}, headers={"x-api-key": k})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["reason"] == "pro_tier_limit"
        _assert_one_hit(db_session, ph.GATE_FORK_CLAIM_PRIVATE_CAP, u, 403)

    def test_allowed_claim_records_nothing(self, app_client, db_session):
        token = self._token(app_client, db_session)
        u = _user(db_session, "free")
        k = _key(db_session, u)
        db_session.commit()
        r = app_client.post("/api/bundles/fork/claim", json={"claim_token": token}, headers={"x-api-key": k})
        assert r.status_code == 201, r.text
        assert _hits(db_session, ph.GATE_FORK_CLAIM_PRIVATE_CAP, u) == []


# ── 3. per-bundle skill cap: POST /api/bundles/{id}/skills → 403 pro_skill_cap


class TestBundleSkillCap:
    def _setup(self, db_session, monkeypatch, *, fill):
        import app.bundle_routes as br

        monkeypatch.setattr(br, "BUNDLE_SKILL_CAP", 2)
        u = _user(db_session, "pro")
        k = _key(db_session, u)
        (b,) = _bundles(db_session, u, 1)
        for i in range(fill):
            db_session.add(
                BundleSkill(
                    bundle_id=b.id,
                    skill_id=_skill(db_session, f"cap-{uuid.uuid4().hex[:6]}").id,
                    source="custom-added",
                )
            )
        target = _skill(db_session, f"cap-new-{uuid.uuid4().hex[:6]}")
        db_session.commit()
        return u, k, b, target

    def test_refusal_records_one_hit(self, app_client, db_session, monkeypatch):
        u, k, b, target = self._setup(db_session, monkeypatch, fill=2)
        r = app_client.post(
            f"/api/bundles/{b.id}/skills", json={"slug": target.slug}, headers={"x-api-key": k}
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["reason"] == "pro_skill_cap"
        _assert_one_hit(db_session, ph.GATE_BUNDLE_SKILL_CAP, u, 403)

    def test_allowed_add_records_nothing(self, app_client, db_session, monkeypatch):
        u, k, b, target = self._setup(db_session, monkeypatch, fill=1)
        r = app_client.post(
            f"/api/bundles/{b.id}/skills", json={"slug": target.slug}, headers={"x-api-key": k}
        )
        assert r.status_code == 201, r.text
        assert _hits(db_session, ph.GATE_BUNDLE_SKILL_CAP, u) == []


# ── 4. deploy tier: /api/bundle-deploy/create → 402 pro_tier_required ─────


class TestDeployTier:
    def test_refusal_records_one_hit_body_unchanged(self, db_session, monkeypatch):
        u = _user(db_session, "free")
        db_session.commit()
        c = _cookie_user_client(db_session, monkeypatch, u)
        r = c.post("/api/cookbook-deploy/create", json={"name": "x", "visibility": "private"})
        assert r.status_code == 402, r.text
        assert r.json()["detail"] == "pro_tier_required:current=free"
        _assert_one_hit(db_session, ph.GATE_DEPLOY_TIER, u, 402)

    def test_allowed_create_records_nothing(self, db_session, monkeypatch):
        u = _user(db_session, "pro")
        db_session.commit()
        c = _cookie_user_client(db_session, monkeypatch, u)
        r = c.post("/api/cookbook-deploy/create", json={"name": "x", "visibility": "private"})
        assert r.status_code in (200, 201), r.text
        assert _hits(db_session, ph.GATE_DEPLOY_TIER, u) == []


# ── 5. fleet member cap: POST /api/fleets/{id}/members → 402 ──────────────


class TestFleetMemberCap:
    def _enroll(self, c, fleet, key, host):
        return c.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": key},
            json={"host": host, "profile": "default", "skills_dir": "~/.hermes/loopskill"},
        )

    def test_second_member_on_free_records_one_hit(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        fleet = Fleet(
            id=uuid.uuid4(),
            owner_user_id=u.id,
            name="pw-fleet",
            fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        )
        db_session.add(fleet)
        db_session.commit()

        r1 = self._enroll(app_client, fleet, k, "h1")
        assert r1.status_code == 201, r1.text
        assert _hits(db_session, ph.GATE_FLEET_MEMBER_CAP, u) == []  # allowed → none

        r2 = self._enroll(app_client, fleet, k, "h2")
        assert r2.status_code == 402, r2.text
        assert r2.json()["detail"]["error"] == "tier_key_cap_exceeded"
        _assert_one_hit(db_session, ph.GATE_FLEET_MEMBER_CAP, u, 402)


# ── 6. forks tier: POST /api/forks/create → 402 needs_tier ────────────────


class TestForksTier:
    def test_refusal_records_one_hit_body_unchanged(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _skill(db_session, "fork-src-free")
        db_session.commit()
        r = app_client.post(
            "/api/forks/create", json={"source_slug": "fork-src-free", "name": "f"}, headers={"x-api-key": k}
        )
        assert r.status_code == 402, r.text
        assert r.json()["detail"] == {"needs_tier": "pro", "current_tier": "free"}
        _assert_one_hit(db_session, ph.GATE_FORKS_TIER, u, 402)

    def test_allowed_pro_records_nothing(self, app_client, db_session, monkeypatch, tmp_path):
        monkeypatch.setenv("RECIPES_FORKS_DIR", str(tmp_path))
        u = _user(db_session, "pro")
        k = _key(db_session, u)
        _skill(db_session, "fork-src-pro")
        db_session.commit()
        r = app_client.post(
            "/api/forks/create", json={"source_slug": "fork-src-pro", "name": "f"}, headers={"x-api-key": k}
        )
        assert r.status_code in (201, 409), r.text
        assert _hits(db_session, ph.GATE_FORKS_TIER, u) == []


# ── 7. API-key cap: POST /api/api-keys → 403 key_cap_exceeded ─────────────


class TestApiKeyCap:
    def test_second_key_on_free_records_one_hit(self, db_session, monkeypatch):
        u = _user(db_session, "free")
        db_session.commit()
        c = _cookie_user_client(db_session, monkeypatch, u)
        r1 = c.post("/api/api-keys", json={"name": "k1"})
        assert r1.status_code in (200, 201), r1.text
        assert _hits(db_session, ph.GATE_API_KEY_CAP, u) == []  # allowed → none

        r2 = c.post("/api/api-keys", json={"name": "k2"})
        assert r2.status_code == 403, r2.text
        assert r2.json()["detail"].startswith("key_cap_exceeded")
        _assert_one_hit(db_session, ph.GATE_API_KEY_CAP, u, 403)


# ── 8. MCP compose verb over the private cap → cookbook_limit ─────────────


class TestMcpComposePrivateCap:
    def _src(self, db_session):
        owner = _user(db_session, "pro")
        (src,) = _bundles(db_session, owner, 1, visibility="public")
        db_session.add(
            BundleSkill(
                bundle_id=src.id,
                skill_id=_skill(db_session, f"mc-{uuid.uuid4().hex[:6]}").id,
                source="custom-added",
            )
        )
        db_session.flush()
        return src

    def test_refusal_records_one_hit(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.bundle_install import CookbookInstallError
        from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links

        src = self._src(db_session)
        u = _user(db_session, "free")
        _bundles(db_session, u, 2)
        db_session.commit()
        with pytest.raises(CookbookInstallError) as ei:
            loopskill_compose_bundle_from_links(
                db_session,
                links=[f"bundle://{src.slug}"],
                ctx=AuthContext(scope="user", user_id=u.id, tier="free"),
            )
        assert (ei.value.code, ei.value.status) == ("cookbook_limit", 403)
        _assert_one_hit(db_session, ph.GATE_MCP_COMPOSE_PRIVATE_CAP, u, 403)

    def test_allowed_compose_records_nothing(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links

        src = self._src(db_session)
        u = _user(db_session, "free")
        db_session.commit()
        loopskill_compose_bundle_from_links(
            db_session,
            links=[f"bundle://{src.slug}"],
            ctx=AuthContext(scope="user", user_id=u.id, tier="free"),
        )
        assert _hits(db_session, ph.GATE_MCP_COMPOSE_PRIVATE_CAP, u) == []


# ── 9. direct skill install tier: GET /api/skills/install → 403 ──────────


class TestSkillInstallTier:
    def test_free_key_pro_skill_records_one_hit(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _skill(db_session, "pw-pro-install", tier="pro")
        db_session.commit()
        r = app_client.get("/api/skills/install?slug=pw-pro-install", headers={"x-api-key": k})
        assert r.status_code == 403, r.text
        assert "Upgrade to install it" in r.json()["detail"]
        _assert_one_hit(db_session, ph.GATE_SKILL_INSTALL_TIER, u, 403)

    def test_free_key_free_skill_records_nothing(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _skill(db_session, "pw-free-install", tier="free")
        db_session.commit()
        r = app_client.get("/api/skills/install?slug=pw-free-install", headers={"x-api-key": k})
        assert r.status_code == 200, r.text
        assert _hits(db_session, ph.GATE_SKILL_INSTALL_TIER, u) == []


# ── 10. bundle single-skill install tier (HTTP) → 403 ─────────────────────


class TestBundleSkillInstallTier:
    def _setup(self, db_session, owner_tier):
        u = _user(db_session, owner_tier)
        k = _key(db_session, u)
        (b,) = _bundles(db_session, u, 1)
        s = _skill(db_session, f"bsi-{uuid.uuid4().hex[:6]}", tier="pro")
        db_session.add(BundleSkill(bundle_id=b.id, skill_id=s.id, source="custom-added"))
        db_session.commit()
        return u, k, b, s

    def test_free_owner_pro_skill_records_one_hit(self, app_client, db_session):
        u, k, b, s = self._setup(db_session, "free")
        r = app_client.get(f"/api/bundles/{b.id}/skills/{s.slug}/install", headers={"x-api-key": k})
        assert r.status_code == 403, r.text
        _assert_one_hit(db_session, ph.GATE_BUNDLE_SKILL_INSTALL_TIER, u, 403)

    def test_pro_owner_records_nothing(self, app_client, db_session):
        u, k, b, s = self._setup(db_session, "pro")
        r = app_client.get(f"/api/bundles/{b.id}/skills/{s.slug}/install", headers={"x-api-key": k})
        assert r.status_code == 200, r.text
        assert _hits(db_session, ph.GATE_BUNDLE_SKILL_INSTALL_TIER, u) == []


# ── 11. MCP bundle install single Pro skill → tier_insufficient ──────────


class TestMcpBundleInstallTier:
    def _setup(self, db_session, owner_tier):
        u = _user(db_session, owner_tier)
        (b,) = _bundles(db_session, u, 1)
        s = _skill(db_session, f"mbi-{uuid.uuid4().hex[:6]}", tier="pro")
        db_session.add(BundleSkill(bundle_id=b.id, skill_id=s.id, source="custom-added"))
        db_session.commit()
        return u, b, s

    def test_free_owner_records_one_hit(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.bundle_install import CookbookInstallError, loopskill_bundle_install

        u, b, s = self._setup(db_session, "free")
        with pytest.raises(CookbookInstallError) as ei:
            loopskill_bundle_install(
                db=db_session, ctx=AuthContext(scope="user", user_id=u.id), cookbook_id=str(b.id), slug=s.slug
            )
        assert (ei.value.code, ei.value.status) == ("tier_insufficient", 403)
        _assert_one_hit(db_session, ph.GATE_MCP_BUNDLE_INSTALL_TIER, u, 403)

    def test_pro_owner_records_nothing(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.bundle_install import loopskill_bundle_install

        u, b, s = self._setup(db_session, "pro")
        loopskill_bundle_install(
            db=db_session, ctx=AuthContext(scope="user", user_id=u.id), cookbook_id=str(b.id), slug=s.slug
        )
        assert _hits(db_session, ph.GATE_MCP_BUNDLE_INSTALL_TIER, u) == []


# ── 12. skill files tier: GET /api/skills/{slug}/file (pro file) → 403 ───


class TestSkillFilesTier:
    def _tar(self, tmp_path, slug):
        import io
        import tarfile

        p = tmp_path / f"{slug}.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            for name, data in {"SKILL.md": b"# s\n", "secret.py": b"X=1\n"}.items():
                ti = tarfile.TarInfo(name=f"{slug}-1.0.0/{name}")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return str(p)

    def test_free_key_pro_file_records_one_hit(self, app_client, db_session, tmp_path):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        _skill(db_session, "pw-files-pro", tier="pro", tarball_path=self._tar(tmp_path, "pw-files-pro"))
        db_session.commit()
        r = app_client.get(
            "/api/skills/pw-files-pro/file", params={"path": "secret.py"}, headers={"x-api-key": k}
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "Pro subscription required to access this skill's files"
        _assert_one_hit(db_session, ph.GATE_SKILL_FILES_TIER, u, 403)

    def test_pro_key_records_nothing(self, app_client, db_session, tmp_path):
        u = _user(db_session, "pro")
        k = _key(db_session, u)
        _skill(db_session, "pw-files-ok", tier="pro", tarball_path=self._tar(tmp_path, "pw-files-ok"))
        db_session.commit()
        r = app_client.get(
            "/api/skills/pw-files-ok/file", params={"path": "secret.py"}, headers={"x-api-key": k}
        )
        assert r.status_code == 200, r.text
        assert _hits(db_session, ph.GATE_SKILL_FILES_TIER, u) == []


# ── 13. artifact like tier: POST /api/personalities/{slug}/like → 403 ─────


class TestArtifactLikeTier:
    def _p(self, db_session, tier):
        slug = f"pw-soul-{uuid.uuid4().hex[:6]}"
        db_session.add(
            Personality(
                id=uuid.uuid4(), slug=slug, title="Soul", tier=tier, is_public=True, system_prompt="be kind"
            )
        )
        db_session.flush()
        return slug

    def test_free_user_pro_personality_records_one_hit(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        slug = self._p(db_session, "pro")
        db_session.commit()
        r = app_client.post(f"/api/personalities/{slug}/like", headers={"x-api-key": k})
        assert r.status_code == 403, r.text
        assert r.json()["detail"].startswith("tier_gated:")
        _assert_one_hit(db_session, ph.GATE_ARTIFACT_LIKE_TIER, u, 403)

    def test_free_user_free_personality_records_nothing(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        slug = self._p(db_session, None)
        db_session.commit()
        r = app_client.post(f"/api/personalities/{slug}/like", headers={"x-api-key": k})
        assert r.status_code == 200, r.text
        assert _hits(db_session, ph.GATE_ARTIFACT_LIKE_TIER, u) == []


# ── 14. recipify tier: POST /api/recipify → 401 needs_tier (historical code)


class TestRecipifyTier:
    def test_free_user_records_one_hit_body_unchanged(self, app_client, db_session):
        u = _user(db_session, "free")
        k = _key(db_session, u)
        db_session.commit()
        r = app_client.post(
            "/api/recipify", json={"slug": "pw-r", "content": "---\nname: x\n---\n"}, headers={"x-api-key": k}
        )
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == {"needs_tier": "pro", "current_tier": "free"}
        _assert_one_hit(db_session, ph.GATE_RECIPIFY_TIER, u, 401)


# ── 15. MCP tailor (fork) verbs → needs_tier ─────────────────────────────


class TestMcpForkTier:
    def test_free_user_records_one_hit(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.fork_deploy import loopskill_tailor_version

        u = _user(db_session, "free")
        db_session.commit()
        out = loopskill_tailor_version(
            db_session,
            fork_id=str(uuid.uuid4()),
            tarball_base64=base64.b64encode(b"x").decode(),
            semver="1.0.1",
            ctx=AuthContext(scope="user", user_id=u.id, tier="free"),
        )
        assert out["error"] == "needs_tier"
        _assert_one_hit(db_session, ph.GATE_MCP_FORK_TIER, u, 402)

    def test_pro_user_passes_gate_records_nothing(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.fork_deploy import loopskill_tailor_version

        u = _user(db_session, "pro")
        db_session.commit()
        out = loopskill_tailor_version(
            db_session,
            fork_id=str(uuid.uuid4()),
            tarball_base64=base64.b64encode(b"x").decode(),
            semver="1.0.1",
            ctx=AuthContext(scope="user", user_id=u.id, tier="pro"),
        )
        assert out.get("error") != "needs_tier"  # gate passed (fork then 404s)
        assert _hits(db_session, ph.GATE_MCP_FORK_TIER, u) == []


# ── 16. metasearch deploy skill cap → 403 skill_cap_reached ──────────────


class TestMetasearchDeploySkillCap:
    """/api/skills/metasearch/* is a public-path prefix in the key middleware,
    so (like its own suite) this injects the authenticated user onto
    request.state rather than sending an x-api-key."""

    def _run(self, db_session, monkeypatch, tmp_path, *, fill):
        import app.metasearch_deploy_routes as mdr
        from tests.test_metasearch_deploy_route import _make_app, _mock_resolvable

        monkeypatch.setattr(mdr, "BUNDLE_SKILL_CAP", 1)
        _mock_resolvable(monkeypatch, tmp_path=tmp_path)
        u = _user(db_session, "pro")
        (b,) = _bundles(db_session, u, 1)
        for _ in range(fill):
            db_session.add(
                BundleSkill(
                    bundle_id=b.id,
                    skill_id=_skill(db_session, f"md-{uuid.uuid4().hex[:6]}").id,
                    source="custom-added",
                )
            )
        db_session.commit()
        with TestClient(_make_app(db_session, user_id=u.id)) as c:
            r = c.post(
                "/api/skills/metasearch/deploy",
                json={"install_ref": "skills-sh:o--r--s", "fleet_id": str(b.id)},
            )
        return u, r

    def test_new_row_over_cap_records_one_hit(self, db_session, monkeypatch, tmp_path):
        u, r = self._run(db_session, monkeypatch, tmp_path, fill=1)
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == {"deployed": False, "reason": "skill_cap_reached", "cap": 1}
        _assert_one_hit(db_session, ph.GATE_METASEARCH_DEPLOY_SKILL_CAP, u, 403)

    def test_under_cap_records_nothing(self, db_session, monkeypatch, tmp_path):
        u, r = self._run(db_session, monkeypatch, tmp_path, fill=0)
        assert r.status_code == 200, r.text
        assert _hits(db_session, ph.GATE_METASEARCH_DEPLOY_SKILL_CAP, u) == []


# ── the gate list and the wired sites stay in lock-step ─────────────────


def test_every_gate_constant_has_a_non_test_call_site():
    """A GATE_* constant nobody records is a gate the pulse silently never shows."""
    import pathlib
    import re

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    src = "\n".join(
        p.read_text()
        for p in app_dir.rglob("*.py")
        if p.name != "paywall_hits.py" and "__pycache__" not in p.parts
    )
    gates = [n for n in dir(ph) if n.startswith("GATE_")]
    missing = [g for g in gates if not re.search(rf"\b{g}\b", src)]
    assert not missing, f"gate constants with no call site: {missing}"
