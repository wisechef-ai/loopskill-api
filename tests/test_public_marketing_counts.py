"""Public marketing-snapshot reachability counts (feat/public-marketing-counts).

The portal (app.loopskill.io) wants to advertise three numbers WITHOUT baking
a build-time API key into client JS: (1) total reachable skills including
the federated superset, (2) personalities count, (3) connectors/MCP count.
This is served PURELY ADDITIVELY from the already-public
``GET /api/marketing/snapshot`` (allow-listed in
``app.middleware._public_paths.PUBLIC_PREFIXES`` via the ``/api/marketing/``
prefix — no middleware change needed).

Tests assert:
  (a) the endpoint returns 200 with NO x-api-key header
  (b) the new count fields are present and are non-negative ints
  (c) the pre-existing keys (skills_total, mcp_tools_count) are unchanged —
      an additive-contract test
  (d) a private/archived Personality/Connector row does NOT leak into the
      new public counts
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.marketing_routes import router as marketing_router
from app.middleware import APIKeyMiddleware
from app.models import Connector, FederationIndexCache, Personality


def _app(db_session, monkeypatch):
    """Minimal app: marketing_router + the REAL APIKeyMiddleware.

    Mirrors tests/_app_factory.py's SessionLocal-repoint trick so the
    middleware's own DB session sees the same (uncommitted) rows the test
    creates, and so this test genuinely exercises the production
    allow-list decision — not a stub.
    """

    class _NoCloseProxy:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def close(self):
            pass

    monkeypatch.setattr("app.database.SessionLocal", lambda: _NoCloseProxy(db_session))

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)
    app.include_router(marketing_router)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=True)


def _make_personality(db, *, slug, is_public=True, is_archived=False):
    p = Personality(
        id=uuid4(),
        slug=slug,
        title=slug,
        is_public=is_public,
        is_archived=is_archived,
        system_prompt="prompt",
    )
    db.add(p)
    db.flush()
    return p


def _make_connector(db, *, slug, is_public=True, is_archived=False):
    c = Connector(
        id=uuid4(),
        slug=slug,
        title=slug,
        connector_type="stdio",
        is_public=is_public,
        is_archived=is_archived,
    )
    db.add(c)
    db.flush()
    return c


def _make_federation_cache_row(db, *, source, indexed_count, deduped_indexed_count=None):
    row = FederationIndexCache(
        source=source,
        indexed_count=indexed_count,
        installable_count=0,
        deduped_indexed_count=deduped_indexed_count,
    )
    db.add(row)
    db.flush()
    return row


# ── (a) public, no auth header ──────────────────────────────────────────────


def test_snapshot_returns_200_with_no_api_key_header(db_session, monkeypatch):
    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")  # deliberately NO x-api-key header
    assert resp.status_code == 200, f"expected public 200, got {resp.status_code}: {resp.text}"


# ── (b) new fields present and non-negative ints ────────────────────────────


def test_new_reachability_fields_are_present_and_nonnegative(db_session, monkeypatch):
    _make_personality(db_session, slug="pub-pers-1")
    _make_connector(db_session, slug="pub-conn-1")
    _make_federation_cache_row(
        db_session, source="hermes-hub", indexed_count=91000, deduped_indexed_count=83000
    )
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    assert resp.status_code == 200
    counts = resp.json()["counts"]

    for key in (
        "federated_skills_total",
        "personalities_total",
        "connectors_total",
        "total_reachable_skills",
    ):
        assert key in counts, f"missing new field {key!r} in {counts!r}"
        assert (
            isinstance(counts[key], int) and counts[key] >= 0
        ), f"{key} must be a non-negative int, got {counts[key]!r}"

    assert counts["federated_skills_total"] == 83000  # deduped value, not raw 91000
    assert counts["personalities_total"] >= 1
    assert counts["connectors_total"] >= 1
    assert counts["total_reachable_skills"] == counts["skills_total"] + counts["federated_skills_total"]


# ── (c) additive contract — pre-existing keys unchanged ─────────────────────


def test_preexisting_keys_unchanged_by_additive_change(db_session, monkeypatch):
    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    assert resp.status_code == 200
    counts = resp.json()["counts"]

    # These keys existed before this change and must still exist, with the
    # same meaning (skills_total = public non-archived Skill count;
    # mcp_tools_count = live MCP tool registry size).
    assert "skills_total" in counts
    assert "mcp_tools_count" in counts
    assert isinstance(counts["skills_total"], int)
    assert isinstance(counts["mcp_tools_count"], int)


# ── (d) private/archived rows never leak into the public counts ────────────


def test_private_personality_not_counted(db_session, monkeypatch):
    _make_personality(db_session, slug="secret-pers", is_public=False)
    _make_personality(db_session, slug="visible-pers", is_public=True)
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    counts = resp.json()["counts"]
    assert (
        counts["personalities_total"] == 1
    ), f"private personality leaked into public count: {counts['personalities_total']!r}"


def test_archived_personality_not_counted(db_session, monkeypatch):
    _make_personality(db_session, slug="archived-pers", is_public=True, is_archived=True)
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    counts = resp.json()["counts"]
    assert (
        counts["personalities_total"] == 0
    ), f"archived personality leaked into public count: {counts['personalities_total']!r}"


def test_private_connector_not_counted(db_session, monkeypatch):
    _make_connector(db_session, slug="secret-conn", is_public=False)
    _make_connector(db_session, slug="visible-conn", is_public=True)
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    counts = resp.json()["counts"]
    assert (
        counts["connectors_total"] == 1
    ), f"private connector leaked into public count: {counts['connectors_total']!r}"


def test_archived_connector_not_counted(db_session, monkeypatch):
    _make_connector(db_session, slug="archived-conn", is_public=True, is_archived=True)
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    counts = resp.json()["counts"]
    assert (
        counts["connectors_total"] == 0
    ), f"archived connector leaked into public count: {counts['connectors_total']!r}"


def test_federated_dedupe_excludes_directly_indexed_overlap(db_session, monkeypatch):
    """A source with only a raw indexed_count (no dedupe) still contributes,
    but a hermes-hub-style row with deduped_indexed_count set uses THAT
    value, never the inflated raw count — this is the honest-count
    discipline documented in app/services/federation_cache.py.
    """
    _make_federation_cache_row(
        db_session, source="hermes-hub", indexed_count=91000, deduped_indexed_count=83000
    )
    _make_federation_cache_row(db_session, source="skills-sh", indexed_count=8000)  # no dedupe field
    db_session.commit()

    client = _app(db_session, monkeypatch)
    resp = client.get("/api/marketing/snapshot")
    counts = resp.json()["counts"]
    assert counts["federated_skills_total"] == 83000 + 8000
