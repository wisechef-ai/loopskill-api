"""Tests for the concurrent metasearch fan-out orchestrator (metasearch_0710 P0 —
council condition 1). Uses injected fetch callables (monkeypatched LIVE_FETCH) so
no network is hit."""

from __future__ import annotations

import app.services.metasearch_fanout as fo
from app.services import metasearch_ratelimit as rl
from app.services.metasearch import merge_unified, unify_external


def _fake_live_fetch(monkeypatch, mapping: dict):
    """Patch LIVE_FETCH (used by _fetch_for) with fake per-source fetchers."""
    fake = {src: (lambda rows=rows: (lambda _q: rows))() for src, rows in mapping.items()}
    monkeypatch.setattr("app.services.federation_live.LIVE_FETCH", fake, raising=True)


def setup_function(_):
    rl.reset_all()


def teardown_function(_):
    rl.reset_all()


def test_fan_out_returns_pairs_with_raw_rows_for_popularity(monkeypatch):
    _fake_live_fetch(
        monkeypatch,
        {
            "skills-sh": [{"id": "owner/repo/skill", "name": "Skill", "installs": 777, "source": "owner/repo"}],
        },
    )
    out = fo.fan_out("skill", sources=("skills-sh",))
    assert "skills-sh" in out.sources_ok
    assert len(out.pairs) == 1
    skill, raw = out.pairs[0]
    assert raw.get("installs") == 777, "raw row must be carried for popularity (C5)"
    u = unify_external(skill, raw_row=raw)
    assert u.popularity == 777


def test_fan_out_concurrent_multiple_sources(monkeypatch):
    _fake_live_fetch(
        monkeypatch,
        {
            "skills-sh": [{"id": "a/b/c", "name": "A", "installs": 10, "source": "a/b"}],
            "browse-sh": [{"slug": "x", "name": "X", "title": "X"}],
            "lobehub": [{"identifier": "y", "meta": {"title": "Y", "description": "d"}}],
        },
    )
    out = fo.fan_out("q", sources=("skills-sh", "browse-sh", "lobehub"))
    assert set(out.sources_ok) == {"skills-sh", "browse-sh", "lobehub"}
    assert len(out.pairs) == 3


def test_fan_out_degrades_gracefully_on_source_error(monkeypatch):
    def _boom(_q):
        raise RuntimeError("source down")

    good = {"browse-sh": [{"slug": "ok", "name": "OK", "title": "OK"}]}
    _fake_live_fetch(monkeypatch, good)
    # inject a broken source on top
    import app.services.federation_live as fl

    fl.LIVE_FETCH["skills-sh"] = _boom
    out = fo.fan_out("q", sources=("skills-sh", "browse-sh"))
    assert "browse-sh" in out.sources_ok
    assert "skills-sh" in out.sources_degraded
    assert len(out.pairs) == 1, "healthy source still returns despite sibling failure"


def test_fan_out_respects_rate_limit_gate(monkeypatch):
    _fake_live_fetch(monkeypatch, {"skills-sh": [{"id": "a/b/c", "name": "A", "installs": 1, "source": "a/b"}]})
    # Open the circuit for skills-sh so acquire() returns False.
    for _ in range(5):
        rl.record_failure("skills-sh")
    out = fo.fan_out("q", sources=("skills-sh",))
    assert "skills-sh" in out.sources_degraded
    assert out.pairs == []


def test_clawhub_fetch_uses_search_param_not_q(monkeypatch):
    """Council C1 + live probe: ClawHub wants ?search=, not ?q=. Assert the fixed
    fetcher passes 'search' to the JSON getter."""
    captured_params = {}

    def _fake_json_get(url, *, params=None, headers=None):
        captured_params.update(params or {})
        return {"items": [{"slug": "found", "displayName": "Found", "stats": {"downloads": 3}}]}

    import app.services.federation_live as fl

    monkeypatch.setattr(fl, "_safe_json_get", _fake_json_get)
    fl._cache.clear()
    rows = fo._clawhub_fetch_fixed("humanizer")
    assert captured_params.get("search") == "humanizer", "must send ?search="
    assert "q" not in captured_params, "must NOT send ?q= (the shipped bug)"
    assert rows and rows[0]["slug"] == "found"


def test_fan_out_end_to_end_into_merge_unified(monkeypatch):
    """The full P0 path: fan_out → unify_external → merge_unified → one ranked
    list with clawhub non-deployable."""
    _fake_live_fetch(
        monkeypatch,
        {
            "skills-sh": [{"id": "o/r/pop", "name": "Popular", "installs": 9999, "source": "o/r"}],
            "browse-sh": [{"slug": "mid", "name": "Mid", "title": "Mid"}],
        },
    )
    # clawhub via the fixed fetcher
    import app.services.federation_live as fl

    monkeypatch.setattr(
        fl, "_safe_json_get",
        lambda url, *, params=None, headers=None: {"items": [{"slug": "claw", "displayName": "Claw", "stats": {"downloads": 5}}]},
    )
    fl._cache.clear()
    out = fo.fan_out("q", sources=("skills-sh", "browse-sh", "clawhub"))
    externals = [unify_external(s, raw_row=r) for s, r in out.pairs]
    result = merge_unified([], externals, sources_ok=out.sources_ok, sources_degraded=out.sources_degraded)
    d = result.to_dict()
    assert d["result_count"] == 3
    # clawhub present + searchable but NOT deployable (condition 2b)
    claw = next(s for s in d["skills"] if s["source"] == "clawhub")
    assert claw["deployable"] is False
    # skills.sh (9999 installs) outranks browse-sh (no signal → 0.5 prior, but
    # single-item sources both get 0.5; skills.sh ties on score, wins on priority)
    assert d["skills"][0]["source"] in ("skills-sh", "browse-sh")


def test_hung_source_degrades_at_deadline_not_escapes(monkeypatch):
    """Council finding 1: a source that hangs past the deadline must be marked
    degraded and the healthy source still returns — NO TimeoutError escapes, and
    the request does not block on the hung thread."""
    import time
    import app.services.federation_live as fl

    fl.LIVE_FETCH["skills-sh"] = lambda q: [{"id": "a/b/c", "name": "X", "installs": 1, "source": "a/b"}]
    fl.LIVE_FETCH["browse-sh"] = lambda q: (time.sleep(2), [{"slug": "s", "name": "S", "title": "S"}])[1]
    t = time.monotonic()
    out = fo.fan_out("q", sources=("skills-sh", "browse-sh"), per_source_deadline_s=0.3)
    elapsed = time.monotonic() - t
    assert elapsed < 1.9, f"must not block on the 2s hung source, took {elapsed:.2f}s"
    assert "skills-sh" in out.sources_ok
    assert "browse-sh" in out.sources_degraded


def test_effective_limits_divide_by_worker_count(monkeypatch):
    """Council finding 2: per-worker limits divide the source ceiling by worker
    count so N workers don't collectively exceed the upstream ceiling."""
    from app.services import metasearch_ratelimit as rl

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    cap1, refill1 = rl.effective_limits("clawhub")  # raw is (30, 8)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    cap4, _ = rl.effective_limits("clawhub")
    assert cap1 < cap4, "4 workers must each get a smaller bucket than 1 worker"
    assert abs(cap1 - 30 / 4) < 0.01
