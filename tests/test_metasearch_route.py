"""Integration tests for GET /api/skills/metasearch (metasearch_0710 P0).

Covers: one unified ranked list (no namespace split), curated + external merge,
ClawHub non-deployable end-to-end (condition 2b), funnel event emission (§1.5.4),
public access with no api-key (Mom-test cold stranger), and no-network hermetic
fan-out (LIVE_FETCH monkeypatched).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.services.federation_live as fl
from app.models import Skill, TelemetryEvent


def _seed_curated(db, slug: str, title: str, installs: int = 0):
    s = Skill(slug=slug, title=title, description=f"{title} desc", is_public=True, is_archived=False,
              install_count=installs, skill_variant="custom", kind="skill")
    db.add(s)
    db.commit()
    return s


def _fake_fanout(monkeypatch, mapping: dict):
    """Patch LIVE_FETCH + ClawHub JSON getter so fan_out hits no network."""
    live = {}
    for src, rows in mapping.items():
        if src != "clawhub":
            live[src] = (lambda rows=rows: (lambda _q: rows))()
    monkeypatch.setattr(fl, "LIVE_FETCH", live, raising=True)
    claw_rows = mapping.get("clawhub", [])
    monkeypatch.setattr(
        fl, "_safe_json_get",
        lambda url, *, params=None, headers=None: {"items": claw_rows},
    )
    fl._cache.clear()


def setup_function(_):
    fl._cache.clear()
    from app.services import metasearch_ratelimit as rl
    rl.reset_all()


def test_metasearch_returns_one_ranked_list(client, db_session, monkeypatch):
    _seed_curated(db_session, "curated-writer", "Curated Writer", installs=5)
    _fake_fanout(monkeypatch, {
        "skills-sh": [{"id": "o/r/pop", "name": "Popular Ext", "installs": 9999, "source": "o/r"}],
    })
    resp = client.get("/api/skills/metasearch?q=writer")
    assert resp.status_code == 200
    body = resp.json()
    # the intact seam — one flat list, no internal/external split keys
    assert "skills" in body and "internal" not in body and "external" not in body
    assert body["result_count"] >= 1
    assert "source_count" in body


def test_clawhub_result_is_searchable_but_not_deployable(client, db_session, monkeypatch):
    _fake_fanout(monkeypatch, {
        "clawhub": [{"slug": "claw-skill", "displayName": "Claw Skill", "stats": {"downloads": 42}}],
    })
    resp = client.get("/api/skills/metasearch?q=claw")
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    claw = [s for s in skills if s["source"] == "clawhub"]
    assert claw, "clawhub must be searchable (present in results)"
    assert claw[0]["deployable"] is False, "clawhub must NOT be fleet-deployable in v1 (condition 2b)"


def test_curated_carries_quality_chip_and_outranks_equal_external(client, db_session, monkeypatch):
    _seed_curated(db_session, "cur-tie", "Tie Curated", installs=1)
    _fake_fanout(monkeypatch, {
        "skills-sh": [{"id": "e/r/tie", "name": "Tie External", "installs": 1, "source": "e/r"}],
    })
    resp = client.get("/api/skills/metasearch?q=tie")
    skills = resp.json()["skills"]
    cur = next(s for s in skills if s["source"] == "recipes")
    assert cur["quality"] == "curated"
    # curated wins the tie (both single-item sources → 0.5 prior + curated boost)
    assert skills[0]["source"] == "recipes"


def test_funnel_event_is_recorded(client, db_session, monkeypatch):
    _fake_fanout(monkeypatch, {
        "skills-sh": [{"id": "o/r/x", "name": "X", "installs": 3, "source": "o/r"}],
    })
    resp = client.get("/api/skills/metasearch?q=funnel")
    assert resp.status_code == 200
    events = db_session.query(TelemetryEvent).filter(TelemetryEvent.event_type == "metasearch.query").all()
    assert len(events) == 1, "one metasearch.query funnel event must be written (§1.5.4)"
    payload = json.loads(events[0].payload)
    assert payload["q"] == "funnel"
    assert "external_count" in payload and "deployable_count" in payload
    assert "sources_ok" in payload


def test_metasearch_degrades_when_a_source_errors(client, db_session, monkeypatch):
    _fake_fanout(monkeypatch, {"browse-sh": [{"slug": "ok", "name": "OK", "title": "OK"}]})

    def _boom(_q):
        raise RuntimeError("down")

    fl.LIVE_FETCH["skills-sh"] = _boom
    resp = client.get("/api/skills/metasearch?q=x")
    assert resp.status_code == 200, "a source failure must NOT 500 the route"
    body = resp.json()
    assert "skills-sh" in body["sources_degraded"]


def test_no_stored_catalog_count_spotify_model(client, db_session, monkeypatch):
    _fake_fanout(monkeypatch, {"skills-sh": [{"id": "o/r/x", "name": "X", "installs": 1, "source": "o/r"}]})
    body = client.get("/api/skills/metasearch?q=x").json()
    for banned in ("total", "indexed", "catalog_size", "external_indexed"):
        assert banned not in body, f"Spotify model (Q2): no stored count — '{banned}' must be absent"


# ── public-access regression (Mom-test cold stranger, real middleware) ───────


@pytest.fixture
def mw_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def test_metasearch_is_public_no_api_key(mw_client, monkeypatch):
    """A cold stranger with NO x-api-key must be able to metasearch."""
    _fake_fanout(monkeypatch, {"skills-sh": [{"id": "o/r/x", "name": "X", "installs": 1, "source": "o/r"}]})
    resp = mw_client.get("/api/skills/metasearch?q=x")  # no headers
    assert resp.status_code == 200, f"metasearch must be public, got {resp.status_code}"
    assert "skills" in resp.json()
