"""MCP ``loopskill_search`` appends federated rows from the shared cache — P2.

The bug this closes, verified live 2026-09-08: ``loopskill_search("graft")``
returned ``{"results": [], "total": 0}`` while the metasearch fan-out found
``skills-sh:trailhq--graft--graft`` and ``loopskill_install`` installed it
fine. Every MCP-facing agent therefore concluded LoopSkill has nothing on any
federated topic — the discovery surface disagreed with the install surface.

The fix is deliberately narrow: the native pass runs EXACTLY as before, then
federated rows are read from the P1 shared SWR cache (``get_entry`` — cache
ONLY, never ``get_or_compute``, never ``fan_out``) and appended.

The invariants these tests pin, in the order they matter:

1. **Never a live fan-out on the MCP thread** (failure mode F1: a >90s cold
   fan-out makes every agent report LoopSkill as broken). A cache miss is a
   normal, fast answer.
2. **Honest freshness**: ``federated`` is ``fresh``/``stale``/``cold``/``degraded``
   and never dresses a miss or a Redis outage up as a result.
3. **Native first, always.** Federated rows append; they never interleave.
4. **The licence gate is the SOURCE's verdict, not the cache's claim.** A
   forged ``deployable: true`` planted in the shared cache cannot promote a
   deep-link / non-redistributable row.
5. **Context discipline**: the append is capped and compact, so it cannot blow
   up the calling agent's context window.
6. **Backward compatible**: ``results`` / ``total`` / ``backend`` /
   ``hybrid_augmented`` keep their exact pre-P2 meaning and type.
"""

from __future__ import annotations

import json

import pytest

from app.mcp.tools.search import loopskill_search
from app.models import MissingSkillQuery
from app.services.metasearch_cache import get_cache
from app.services.metasearch_cache_l2 import RedisL2
from app.services.mcp_federated_search import (
    FEDERATED_APPEND_MAX_BYTES,
    FEDERATED_DEFAULT_CAP,
    FEDERATED_MAX_CAP,
    federated_sources,
)
from tests.conftest import make_skill


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_shared_cache():
    """The cache is a module singleton (one per worker process) — reset it around
    every test so a warm entry can never leak between cases."""
    cache = get_cache()
    original_l2 = cache.l2
    cache.l2 = None  # L1-only by default: no test may depend on a live Redis
    # ``_l2_down`` is deliberately STICKY between L2 interactions (P1: a fresh
    # L1 hit must still report honestly while Redis is down), so it has to be
    # cleared explicitly or one degraded-tier test poisons every later one.
    cache._l2_down = False
    cache.invalidate(None)
    yield cache
    cache.invalidate(None)
    cache._l2_down = False
    cache.l2 = original_l2


def _card(
    *,
    slug: str,
    title: str,
    source: str = "skills-sh",
    install_path: str = "fetch_origin",
    deployable: bool = True,
    origin_url: str | None = None,
    license_id: str | None = "MIT",
    **extra,
) -> dict:
    """One cached metasearch card, shaped exactly as the REST route stores it
    (``UnifiedSkill.to_dict`` merged with ``CardContract.to_dict``)."""
    row = {
        "canonical_id": f"{source}:{slug}",
        "slug": slug,
        "title": title,
        "description": f"{title} — a federated skill",
        "source": source,
        "origin_url": origin_url or f"https://github.com/{slug.replace('--', '/')}",
        "install_ref": f"{source}:{slug}",
        "quality": "community",
        "deployable": deployable,
        "install_path": install_path,
        "popularity": 12,
        "license": license_id,
        "updated_at": None,
        "rank_score": 0.5,
        "source_badge": "skills.sh",
        "quality_chip": {"label": "Community", "tone": "neutral"},
        "primary_action": "deploy_to_fleet",
        "action_label": "",
        "actionable": True,
        "installable": True,
    }
    row.update(extra)
    return row


