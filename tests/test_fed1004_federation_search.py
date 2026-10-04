"""fed1004 — federated search that finds what federation holds.

Three defects, each verified live on 2026-10-04 and each pinned here by a test
that fails on the pre-fed1004 code:

1. MCP ``loopskill_search`` answered ``federated: cold`` with ZERO federated
   rows for any query no REST caller had warmed. ``loopskill_search("ste100")``
   returned nothing while ``/api/skills/metasearch?q=ste100`` returned 30 rows.
   Fix: on a miss, a bounded single-flight live fan-out, then the local hub
   index as a floor (``warming``).
2. ClawHub's ``/api/v1/skills`` ignores ``?q=`` AND ``?search=``; only
   ``/api/v1/search?q=`` ranks by query. Every metasearch carried up to 25
   unrelated ClawHub rows (``wayza``, ``aigate`` … for every query).
3. Hub-snapshot origin URLs 404'd: skills.sh 7/40 resolved, official 0/15. The
   skills.sh page resolves 40/40; GitHub ``tree/HEAD`` resolves official 15/15.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.services import mcp_federated_search as mfs
from app.services.metasearch_cache import get_cache

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_cache():
    cache = get_cache()
    original_l2 = cache.l2
    cache.l2 = None
    cache._l2_down = False
    cache.invalidate(None)
    yield cache
    cache.invalidate(None)
    cache._l2_down = False
    cache.l2 = original_l2


def _card(slug: str, *, source: str = "skills-sh") -> dict:
    return {
        "canonical_id": f"{source}:{slug}",
        "slug": slug,
        "title": slug,
        "description": "",
        "source": source,
        "origin_url": f"https://www.skills.sh/{slug.replace('--', '/')}",
        "install_ref": f"{source}:{slug}",
        "quality": "community",
        "deployable": True,
        "install_path": "fetch_origin",
        "license": "MIT",
    }


STE = _card("danyuchn--asd-ste100-skill--asd-ste100")


def _budget(monkeypatch, seconds: float) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "MCP_FEDERATED_LIVE_BUDGET_S", seconds)


def _put(query: str, rows: list[dict]) -> None:
    get_cache().put(query, mfs.federated_sources(), rows, sources_ok=["recipes", "skills-sh"])


# ── 1. MCP live-on-miss ──────────────────────────────────────────────────────


def test_budget_zero_keeps_the_cache_only_contract(monkeypatch):
    """Budget 0 is the documented off-switch: a miss stays a fast ``cold`` and
    no warm thread ever starts."""
    _budget(monkeypatch, 0)
    monkeypatch.setattr(mfs, "_warm_query", lambda *a: pytest.fail("warm must not run at budget 0"))
    rows, flag = mfs.federated_append("ste100")
    assert (rows, flag) == ([], "cold")


def test_cold_miss_is_answered_by_the_live_fanout_within_budget(monkeypatch):
    """THE reported bug: a first-time query must return the federated rows, not
    ``cold`` + nothing."""
    _budget(monkeypatch, 2.0)
    calls: list[str] = []

    def _fast_warm(query, sources):
        calls.append(query)
        _put(query, [STE])

    monkeypatch.setattr(mfs, "_warm_query", _fast_warm)
    rows, flag = mfs.federated_append("ste100")
    assert flag == "fresh"
    assert [r["slug"] for r in rows] == [STE["slug"]]
    assert calls == ["ste100"]


def test_slow_fanout_never_blocks_past_budget_and_serves_the_local_floor(monkeypatch):
    _budget(monkeypatch, 0.2)
    release = threading.Event()

    def _slow_warm(query, sources):
        release.wait(5)
        _put(query, [STE])

    floor_row = {**mfs.compact_row(_card("aminblg--simpleenglish--simple-english")), "slug": "floor-row"}
    monkeypatch.setattr(mfs, "_warm_query", _slow_warm)
    monkeypatch.setattr(mfs, "local_floor", lambda q, *, limit, exclude_slugs: [floor_row])

    t0 = time.monotonic()
    rows, flag = mfs.federated_append("ste100")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"blocked {elapsed:.2f}s on a 0.2s budget"
    assert flag == mfs.WARMING
    assert [r["slug"] for r in rows] == ["floor-row"]

    # The fan-out keeps running behind the answer and warms the cache: the
    # NEXT call is a fresh hit with the full live rows.
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows2, flag2 = mfs.federated_append("ste100")
        if flag2 == "fresh":
            break
        time.sleep(0.05)
    assert flag2 == "fresh"
    assert [r["slug"] for r in rows2] == [STE["slug"]]


def test_stale_entry_is_served_at_once_and_refreshed_behind(monkeypatch):
    _budget(monkeypatch, 2.0)
    _put("ste100", [STE])
    cache = get_cache()
    key = cache._key("ste100", mfs.federated_sources())
    cache._store[key].computed_at = time.time() - (cache.ttl_s + 1)

    started = threading.Event()
    monkeypatch.setattr(mfs, "_warm_query", lambda q, s: started.set())

    rows, flag = mfs.federated_append("ste100")
    assert flag == "stale"
    assert [r["slug"] for r in rows] == [STE["slug"]]
    assert started.wait(2), "a stale serve must trigger a background refresh"


def test_saturated_warm_slots_answer_cold_without_a_new_fanout(monkeypatch):
    _budget(monkeypatch, 1.0)
    monkeypatch.setattr(mfs, "_warm_slots", threading.BoundedSemaphore(1))
    assert mfs._warm_slots.acquire(blocking=False)  # every slot busy
    monkeypatch.setattr(mfs, "_warm_query", lambda *a: pytest.fail("no slot → no fan-out"))
    monkeypatch.setattr(mfs, "local_floor", lambda q, *, limit, exclude_slugs: [])
    try:
        rows, flag = mfs.federated_append("ste100")
    finally:
        mfs._warm_slots.release()
    assert (rows, flag) == ([], "cold")


def test_a_raising_warm_never_breaks_search_and_frees_its_slot(monkeypatch):
    _budget(monkeypatch, 0.5)
    monkeypatch.setattr(mfs, "_warm_slots", threading.BoundedSemaphore(1))

    def _boom(query, sources):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(mfs, "_warm_query", _boom)
    monkeypatch.setattr(mfs, "local_floor", lambda q, *, limit, exclude_slugs: [])
    rows, flag = mfs.federated_append("ste100")
    assert rows == [] and flag == "cold"
    assert mfs._warm_slots.acquire(blocking=False), "the failed warm must release its slot"
    mfs._warm_slots.release()


@pytest.mark.parametrize(("raw", "expected"), [(-3, 0.0), (0, 0.0), (4, 4.0), (999, 10.0), ("x", 0.0)])
def test_live_budget_is_clamped(monkeypatch, raw, expected):
    _budget(monkeypatch, raw)
    assert mfs.live_budget_s() == expected


def test_warm_fills_the_exact_key_the_mcp_reader_reads(monkeypatch):
    """REST and MCP must share ONE cache entry per query — a key mismatch would
    make every MCP warm invisible to the reader (and double the fan-outs)."""
    from app.services import metasearch_compute as mc

    monkeypatch.setattr(
        mc, "build_unified_own_session", lambda q, **kw: ([STE], ["recipes", "skills-sh"], [])
    )
    mc.warm("ste100", mfs.federated_sources())
    lookup = get_cache().get_entry("ste100", mfs.federated_sources())
    assert lookup.state == "fresh"
    assert [r["slug"] for r in lookup.entry.skills] == [STE["slug"]]


@pytest.mark.parametrize(("budget", "expected"), [(4.0, 3.0), (2.5, 1.75), (1.0, 1.2), (10.0, 3.0)])
def test_mcp_warm_deadline_fits_inside_the_budget(budget, expected):
    assert mfs.mcp_source_deadline_s(budget) == pytest.approx(expected)


def test_production_warm_passes_the_mcp_deadline_to_the_fanout(monkeypatch):
    from app.services import metasearch_compute as mc
    from app.services import metasearch_fanout as fo

    _budget(monkeypatch, 4.0)
    seen: dict = {}

    def _fake_fan_out(query, *, sources, **kwargs):
        seen.update(kwargs)
        return fo.FanoutOutput(pairs=[], sources_ok=[], sources_degraded=[])

    monkeypatch.setattr(fo, "fan_out", _fake_fan_out)
    monkeypatch.setattr(mc, "curated_candidates", lambda db, q, limit: [])
    monkeypatch.setattr("app.services.clawhub_owner_prime.prime_clawhub_owner_cache", lambda db: None)
    monkeypatch.setattr(mc, "build_unified_own_session", lambda q, **kw: mc.build_unified(None, q, **kw))
    mfs._warm_query("ste100", mfs.federated_sources())
    assert seen == {"per_source_deadline_s": 3.0}


def test_every_source_gets_its_own_thread_up_to_the_cap(monkeypatch):
    """With fewer threads than sources, the queued sources only START after
    others finish and blow the shared deadline — every source must start at once."""
    from app.services import metasearch_fanout as fo

    started: list[float] = []
    lock = threading.Lock()

    def _slow(source, query, *, limit):
        with lock:
            started.append(time.monotonic())
        time.sleep(0.3)
        return fo.SourceResult(source=source, skills=[], raw_rows=[], ok=True)

    monkeypatch.setattr(fo, "_query_one_source", _slow)
    monkeypatch.setattr(fo.rl, "record_success", lambda src: None)
    sources = tuple(f"s{i}" for i in range(14))
    out = fo.fan_out("q", sources=sources, per_source_deadline_s=0.6)
    assert sorted(out.sources_ok) == sorted(sources)
    assert max(started) - min(started) < 0.25, "a source waited for a free thread"


def test_local_floor_reads_the_hub_index_ranked_and_compact(db_session, monkeypatch):
    from app.models import FederationHubSkill

    for slug, title in (
        ("skills-sh-danyuchn-asd-ste100-skill-asd-ste100", "asd-ste100"),
        ("unrelated-skill", "kubernetes helper"),
    ):
        db_session.add(
            FederationHubSkill(
                slug=slug,
                title=title,
                description="Simplified Technical English" if "ste" in slug else "k8s",
                source="hermes-hub",
                upstream_source="skills-sh",
                identifier=f"skills-sh/x/{slug}",
                origin_url=f"https://www.skills.sh/x/{slug}",
                install_path="fetch_origin",
                repo="danyuchn/asd-ste100-skill",
                path="asd-ste100",
            )
        )
    db_session.commit()
    import app.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    rows = mfs.local_floor("ste100", limit=5, exclude_slugs=set())
    assert [r["slug"] for r in rows] == ["skills-sh-danyuchn-asd-ste100-skill-asd-ste100"]
    assert set(rows[0]) == {
        "slug",
        "title",
        "install_ref",
        "deployable",
        "install_path",
        "origin_url",
        "quality",
    }
    assert rows[0]["install_ref"] == "hermes-hub:skills-sh-danyuchn-asd-ste100-skill-asd-ste100"
    assert mfs.local_floor("", limit=5, exclude_slugs=set()) == []


def test_mcp_tool_contract_names_every_flag_the_tool_can_return():
    from app.mcp.registry import _tool_definitions

    tool = next(t for t in _tool_definitions() if t.name == "loopskill_search")
    for flag in ("fresh", "stale", "warming", "cold", "degraded"):
        assert flag in tool.description


# ── 2. ClawHub search routing ────────────────────────────────────────────────


def _hit(slug: str, *, kind: str = "clawhub", owner: str = "steipete", suspicious: bool = False, dl: int = 7):
    return {
        "slug": slug,
        "displayName": slug.title(),
        "summary": f"{slug} summary",
        "downloads": dl,
        "ownerHandle": owner,
        "install": {"kind": kind, "reference": f"{owner}/{slug}"},
        "native": {
            "ownerHandle": owner,
            "skill": {"slug": slug, "isSuspicious": suspicious, "stats": {"stars": 3}},
        },
    }


def test_a_query_goes_to_the_search_route_never_the_browse_list():
    from app.services import clawhub_search as cs

    seen: list[tuple[str, dict]] = []

    def _get(url, *, params=None, headers=None):
        seen.append((url, dict(params or {})))
        return {"results": [_hit("obsidian")]}

    rows = cs.fetch_rows(_get, "obsidian")
    assert seen == [(cs.CLAWHUB_SEARCH_URL, {"q": "obsidian", "limit": cs.SEARCH_LIMIT})]
    assert rows[0]["slug"] == "obsidian"


def test_an_empty_query_browses_the_list_route():
    from app.services import clawhub_search as cs

    seen: list[str] = []

    def _get(url, *, params=None, headers=None):
        seen.append(url)
        return {"items": [{"slug": "a"}]}

    assert cs.fetch_rows(_get, "  ") == [{"slug": "a"}]
    assert seen == [cs.CLAWHUB_BROWSE_URL]


def test_mirrors_and_suspicious_hits_are_dropped_and_rows_keep_the_browse_shape():
    from app.services import clawhub_search as cs

    rows = cs.parse_response(
        {
            "results": [
                _hit("obsidian", dl=110797),
                _hit("obsidian", kind="skills-sh", owner="bitbonsai"),  # mirror of a source we index directly
                _hit("evil", suspicious=True),
                {"slug": ""},
                "not-a-dict",
            ]
        }
    )
    assert rows == [
        {
            "slug": "obsidian",
            "displayName": "Obsidian",
            "summary": "obsidian summary",
            "ownerHandle": "steipete",
            "stats": {"stars": 3, "downloads": 110797},
            "tags": {},
        }
    ]


def test_clawhub_rows_map_to_owner_scoped_links_without_a_live_owner_lookup(monkeypatch):
    """The search hit carries ``ownerHandle``, so mapping must not call
    ``resolve_owner`` (the per-row network call behind the old >90s cold path)."""
    import app.services.federation_live as fl
    from app.services import clawhub_url
    from app.services.federation_adapters import ClawHubAdapter

    fl._cache.clear()
    monkeypatch.setattr(
        fl, "_safe_json_get", lambda url, *, params=None, headers=None: {"results": [_hit("obsidian")]}
    )
    monkeypatch.setattr(clawhub_url, "resolve_owner", lambda slug: pytest.fail("no owner lookup needed"))
    skills = ClawHubAdapter(fetch=fl.clawhub_fetch).search("obsidian", limit=5)
    assert [s.origin_url for s in skills] == ["https://clawhub.ai/steipete/skills/obsidian"]


# ── 3. Origin URLs that resolve ──────────────────────────────────────────────


def test_skills_sh_rows_link_the_skills_sh_page():
    from app.services.hub_snapshot import origin_url_for_row

    row = {
        "source": "skills.sh",
        "identifier": "skills-sh/danyuchn/asd-ste100-skill/asd-ste100",
        "repo": "danyuchn/asd-ste100-skill",
        "path": "asd-ste100",
        "name": "asd-ste100",
    }
    assert origin_url_for_row(row) == "https://www.skills.sh/danyuchn/asd-ste100-skill/asd-ste100"


@pytest.mark.parametrize(
    "identifier",
    [
        "skills-sh/obra/superpowers-skills/test-driven-development-(tdd)",  # the one live unsafe id
        "skills-sh/a/b",
        "skills-sh/a/../c",
        "skills-sh/a/b/c?x=1",
        "clawhub/a/b/c",
    ],
)
def test_unsafe_or_misshapen_skills_sh_ids_fall_back_to_github(identifier):
    from app.services.hub_snapshot import origin_url_for_row

    row = {"source": "skills.sh", "identifier": identifier, "repo": "owner/repo", "path": "x"}
    assert origin_url_for_row(row) == "https://github.com/owner/repo/tree/main/x"


def test_official_rows_link_the_github_tree_on_the_default_branch():
    from app.services.hub_snapshot import origin_url_for_row

    row = {
        "source": "official",
        "name": "simple-english",
        "identifier": "official/creative/simple-english",
        "repo": "NousResearch/hermes-agent",
        "path": "optional-skills/creative/simple-english",
    }
    assert origin_url_for_row(row) == (
        "https://github.com/NousResearch/hermes-agent/tree/HEAD/optional-skills/creative/simple-english"
    )


def test_official_row_without_coordinates_keeps_the_name_fallback():
    from app.services.hub_snapshot import origin_url_for_row

    row = {"source": "official", "name": "hermes-markdown", "identifier": "official/hermes-markdown"}
    assert "hermes-agent.nousresearch.com/skills/hermes-markdown" in origin_url_for_row(row)


def test_github_rows_are_unchanged():
    from app.services.hub_snapshot import origin_url_for_row

    row = {"source": "github", "repo": "garrytan/gstack", "path": "plan-devex-review", "identifier": "x"}
    assert origin_url_for_row(row) == "https://github.com/garrytan/gstack/tree/main/plan-devex-review"


# ── 4. MCP tool calls never block the event loop ─────────────────────────────


def test_a_slow_mcp_tool_does_not_freeze_the_event_loop():
    """Prod runs ONE uvicorn worker. A sync tool on the loop (a live federated
    wait is up to 4s) would stall every other request, /api/healthz included."""
    import asyncio

    from app.mcp._offloop import dispatch_off_loop

    class _Db:
        closed = False

        def close(self):
            _Db.closed = True

    def _slow_dispatch(name, db, args, caller):
        time.sleep(0.5)
        return {"ok": name}

    async def _main():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(_ticker())
        payload = await dispatch_off_loop(_slow_dispatch, "loopskill_search", _Db, {}, {})
        task.cancel()
        return payload, ticks

    payload, ticks = asyncio.run(_main())
    assert payload == {"ok": "loopskill_search"}
    assert ticks >= 10, f"event loop starved during the tool call ({ticks} ticks in 0.5s)"
    assert _Db.closed


def test_a_raising_tool_returns_an_error_payload():
    import asyncio

    from app.mcp._offloop import dispatch_off_loop

    def _boom(name, db, args, caller):
        raise ValueError("unknown tool: nope")

    class _Db:
        def close(self):
            pass

    assert asyncio.run(dispatch_off_loop(_boom, "nope", _Db, None, {})) == {
        "error": "unknown tool: nope",
        "tool": "nope",
    }


def test_the_real_mcp_call_tool_handler_keeps_the_loop_free(monkeypatch):
    """End-to-end through ``build_mcp_server``'s registered handler: while a
    slow tool runs, other coroutines on the loop keep running."""
    import asyncio

    import mcp.types as types

    import app.mcp.server as server_mod

    def _slow_dispatch(name, db, args, caller):
        time.sleep(0.5)
        return {"results": [], "federated": "fresh"}

    class _Db:
        def close(self):
            pass

    monkeypatch.setattr(server_mod, "_dispatch", _slow_dispatch)
    monkeypatch.setattr(server_mod, "_caller_from_request_context", lambda server: {"scope": "master"})
    handler = server_mod.build_mcp_server(db_factory=_Db).request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="loopskill_search", arguments={"query": "x"}),
    )

    async def _main():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(_ticker())
        await handler(req)
        task.cancel()
        return ticks

    assert asyncio.run(_main()) >= 10, "the MCP handler blocked the event loop"
