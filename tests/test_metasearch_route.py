"""Integration tests for GET /api/skills/metasearch (metasearch_0710 P0).

Covers: one unified ranked list (no namespace split), curated + external merge,
ClawHub non-deployable end-to-end (condition 2b), funnel event emission (§1.5.4),
public access with no api-key (Mom-test cold stranger), and no-network hermetic
fan-out (LIVE_FETCH monkeypatched).
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _clear_metasearch_cache():
    """Isolation: clear the module-level hot-query cache before each test."""
    from app.services.metasearch_cache import get_cache

    get_cache().invalidate()


from fastapi.testclient import TestClient

import app.services.federation_live as fl
from app.models import Skill, TelemetryEvent


def _seed_curated(db, slug: str, title: str, installs: int = 0):
    s = Skill(
        slug=slug,
        title=title,
        description=f"{title} desc",
        is_public=True,
        is_archived=False,
        install_count=installs,
        skill_variant="custom",
        kind="skill",
    )
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
        fl,
        "_safe_json_get",
        lambda url, *, params=None, headers=None: {"items": claw_rows},
    )
    fl._cache.clear()


def setup_function(_):
    fl._cache.clear()
    from app.services import metasearch_ratelimit as rl

    rl.reset_all()


def test_metasearch_returns_one_ranked_list(client, db_session, monkeypatch):
    _seed_curated(db_session, "curated-writer", "Curated Writer", installs=5)
    _fake_fanout(
        monkeypatch,
        {
            "skills-sh": [{"id": "o/r/pop", "name": "Popular Ext", "installs": 9999, "source": "o/r"}],
        },
    )
    resp = client.get("/api/skills/metasearch?q=writer")
    assert resp.status_code == 200
    body = resp.json()
    # the intact seam — one flat list, no internal/external split keys
    assert "skills" in body and "internal" not in body and "external" not in body
    assert body["result_count"] >= 1
    assert "source_count" in body


def test_clawhub_result_is_searchable_but_not_deployable(client, db_session, monkeypatch):
    _fake_fanout(
        monkeypatch,
        {
            "clawhub": [{"slug": "claw-skill", "displayName": "Claw Skill", "stats": {"downloads": 42}}],
        },
    )
    resp = client.get("/api/skills/metasearch?q=claw")
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    claw = [s for s in skills if s["source"] == "clawhub"]
    assert claw, "clawhub must be searchable (present in results)"
    assert claw[0]["deployable"] is False, "clawhub must NOT be fleet-deployable in v1 (condition 2b)"


def test_curated_carries_quality_chip_and_outranks_equal_external(client, db_session, monkeypatch):
    _seed_curated(db_session, "cur-tie", "Tie Curated", installs=1)
    _fake_fanout(
        monkeypatch,
        {
            "skills-sh": [{"id": "e/r/tie", "name": "Tie External", "installs": 1, "source": "e/r"}],
        },
    )
    resp = client.get("/api/skills/metasearch?q=tie")
    skills = resp.json()["skills"]
    cur = next(s for s in skills if s["source"] == "recipes")
    assert cur["quality"] == "curated"
    # curated wins the tie (both single-item sources → 0.5 prior + curated boost)
    assert skills[0]["source"] == "recipes"


def test_funnel_event_is_recorded(client, db_session, monkeypatch):
    _fake_fanout(
        monkeypatch,
        {
            "skills-sh": [{"id": "o/r/x", "name": "X", "installs": 3, "source": "o/r"}],
        },
    )
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


# ── §5 render contract (P2) ──────────────────────────────────────────────────


def test_metasearch_response_carries_render_contract(client, db_session, monkeypatch):
    """P2: the metasearch response carries a render_contract meta block (§5.1/Q2/§5.5)."""
    import app.services.metasearch_fanout as fo

    monkeypatch.setattr(
        fo, "fan_out", lambda *a, **k: fo.FanoutOutput(pairs=[], sources_ok=[], sources_degraded=[])
    )
    resp = client.get("/api/skills/metasearch?q=x")
    assert resp.status_code == 200
    rc = resp.json()["render_contract"]
    assert rc["one_ranked_list"] is True
    assert rc["catalog_count_shown"] is False  # Spotify model — no count
    assert "latency_ms" in rc and "within_budget" in rc


def test_metasearch_cards_carry_badge_chip_action(client, db_session, monkeypatch):
    """P2: every returned card carries the §5 contract fields."""
    import app.services.metasearch_fanout as fo

    monkeypatch.setattr(
        fo, "fan_out", lambda *a, **k: fo.FanoutOutput(pairs=[], sources_ok=[], sources_degraded=[])
    )
    # seed one curated skill so there's at least one card
    from app.models import Skill

    db_session.add(
        Skill(
            slug="c1",
            title="C1",
            description="d",
            readme="# b",
            tier="free",
            is_public=True,
            is_archived=False,
            skill_variant="custom",
            kind="skill",
        )
    )
    db_session.commit()
    resp = client.get("/api/skills/metasearch?q=C1")
    cards = resp.json()["skills"]
    if cards:  # curated match present
        card = cards[0]
        assert "source_badge" in card
        assert card["quality_chip"]["tone"] in ("gold", "neutral")
        assert card["primary_action"] in ("deploy_to_fleet", "preview_install")
        assert card["actionable"] is True


def test_metasearch_no_stored_count_field(client, db_session, monkeypatch):
    """Q2 Spotify model: the response must NOT expose a total catalog count."""
    import app.services.metasearch_fanout as fo

    monkeypatch.setattr(
        fo, "fan_out", lambda *a, **k: fo.FanoutOutput(pairs=[], sources_ok=[], sources_degraded=[])
    )
    resp = client.get("/api/skills/metasearch?q=x")
    body = resp.json()
    # result_count is the count of THIS page's returned cards (fine); there must be
    # no total/catalog/available count implying a stored inventory size.
    for banned in ("total_count", "catalog_count", "total_skills", "available_count"):
        assert banned not in body, f"{banned} leaks a stored count (Q2 violation)"


# ── §7 hot-query cache (P5) ───────────────────────────────────────────────────


def test_metasearch_response_carries_cache_metadata(client, db_session, monkeypatch):
    """Every metasearch response carries a `cache` block (hit or miss)."""
    _fake_fanout(monkeypatch, {"browse-sh": [{"slug": "s", "name": "S", "title": "S"}]})
    from app.services.metasearch_cache import get_cache

    get_cache().invalidate()  # clean slate
    resp = client.get("/api/skills/metasearch?q=browser")
    assert resp.status_code == 200
    body = resp.json()
    assert "cache" in body
    assert body["cache"]["cache_hit"] is False  # first call = miss


def test_second_search_for_same_query_is_cache_hit(client, db_session, monkeypatch):
    """§7: the second search for "browser" within TTL is a cache hit — no fan-out."""
    _fake_fanout(monkeypatch, {"browse-sh": [{"slug": "s", "name": "S", "title": "S"}]})
    from app.services.metasearch_cache import get_cache

    get_cache().invalidate()
    client.get("/api/skills/metasearch?q=browser")  # miss → fan-out → cache
    resp2 = client.get("/api/skills/metasearch?q=browser")  # hit
    assert resp2.status_code == 200
    assert resp2.json()["cache"]["cache_hit"] is True
    assert resp2.json()["cache"]["cache_age_s"] >= 0


def test_cache_hit_returns_same_skills_as_first_call(client, db_session, monkeypatch):
    """A cache hit returns the same ranked skills as the live fan-out did."""
    _fake_fanout(monkeypatch, {"browse-sh": [{"slug": "s", "name": "S", "title": "S"}]})
    from app.services.metasearch_cache import get_cache

    get_cache().invalidate()
    r1 = client.get("/api/skills/metasearch?q=scraper").json()
    r2 = client.get("/api/skills/metasearch?q=scraper").json()
    assert [s["slug"] for s in r1["skills"]] == [s["slug"] for s in r2["skills"]]
