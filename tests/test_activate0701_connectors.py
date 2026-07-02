"""Phase B (loopskill_activate_0701) — CONNECTOR ARTIFACT server-side tests.

Covers the contract in docs/design/activate0701-phaseB-connectors.md §Tests
(server section): publish happy / dup-409 / secret-lint-reject /
required-env-consistency; bundle declare; reconcile diff carries connectors
section; generation bump on new ConnectorVersion (Phase 0 bug-4 regression
class — 304 invalidation); public browse pagination; anonymous write 401.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Bundle,
    BundleConnector,
    Connector,
    ConnectorVersion,
    User,
)
from app.services.connector_validation import (
    ConnectorValidationError,
    validate_connector_version,
)
from app.services.reconcile import (
    bump_declaring_bundles_for_connector as _bump_for_connector,
)
from tests._app_factory import build_test_app

# ───────────────────────────── fixtures ────────────────────────────────────

_MASTER_KEY = "rec_dev_wiserecipes_local_testing_key"


@pytest.fixture()
def app_and_client(db_session: Session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    with TestClient(app, headers={"x-api-key": _MASTER_KEY}) as c:
        yield c


def _zai_template() -> dict:
    """A valid stdio template using ${VAR} refs only (grep-proof)."""
    return {
        "command": "npx",
        "args": ["-y", "zai-mcp"],
        "env": {"ZAI_API_KEY": "${ZAI_API_KEY}"},
    }


# ──────────────────────── validation: secret lint ──────────────────────────


class TestSecretLint:
    def test_clean_template_passes(self) -> None:
        validate_connector_version(
            connector_type="stdio",
            config_template=_zai_template(),
            required_env=["ZAI_API_KEY"],
        )

    def test_literal_stripe_key_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError, match="literal.secret"):
            validate_connector_version(
                connector_type="stdio",
                config_template={
                    "command": "x",
                    "env": {"SK": "sk_live_" + "a" * 24},
                },
                required_env=[],
            )

    def test_literal_openai_key_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError):
            validate_connector_version(
                connector_type="stdio",
                config_template={"command": "x", "env": {"K": "sk-" + "a" * 30}},
                required_env=[],
            )

    def test_bearer_token_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError, match="literal.secret|Bearer"):
            validate_connector_version(
                connector_type="http",
                config_template={
                    "url": "https://x.example",
                    "headers": {"Authorization": "Bearer abc.def.ghi"},
                },
                required_env=[],
            )

    def test_long_base64ish_string_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError):
            validate_connector_version(
                connector_type="stdio",
                config_template={"command": "x", "env": {"TOK": "A" * 48}},
                required_env=[],
            )

    def test_absolute_home_path_warned_and_blocked(self) -> None:
        # /home/<user> path is a leak class — reject per §0.5
        with pytest.raises(ConnectorValidationError):
            validate_connector_version(
                connector_type="stdio",
                config_template={"command": "/home/bob/bin/run"},
                required_env=[],
            )

    def test_var_ref_only_passes_through_env(self) -> None:
        # ${VAR} is allowed in any position
        validate_connector_version(
            connector_type="stdio",
            config_template={"command": "x", "env": {"A": "${MY_VAR}"}},
            required_env=["MY_VAR"],
        )


# ─────────────────── validation: required_env consistency ──────────────────


class TestRequiredEnvConsistency:
    def test_required_env_not_in_template_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError, match="required_env"):
            validate_connector_version(
                connector_type="stdio",
                config_template={"command": "x"},
                required_env=["NEVER_REFERENCED"],
            )

    def test_template_var_without_required_env_passes(self) -> None:
        # template can reference vars not declared in required_env (the agent
        # is allowed to leave optional ones out). required_env is the floor.
        validate_connector_version(
            connector_type="stdio",
            config_template={"command": "x", "env": {"OPT": "${OPT_VAR}"}},
            required_env=[],
        )

    def test_required_env_listed_and_present_passes(self) -> None:
        validate_connector_version(
            connector_type="stdio",
            config_template={"command": "x", "env": {"A": "${A}"}},
            required_env=["A"],
        )


# ─────────────────────── validation: type-specific ─────────────────────────


class TestTypeSpecificFields:
    def test_stdio_requires_command(self) -> None:
        with pytest.raises(ConnectorValidationError, match="command"):
            validate_connector_version(
                connector_type="stdio",
                config_template={"args": ["x"]},
                required_env=[],
            )

    def test_http_requires_url(self) -> None:
        with pytest.raises(ConnectorValidationError, match="url"):
            validate_connector_version(
                connector_type="http",
                config_template={"headers": {}},
                required_env=[],
            )

    def test_sse_requires_url(self) -> None:
        with pytest.raises(ConnectorValidationError, match="url"):
            validate_connector_version(
                connector_type="sse",
                config_template={},
                required_env=[],
            )

    def test_unknown_connector_type_rejected(self) -> None:
        with pytest.raises(ConnectorValidationError, match="connector_type"):
            validate_connector_version(
                connector_type="weird",
                config_template={"command": "x"},
                required_env=[],
            )


# ──────────────────────────── HTTP publish ─────────────────────────────────


class TestPublishHTTP:
    def test_publish_happy_path(self, app_and_client: TestClient, db_session: Session) -> None:
        slug = f"zai-{uuid4().hex[:6]}"
        r = app_and_client.post(
            "/api/connectors",
            json={
                "slug": slug,
                "title": "ZAI MCP",
                "description": "Z.AI search",
                "connector_type": "stdio",
                "residency_tag": "non-eu",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == slug
        assert body["connector_type"] == "stdio"
        assert body["residency_tag"] == "non-eu"
        assert body["is_public"] is True

        # publish a version
        v = app_and_client.post(
            f"/api/connectors/{slug}/versions",
            json={
                "semver": "1.0.0",
                "config_template": _zai_template(),
                "required_env": ["ZAI_API_KEY"],
                "changelog": "init",
            },
        )
        assert v.status_code == 201, v.text
        vbody = v.json()
        assert vbody["semver"] == "1.0.0"
        # config_template is stored VERBATIM with ${VAR} refs (grep-proof)
        assert vbody["config_template"]["env"]["ZAI_API_KEY"] == "${ZAI_API_KEY}"

    def test_publish_duplicate_slug_409(self, app_and_client: TestClient, db_session: Session) -> None:
        slug = f"dup-{uuid4().hex[:6]}"
        body = {"slug": slug, "title": "T", "connector_type": "stdio"}
        r1 = app_and_client.post("/api/connectors", json=body)
        assert r1.status_code == 201
        r2 = app_and_client.post("/api/connectors", json=body)
        assert r2.status_code == 409

    def test_publish_duplicate_semver_409(self, app_and_client: TestClient) -> None:
        slug = f"sv-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        payload = {
            "semver": "1.0.0",
            "config_template": _zai_template(),
            "required_env": ["ZAI_API_KEY"],
        }
        v1 = app_and_client.post(f"/api/connectors/{slug}/versions", json=payload)
        assert v1.status_code == 201
        v2 = app_and_client.post(f"/api/connectors/{slug}/versions", json=payload)
        assert v2.status_code == 409

    def test_secret_lint_rejects_literal_key_at_publish(self, app_and_client: TestClient) -> None:
        slug = f"leak-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        v = app_and_client.post(
            f"/api/connectors/{slug}/versions",
            json={
                "semver": "1.0.0",
                "config_template": {"command": "x", "env": {"K": "sk-" + "a" * 30}},
                "required_env": [],
            },
        )
        assert v.status_code == 422, v.text
        assert "literal.secret" in v.text or "secret" in v.text

    def test_required_env_consistency_rejected_at_publish(self, app_and_client: TestClient) -> None:
        slug = f"inv-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        v = app_and_client.post(
            f"/api/connectors/{slug}/versions",
            json={
                "semver": "1.0.0",
                "config_template": {"command": "x"},
                "required_env": ["MISSING_VAR"],
            },
        )
        assert v.status_code == 422

    def test_anonymous_write_401(self, db_session: Session, monkeypatch) -> None:
        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        with TestClient(app) as anon:  # no x-api-key
            r = anon.post("/api/connectors", json={"slug": "x", "title": "y", "connector_type": "stdio"})
            assert r.status_code == 401


# ─────────────────────────── browse + detail ───────────────────────────────


class TestBrowseDetail:
    def test_public_browse_anonymous(self, db_session: Session, monkeypatch) -> None:
        # Seed a connector directly
        c = Connector(
            slug=f"pub-{uuid4().hex[:6]}",
            title="Pub",
            connector_type="stdio",
            is_public=True,
        )
        db_session.add(c)
        db_session.commit()

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        with TestClient(app) as anon:  # no api key — must still see browse
            r = anon.get("/api/connectors")
            assert r.status_code == 200
            slugs = [row["slug"] for row in r.json()["results"]]
            assert c.slug in slugs

    def test_detail_public_anonymous(self, db_session: Session, monkeypatch) -> None:
        c = Connector(
            slug=f"d-{uuid4().hex[:6]}",
            title="D",
            connector_type="stdio",
            is_public=True,
        )
        db_session.add(c)
        db_session.commit()
        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        with TestClient(app) as anon:
            r = anon.get(f"/api/connectors/{c.slug}")
            assert r.status_code == 200
            assert r.json()["slug"] == c.slug

    def test_detail_404_unknown(self, app_and_client: TestClient) -> None:
        r = app_and_client.get("/api/connectors/no-such-slug")
        assert r.status_code == 404


# ─────────────────────── bundle connector declare ──────────────────────────


@pytest.fixture()
def owner_and_bundle(db_session: Session) -> tuple[User, Bundle]:
    u = User(display_name="cb-owner", email=f"{uuid4().hex[:8]}@t.local")
    db_session.add(u)
    db_session.commit()
    cb = Bundle(name=f"cb-{uuid4().hex[:6]}", bundle_owner=u.id)
    db_session.add(cb)
    db_session.commit()
    return u, cb


class TestBundleDeclare:
    def test_declare_connector_in_bundle(
        self, app_and_client: TestClient, db_session: Session, owner_and_bundle
    ) -> None:
        _u, cb = owner_and_bundle
        slug = f"dc-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        r = app_and_client.post(
            f"/api/bundles/{cb.id}/connectors",
            json={"slug": slug, "pinned_semver": "1.0.0"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == slug
        assert body["pinned_semver"] == "1.0.0"

        # persisted
        rows = db_session.query(BundleConnector).filter(BundleConnector.bundle_id == cb.id).all()
        assert len(rows) == 1

    def test_undeclare_connector(
        self, app_and_client: TestClient, db_session: Session, owner_and_bundle
    ) -> None:
        _u, cb = owner_and_bundle
        slug = f"ud-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        app_and_client.post(f"/api/bundles/{cb.id}/connectors", json={"slug": slug})
        d = app_and_client.delete(f"/api/bundles/{cb.id}/connectors/{slug}")
        assert d.status_code == 200
        rows = db_session.query(BundleConnector).filter(BundleConnector.bundle_id == cb.id).all()
        assert len(rows) == 0


# ─────────────────── reconcile: connectors diff section ────────────────────


class TestReconcileDiff:
    def test_reconcile_diff_has_connectors_section(
        self, app_and_client: TestClient, db_session: Session, owner_and_bundle
    ) -> None:
        _u, cb = owner_and_bundle
        slug = f"rc-{uuid4().hex[:6]}"
        app_and_client.post(
            "/api/connectors",
            json={"slug": slug, "title": "T", "connector_type": "stdio"},
        )
        app_and_client.post(
            f"/api/connectors/{slug}/versions",
            json={
                "semver": "1.0.0",
                "config_template": _zai_template(),
                "required_env": ["ZAI_API_KEY"],
            },
        )
        app_and_client.post(
            f"/api/bundles/{cb.id}/connectors",
            json={"slug": slug},
        )

        r = app_and_client.post(
            f"/api/bundles/{cb.id}/reconcile",
            json={"local": [], "dry_run": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "connectors" in body["diff"], body
        # The declared connector is an ADD when local_connectors is empty/absent
        adds = body["diff"]["connectors"].get("add", [])
        assert any(a["slug"] == slug for a in adds)

    def test_local_connectors_in_post_body_additive(
        self, app_and_client: TestClient, owner_and_bundle
    ) -> None:
        """local_connectors absent must not break the existing skill reconcile
        (backward-compat). When present it is consumed additively."""
        _u, cb = owner_and_bundle
        # No connectors declared; just confirm the request still works.
        r = app_and_client.post(
            f"/api/bundles/{cb.id}/reconcile",
            json={"local": [], "local_connectors": [], "dry_run": True},
        )
        assert r.status_code == 200
        assert "connectors" in r.json()["diff"]


# ─────────── generation bump (304-invalidation regression) ─────────────────


class TestGenerationBumpConnectors:
    """Phase 0 bug-4 class: publishing a ConnectorVersion MUST bump declaring
    bundles' generation so the 304 fast-path breaks and agents see the new
    version. Mirrors tests/test_activate0701_generation_bump.py."""

    def test_new_connector_version_bumps_declaring_bundle(
        self, db_session: Session, owner_and_bundle
    ) -> None:
        _u, cb = owner_and_bundle
        c = Connector(
            slug=f"gb-{uuid4().hex[:6]}",
            title="T",
            connector_type="stdio",
            is_public=True,
        )
        db_session.add(c)
        db_session.commit()
        db_session.add(
            ConnectorVersion(
                connector_id=c.id,
                semver="1.0.0",
                config_template=_zai_template(),
                required_env=["ZAI_API_KEY"],
            )
        )
        db_session.add(BundleConnector(bundle_id=cb.id, connector_id=c.id, pinned_semver="1.0.0"))
        db_session.commit()

        before = cb.updated_at or datetime.utcnow()
        before = before - timedelta(hours=1)
        db_session.query(Bundle).filter(Bundle.id == cb.id).update(
            {"updated_at": before}, synchronize_session=False
        )
        db_session.commit()

        # Publish a NEW version (the second one)
        db_session.add(
            ConnectorVersion(
                connector_id=c.id,
                semver="1.0.1",
                config_template=_zai_template(),
                required_env=["ZAI_API_KEY"],
                changelog="bump",
            )
        )
        bumped = _bump_for_connector(db_session, c.id)
        db_session.commit()
        db_session.refresh(cb)

        assert bumped >= 1
        assert cb.updated_at is not None
        assert cb.updated_at > before, (
            "ConnectorVersion publish MUST advance declaring bundles' generation "
            "or the 304 fast-path makes the update invisible to polling agents"
        )

    def test_unrelated_bundle_not_bumped(self, db_session: Session, owner_and_bundle) -> None:
        _u, cb = owner_and_bundle
        # Second bundle that does NOT declare the connector
        other = Bundle(name=f"other-{uuid4().hex[:6]}", bundle_owner=_u.id)
        db_session.add(other)
        db_session.commit()
        before_other = other.updated_at

        c = Connector(
            slug=f"uo-{uuid4().hex[:6]}",
            title="T",
            connector_type="stdio",
            is_public=True,
        )
        db_session.add(c)
        db_session.commit()
        db_session.add(
            ConnectorVersion(
                connector_id=c.id,
                semver="1.0.0",
                config_template=_zai_template(),
                required_env=["ZAI_API_KEY"],
            )
        )
        db_session.add(BundleConnector(bundle_id=cb.id, connector_id=c.id))

        db_session.commit()
        _bump_for_connector(db_session, c.id)
        db_session.commit()
        db_session.refresh(other)
        assert other.updated_at == before_other, "unrelated bundle must not be bumped"