GRAFT = _card(slug="trailhq--graft--graft", title="graft")
PONYTAIL = _card(slug="sp00ler--ponytail--ponytail", title="ponytail")


def _warm(query: str, rows: list[dict]) -> None:
    """Warm the shared cache the way the REST route does — same public ``put``."""
    get_cache().put(query, federated_sources(), rows, sources_ok=["recipes", "skills-sh"])


def _federated(result: dict) -> list[dict]:
    """The appended federated rows (compact shape carries ``install_ref``)."""
    return [r for r in result["results"] if "install_ref" in r]


# ── 1. The reported bug ───────────────────────────────────────────────────────


def test_warm_cache_graft_returns_installable_federated_row(db_session):
    """The live 2026-09-08 repro: search('graft') must surface the row that
    loopskill_install already accepts."""
    _warm("graft", [GRAFT])

    result = loopskill_search(db_session, query="graft")

    refs = {r["install_ref"] for r in _federated(result)}
    assert "skills-sh:trailhq--graft--graft" in refs
    row = next(r for r in _federated(result) if r["install_ref"] == "skills-sh:trailhq--graft--graft")
    assert row["deployable"] is True
    assert result["federated"] == "fresh"


def test_warm_cache_ponytail_returns_at_least_one_federated_row(db_session):
    _warm("ponytail", [PONYTAIL])

    result = loopskill_search(db_session, query="ponytail")

    assert len(_federated(result)) >= 1


# ── 2. Native-first ordering ──────────────────────────────────────────────────


def test_native_rows_always_precede_federated_rows(db_session):
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()
    _warm("graft", [GRAFT])

    result = loopskill_search(db_session, query="graft")

    native_idx = [i for i, r in enumerate(result["results"]) if "install_ref" not in r]
    fed_idx = [i for i, r in enumerate(result["results"]) if "install_ref" in r]
    assert native_idx and fed_idx
    assert max(native_idx) < min(fed_idx), "federated rows must never interleave above native"


# ── 3. Cold / miss worker: fast, honest, and ZERO upstream ────────────────────


def test_cold_worker_returns_native_plus_cold_flag_and_never_fans_out(db_session, monkeypatch):
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()

    calls: list[str] = []

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        calls.append("fan_out")
        raise AssertionError("MCP search must NEVER fan out (failure mode F1)")

    import app.services.metasearch_fanout as fanout_mod

    monkeypatch.setattr(fanout_mod, "fan_out", _explode)
    monkeypatch.setattr(
        get_cache(),
        "get_or_compute",
        lambda *a, **k: pytest.fail("MCP search must use the cache-ONLY reader"),
    )

    result = loopskill_search(db_session, query="graft")

    assert result["federated"] == "cold"
    assert calls == []
    assert any(r["slug"] == "graft-native" for r in result["results"])
    assert _federated(result) == []


# ── 4. Degraded shared tier ───────────────────────────────────────────────────


def test_redis_down_returns_native_plus_degraded_without_raising(db_session):
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()
    # A client factory that yields None is EXACTLY what app.middleware.get_redis
    # returns while Redis is unreachable (including its 30s backoff window).
    get_cache().l2 = RedisL2(client_factory=lambda: None)

    result = loopskill_search(db_session, query="graft")

    assert result["federated"] == "degraded"
    assert any(r["slug"] == "graft-native" for r in result["results"])


def test_stale_entry_is_flagged_stale_and_still_served(db_session):
    import time as _time

    _warm("graft", [GRAFT])
    cache = get_cache()
    key = cache._key("graft", federated_sources())
    cache._store[key].computed_at = _time.time() - (cache.ttl_s + 1)

    result = loopskill_search(db_session, query="graft")

    assert result["federated"] == "stale"
    assert len(_federated(result)) == 1


# ── 5. The licence gate is the source's verdict, never the cache's claim ─────


