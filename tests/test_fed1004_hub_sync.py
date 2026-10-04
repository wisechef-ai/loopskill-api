"""fed1004 — change-driven Hermes Hub snapshot sync.

The hub index was ingested once a day while upstream rebuilds it several times
a day. ``sync_hub_snapshot`` sends If-None-Match with the ETag of the last good
ingest: a 304 costs one request and touches no row; a 200 re-ingests atomically
and stores the new ETag; a failure keeps both the index and the ETag.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import FederationHubSkill, FederationIndexCache
from app.services.hub_snapshot_sync import sync_hub_snapshot
from tests.test_spotify_1507_c2_hub_snapshot import _make_snapshot


class _Resp:
    def __init__(self, status: int, data: dict | None = None, etag: str | None = None):
        self.status_code = status
        self.content = json.dumps(data or {}).encode()
        self.headers = {"etag": etag} if etag else {}

    def iter_bytes(self, **kw):
        yield self.content


class _Upstream:
    """A fake GitHub Pages: honours If-None-Match against its current ETag."""

    def __init__(self, data: dict, etag: str):
        self.data, self.etag = data, etag
        self.calls: list[dict | None] = []

    def __call__(self, url, *, timeout=None, headers=None):
        self.calls.append(headers)
        if headers and headers.get("If-None-Match") == self.etag:
            return _Resp(304)
        return _Resp(200, self.data, self.etag)


def _row(db) -> FederationIndexCache:
    db.expire_all()
    return db.get(FederationIndexCache, "hermes-hub")


def test_first_sync_ingests_fully_and_stores_the_etag(db_session):
    up = _Upstream(_make_snapshot(), '"etag-1"')
    report = sync_hub_snapshot(db_session, _get=up)
    assert report["status"] == "ok" and report["indexed"] == 10
    assert up.calls == [None], "no ETag yet → an unconditional fetch"
    assert _row(db_session).upstream_etag == '"etag-1"'
    assert db_session.query(FederationHubSkill).count() == 10


def test_unchanged_upstream_is_a_304_that_touches_no_row(db_session):
    up = _Upstream(_make_snapshot(), '"etag-1"')
    sync_hub_snapshot(db_session, _get=up)
    row = _row(db_session)
    row.walked_at = datetime.now(timezone.utc) - timedelta(hours=5)
    ids_before = sorted(r.id for r in db_session.query(FederationHubSkill).all())
    db_session.commit()

    report = sync_hub_snapshot(db_session, _get=up)

    assert report["status"] == "unchanged"
    assert report["indexed"] == 10
    assert up.calls[-1] == {"If-None-Match": '"etag-1"'}
    # Same row ids = no delete-and-reinsert happened.
    assert sorted(r.id for r in db_session.query(FederationHubSkill).all()) == ids_before
    # The index IS current, so the freshness clock moves.
    walked = _row(db_session).walked_at
    walked = walked if walked.tzinfo else walked.replace(tzinfo=timezone.utc)
    assert datetime.now(timezone.utc) - walked < timedelta(minutes=1)


def test_changed_upstream_reingests_and_rotates_the_etag(db_session):
    up = _Upstream(_make_snapshot(), '"etag-1"')
    sync_hub_snapshot(db_session, _get=up)
    changed = _make_snapshot()
    changed["skills"] = changed["skills"][:7]
    changed["skill_count"] = 7
    up.data, up.etag = changed, '"etag-2"'

    report = sync_hub_snapshot(db_session, _get=up)

    assert report["status"] == "ok" and report["indexed"] == 7
    assert _row(db_session).upstream_etag == '"etag-2"'
    assert db_session.query(FederationHubSkill).count() == 7


@pytest.mark.parametrize("failure", ["raise", 500, 404])
def test_a_failed_fetch_keeps_the_index_and_the_etag(db_session, failure):
    up = _Upstream(_make_snapshot(), '"etag-1"')
    sync_hub_snapshot(db_session, _get=up)

    def _broken(url, *, timeout=None, headers=None):
        if failure == "raise":
            raise ConnectionError("pages down")
        return _Resp(failure)

    report = sync_hub_snapshot(db_session, _get=_broken)

    assert report["status"] == "error"
    assert _row(db_session).upstream_etag == '"etag-1"'
    assert db_session.query(FederationHubSkill).count() == 10


def test_a_failed_ingest_does_not_store_the_new_etag(db_session):
    """A 200 whose body cannot be ingested must not record its ETag — or the
    next run would 304 against an index that never received that content."""
    up = _Upstream(_make_snapshot(), '"etag-1"')
    sync_hub_snapshot(db_session, _get=up)

    def _garbage(url, *, timeout=None, headers=None):
        resp = _Resp(200, None, '"etag-bad"')
        resp.content = b"{not json"
        return resp

    report = sync_hub_snapshot(db_session, _get=_garbage)

    assert report["status"] == "error"
    assert _row(db_session).upstream_etag == '"etag-1"'
    assert db_session.query(FederationHubSkill).count() == 10


def test_an_etag_without_a_good_ingest_is_never_trusted(db_session):
    db_session.add(FederationIndexCache(source="hermes-hub", indexed_count=None, upstream_etag='"etag-1"'))
    db_session.commit()
    up = _Upstream(_make_snapshot(), '"etag-1"')
    report = sync_hub_snapshot(db_session, _get=up)
    assert up.calls == [None], "an untrusted ETag must not be sent"
    assert report["status"] == "ok" and report["indexed"] == 10


def test_reindex_cli_routes_hermes_hub_through_the_conditional_sync(monkeypatch, db_session):
    import sys

    import app.database as database
    import scripts.federation_reindex as fr

    seen: list[str] = []
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.services.hub_snapshot_sync.sync_hub_snapshot",
        lambda db: seen.append("sync") or {"status": "unchanged", "indexed": 10},
    )
    monkeypatch.setattr(fr, "reindex_source", lambda db, src, dry_run: pytest.fail("forced ingest ran"))
    monkeypatch.setattr(sys, "argv", ["federation_reindex.py", "--source", "hermes-hub", "--if-changed"])
    fr.main()
    assert seen == ["sync"]


def test_a_failed_conditional_sync_is_a_nonzero_exit(monkeypatch, db_session):
    """R1 #11: an hourly job that always exits 0 reports health while the index
    stays stale."""
    import sys

    import app.database as database
    import scripts.federation_reindex as fr

    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.services.hub_snapshot_sync.sync_hub_snapshot",
        lambda db: {"status": "error", "indexed": None, "error": "unexpected status 500"},
    )
    monkeypatch.setattr(sys, "argv", ["federation_reindex.py", "--source", "hermes-hub", "--if-changed"])
    assert fr.main() == 1
