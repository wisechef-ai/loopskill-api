"""Self-contained test for transparency router."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.transparency_routes import router as transparency_router
from app.database import get_db


@pytest.fixture()
def client(db_session, monkeypatch):
    # Hermeticity: _compute_median_issue_resolution_h() makes a REAL network
    # call to api.github.com whenever GITHUB_TOKEN/GITHUB_DISPATCH_PAT is
    # present in the environment (see transparency_routes.py). Any dev/CI
    # shell that happens to export a GitHub token (gh auth, agentic runners)
    # turns every test in this file into a live network test, which
    # conftest.py's net_guard correctly blocks -- but as a hard failure, not
    # a skip. Strip both so this suite's result never depends on what the
    # invoking environment happens to have exported.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_DISPATCH_PAT", raising=False)

    app = FastAPI()
    app.include_router(transparency_router)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_transparency_returns_200(client):
    r = client.get("/api/health/transparency")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "install_count_drift",
        # t_61421779: a quotient is never published alone — the base and the
        # numerator ship beside the rate so 0.0-over-nothing can never again be
        # mistaken for 0.0-over-thousands.
        "skill_error_count_7d",
        "installs_7d",
        "skill_error_rate_7d",
        "feedback_volume_7d",
        "median_issue_resolution_h",
        "computed_at",
        "ttl_seconds",
    ):
        assert key in body, key
    assert body["ttl_seconds"] == 60
    assert isinstance(body["install_count_drift"], int)
    assert isinstance(body["skill_error_count_7d"], int)
    assert isinstance(body["installs_7d"], int)
    assert 0.0 <= body["skill_error_rate_7d"] <= 1.0


def test_transparency_no_auth(client):
    r = client.get("/api/health/transparency")
    assert r.status_code == 200


def test_transparency_cached(client):
    r1 = client.get("/api/health/transparency").json()
    r2 = client.get("/api/health/transparency").json()
    assert r1["computed_at"] == r2["computed_at"]


def test_health_status_returns_200(client):
    """spotify_1507 Ph0: bare GET /api/health is public, always-200, no DB."""
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "loopskill-api"
    assert "version" in body


def test_health_status_no_db_dependency(client):
    """Liveness must not depend on the DB session — status page semantics."""
    app = FastAPI()
    app.include_router(transparency_router)
    # deliberately NO get_db override — the route must still 200
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/api/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


# ── t_a1edaf10: last_backfill_at must never present an old timestamp as fresh ──
#
# The drift probe (the only writer of the sentinel) was dead from 2026-07-12 and
# the endpoint kept serving that timestamp with nothing to say it was months old.


def _write_sentinel(tmp_path, monkeypatch, text):
    import app.transparency_routes as tr

    path = tmp_path / "last_backfill_at"
    path.write_text(text)
    monkeypatch.setattr(tr, "BACKFILL_SENTINEL_PATH", str(path))
    return tr


def _reset_cache():
    import app.transparency_routes as tr

    tr._cache_payload = None


def test_fresh_sentinel_is_not_stale(client, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    _write_sentinel(tmp_path, monkeypatch, (datetime.now(UTC) - timedelta(minutes=30)).isoformat())
    _reset_cache()
    body = client.get("/api/health/transparency").json()
    assert body["last_backfill_stale"] is False
    assert body["last_backfill_at"] is not None


def test_the_real_prod_timestamp_is_reported_stale(client, tmp_path, monkeypatch):
    """The exact value prod served for months (2026-07-12)."""
    _write_sentinel(tmp_path, monkeypatch, "2026-07-12T11:00:02.505644+00:00")
    _reset_cache()
    body = client.get("/api/health/transparency").json()
    assert body["last_backfill_stale"] is True
    assert body["last_backfill_at"].startswith("2026-07-12"), "the value is shown, never hidden or rewritten"


def test_missing_sentinel_is_stale_with_null_timestamp(client, tmp_path, monkeypatch):
    import app.transparency_routes as tr

    monkeypatch.setattr(tr, "BACKFILL_SENTINEL_PATH", str(tmp_path / "nope"))
    _reset_cache()
    body = client.get("/api/health/transparency").json()
    assert body["last_backfill_at"] is None
    assert body["last_backfill_stale"] is True


@pytest.mark.parametrize("garbage", ["", "not-a-date", "  \n", "2026-13-45T99:99"])
def test_unparseable_sentinel_is_stale_not_a_500(client, tmp_path, monkeypatch, garbage):
    _write_sentinel(tmp_path, monkeypatch, garbage)
    _reset_cache()
    r = client.get("/api/health/transparency")
    assert r.status_code == 200
    assert r.json()["last_backfill_stale"] is True


def test_stale_boundary_is_24h():
    from datetime import UTC, datetime, timedelta

    from app.transparency_routes import _backfill_is_stale

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert _backfill_is_stale(now - timedelta(hours=23, minutes=59), now) is False
    assert _backfill_is_stale(now - timedelta(hours=24, seconds=1), now) is True
    assert _backfill_is_stale(now - timedelta(hours=24), now) is False, "exactly 24h is still inside the window"


def test_future_timestamp_is_stale_beyond_clock_skew():
    """A far-future stamp would read as fresh forever — it is bogus, not fresh."""
    from datetime import UTC, datetime, timedelta

    from app.transparency_routes import _backfill_is_stale

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert _backfill_is_stale(now + timedelta(minutes=2), now) is False, "small skew tolerated"
    assert _backfill_is_stale(now + timedelta(minutes=5), now) is False, "exactly the skew limit is tolerated"
    assert _backfill_is_stale(now + timedelta(minutes=5, seconds=1), now) is True
    assert _backfill_is_stale(now + timedelta(days=400), now) is True


def test_naive_timestamp_is_read_as_utc_not_a_crash():
    from datetime import UTC, datetime, timedelta

    from app.transparency_routes import _backfill_is_stale

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert _backfill_is_stale((now - timedelta(hours=1)).replace(tzinfo=None), now) is False
    assert _backfill_is_stale((now - timedelta(days=3)).replace(tzinfo=None), now) is True


def test_none_is_stale():
    from app.transparency_routes import _backfill_is_stale

    assert _backfill_is_stale(None) is True