def test_deep_link_row_is_never_deployable_even_when_cache_claims_it(db_session):
    forged = _card(
        slug="acme--proprietary--thing",
        title="Proprietary Thing",
        install_path="deep_link",
        deployable=True,  # forged: a poisoned shared entry claiming installability
        license_id=None,
    )
    _warm("graft", [forged])

    result = loopskill_search(db_session, query="graft")

    rows = _federated(result)
    assert len(rows) == 1
    assert rows[0]["deployable"] is False


def test_non_redistributable_fetch_origin_row_is_never_deployable(db_session):
    forged = _card(
        slug="acme--noredist--thing",
        title="No Redist",
        install_path="fetch_origin",
        deployable=True,
        license_id="Proprietary",
        redistributable=False,  # the source's own verdict — it wins
    )
    _warm("graft", [forged])

    rows = _federated(loopskill_search(db_session, query="graft"))
    assert rows[0]["deployable"] is False


def test_source_outside_fleet_allowlist_is_never_deployable(db_session):
    # ClawHub is searchable + ad-hoc-installable but NOT fleet-deployable in v1.
    _warm("graft", [_card(slug="graft", title="graft", source="clawhub", deployable=True)])

    rows = _federated(loopskill_search(db_session, query="graft"))
    assert rows[0]["deployable"] is False


# ── 6. Context discipline: compact shape + hard cap ──────────────────────────


def test_federated_rows_are_compact_and_carry_only_the_agreed_fields(db_session):
    _warm("graft", [GRAFT])

    rows = _federated(loopskill_search(db_session, query="graft"))

    assert set(rows[0]) == {
        "slug",
        "title",
        "install_ref",
        "deployable",
        "install_path",
        "origin_url",
        "quality",
    }


def test_append_is_capped_at_ten_by_default_and_thirty_at_most(db_session):
    _warm("graft", [_card(slug=f"owner--repo--s{i}", title=f"s{i}") for i in range(40)])

    assert len(_federated(loopskill_search(db_session, query="graft"))) == FEDERATED_DEFAULT_CAP
    assert (
        len(_federated(loopskill_search(db_session, query="graft", federated_limit=30)))
        == FEDERATED_MAX_CAP
    )
    # An over-cap request is clamped, never honoured.
    assert (
        len(_federated(loopskill_search(db_session, query="graft", federated_limit=999)))
        == FEDERATED_MAX_CAP
    )


def test_federated_append_cannot_blow_up_the_agent_context(db_session):
    """Hostile-width rows at the maximum cap must still fit the documented ceiling."""
    fat = [
        _card(
            slug=("x" * 900) + str(i),
            title="T" * 900,
            origin_url="https://example.com/" + ("y" * 900),
        )
        for i in range(40)
    ]
    _warm("graft", fat)

    rows = _federated(loopskill_search(db_session, query="graft", federated_limit=30))

    size = len(json.dumps(rows).encode("utf-8"))
    assert len(rows) == FEDERATED_MAX_CAP
    assert size <= FEDERATED_APPEND_MAX_BYTES, f"federated append was {size} bytes"


# ── 7. Backward compatibility ────────────────────────────────────────────────


def test_original_four_keys_keep_their_meaning_and_type(db_session):
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()
    _warm("graft", [GRAFT])

    before = loopskill_search(db_session, query="graft", federated_limit=1, hybrid=False)

    assert isinstance(before["results"], list)
    assert isinstance(before["backend"], str)
    assert isinstance(before["hybrid_augmented"], bool)
    # `total` is the NATIVE total — federated rows are appended, never counted
    # into it (that would silently redefine an existing key's meaning).
    assert before["total"] == 1


def test_no_query_still_answers_and_reports_a_flag(db_session):
    result = loopskill_search(db_session)
    assert result["federated"] in {"fresh", "stale", "cold", "degraded"}
    assert result["backend"] == "keyword"


# ── 8. Demand capture lands in its OWN store ─────────────────────────────────


def _fulfilled_events(db):
    from app.models import TelemetryEvent
    from app.services.demand_capture import FEDERATED_FULFILLED_EVENT

    return db.query(TelemetryEvent).filter(TelemetryEvent.event_type == FEDERATED_FULFILLED_EVENT).all()


def test_federated_fulfilled_query_is_recorded_in_its_own_store(db_session):
    _warm("graft", [GRAFT])

    loopskill_search(db_session, query="graft")

    events = _fulfilled_events(db_session)
    assert len(events) == 1
    payload = json.loads(events[0].payload)
    assert payload["query"] == "graft"
    assert payload["federated_count"] == 1
    assert payload["top_install_ref"] == "skills-sh:trailhq--graft--graft"


def test_missing_skill_queries_is_NOT_written_when_federation_fulfilled(db_session):
    """``missing_skill_queries`` means zero TOTAL results. Logging a
    federation-fulfilled query there would corrupt the VOC signal."""
    _warm("graft", [GRAFT])

    loopskill_search(db_session, query="graft")

    assert db_session.query(MissingSkillQuery).count() == 0


def test_no_demand_event_when_native_hits_exist(db_session):
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()
    _warm("graft", [GRAFT])

    loopskill_search(db_session, query="graft")

    assert _fulfilled_events(db_session) == []


def test_no_demand_event_when_federation_found_nothing(db_session):
    loopskill_search(db_session, query="nothing-anywhere")

    assert _fulfilled_events(db_session) == []


def test_a_raising_cache_reader_degrades_instead_of_breaking_search(db_session, monkeypatch):
    """Defence in depth: the append is garnish. If ANYTHING in it throws, the
    native search it rides on still answers."""
    make_skill(db_session, slug="graft-native", title="Graft Native", description="graft helper")
    db_session.commit()

    import app.services.mcp_federated_search as fed_mod

    monkeypatch.setattr(
        fed_mod, "federated_sources", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    result = loopskill_search(db_session, query="graft")

    assert result["federated"] == "degraded"
    assert any(r["slug"] == "graft-native" for r in result["results"])


# ── 9. The compaction / verdict unit surface ─────────────────────────────────


def test_curated_and_unactionable_rows_are_not_appended():
    from app.services.mcp_federated_search import compact_row

    assert compact_row("not-a-dict") is None
    assert compact_row(_card(slug="x", title="X", source="recipes")) is None
    assert compact_row({**_card(slug="x", title="X"), "install_ref": ""}) is None


def test_unrecognised_install_path_fails_closed():
    from app.services.mcp_federated_search import compact_row

    row = compact_row(_card(slug="x", title="X", install_path="teleport", deployable=True))
    assert row is not None
    assert row["deployable"] is False


def test_native_slug_collision_is_not_appended_twice(db_session):
    make_skill(db_session, slug="graft", title="Graft", description="graft helper")
    db_session.commit()
    _warm("graft", [_card(slug="graft", title="graft")])

    result = loopskill_search(db_session, query="graft")

    assert _federated(result) == []
    assert result["federated"] == "fresh"


def test_demand_recorder_refuses_an_empty_query_or_empty_rows(db_session):
    from app.services.demand_capture import record_federated_fulfilled_query

    assert record_federated_fulfilled_query(db_session, "  ", rows=[{"install_ref": "a:b"}]) is False
    assert record_federated_fulfilled_query(db_session, "graft", rows=[]) is False
    assert _fulfilled_events(db_session) == []


def test_demand_recorder_swallows_a_write_failure(db_session, monkeypatch):
    from app.services import demand_capture

    monkeypatch.setattr(
        demand_capture,
        "is_probe_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )
    monkeypatch.setattr(
        db_session, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    written = demand_capture.record_federated_fulfilled_query(
        db_session, "graft", rows=[{"install_ref": "skills-sh:x", "deployable": True}]
    )

    assert written is False
