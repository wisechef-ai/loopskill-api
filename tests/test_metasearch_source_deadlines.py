"""Per-source fan-out deadlines (t_b9887867).

Prod defect 2026-10-05: ``/api/skills/metasearch`` listed clawhub, skills-sh and
github-oss in ``sources_degraded`` on every cold compute. Probed FROM wisechef-hq
inside the app venv, all three upstreams answered 200 with rows; they were just
slower than the single 1.2 s deadline (+0.25 s slack) every source shared:

    clawhub     /api/v1/search?q=   1.62-2.14 s
    github-oss  code search         0.53-1.76 s
    skills-sh   /api/search         0.58-0.76 s (tips over under 14-thread load)

These tests use the DEFAULT deadlines (no ``per_source_deadline_s`` override),
because the default table is what prod runs and what was wrong.
"""

from __future__ import annotations

import time

import app.services.federation_live as fl
import app.services.metasearch_fanout as fo
from app.services import metasearch_ratelimit as rl

_CLAWHUB_ROW = {
    "slug": "git-helper",
    "displayName": "Git helper",
    "summary": "s",
    "ownerHandle": "o",
    "stats": {},
}
_SKILLS_SH_ROW = {"id": "a/b/c", "name": "C", "installs": 3, "source": "a/b"}
_BROWSE_ROW = {"slug": "s", "name": "S", "title": "S"}


def setup_function(_):
    rl.reset_all()


def teardown_function(_):
    rl.reset_all()


def _slow(rows, delay):
    def _fetch(_q):
        time.sleep(delay)
        return rows

    return _fetch


def test_clawhub_at_its_measured_prod_latency_is_ok_not_degraded(monkeypatch):
    """The root cause: ClawHub's live search answers in ~2 s from prod, past the
    old shared 1.45 s gather budget, so it was degraded on every cold query."""
    monkeypatch.setattr(fo, "_clawhub_fetch_fixed", _slow([_CLAWHUB_ROW], 2.0))
    out = fo.fan_out("git", sources=("clawhub",))
    assert out.sources_degraded == []
    assert out.sources_ok == ["clawhub"]
    assert len(out.pairs) == 1


def test_live_search_sources_at_prod_latency_are_all_ok(monkeypatch):
    monkeypatch.setattr(fo, "_clawhub_fetch_fixed", _slow([_CLAWHUB_ROW], 1.8))
    monkeypatch.setitem(fl.LIVE_FETCH, "skills-sh", _slow([_SKILLS_SH_ROW], 1.6))
    monkeypatch.setitem(fl.LIVE_FETCH, "github-oss", _slow([], 1.8))
    out = fo.fan_out("git", sources=("clawhub", "skills-sh", "github-oss"))
    assert out.sources_degraded == []
    assert sorted(out.sources_ok) == ["clawhub", "github-oss", "skills-sh"]


def test_deadline_table_covers_the_measured_latencies():
    """Pin the values against the prod measurement, with headroom: a deadline
    at the measured p-max would flap."""
    for src, measured_max in (("clawhub", 2.14), ("github-oss", 1.76), ("skills-sh", 0.76)):
        assert fo.deadline_for(src) >= measured_max + 0.5, src
    # Catalog sources are served from an in-process cache and keep the tight budget.
    assert fo.deadline_for("browse-sh") == fo._PER_SOURCE_DEADLINE_S


def test_a_hung_fast_source_still_degrades_at_its_own_deadline(monkeypatch):
    """A longer ClawHub budget must not stretch a hung catalog source: it is cut
    at ITS deadline, and the gather ends when the slowest live source answers."""
    monkeypatch.setattr(fo, "_clawhub_fetch_fixed", _slow([_CLAWHUB_ROW], 0.1))
    monkeypatch.setitem(fl.LIVE_FETCH, "browse-sh", _slow([_BROWSE_ROW], 5.0))
    t = time.monotonic()
    out = fo.fan_out("q", sources=("clawhub", "browse-sh"))
    elapsed = time.monotonic() - t
    assert out.sources_ok == ["clawhub"]
    assert out.sources_degraded == ["browse-sh"]
    budget = fo.deadline_for("browse-sh") + fo._DEADLINE_SLACK_S
    assert elapsed < budget + 0.4, f"waited {elapsed:.2f}s for a source budgeted {budget:.2f}s"


def test_a_hung_live_source_is_bounded_by_its_own_deadline(monkeypatch):
    monkeypatch.setitem(fl.LIVE_FETCH, "skills-sh", _slow([_SKILLS_SH_ROW], 10.0))
    t = time.monotonic()
    out = fo.fan_out("q", sources=("skills-sh",))
    elapsed = time.monotonic() - t
    assert out.sources_degraded == ["skills-sh"]
    assert elapsed < fo.deadline_for("skills-sh") + fo._DEADLINE_SLACK_S + 0.4


def test_explicit_override_still_applies_to_every_source(monkeypatch):
    """Callers (and the older tests) that pass ``per_source_deadline_s`` keep a
    uniform budget, ClawHub included."""
    monkeypatch.setattr(fo, "_clawhub_fetch_fixed", _slow([_CLAWHUB_ROW], 1.0))
    t = time.monotonic()
    out = fo.fan_out("q", sources=("clawhub",), per_source_deadline_s=0.2)
    assert out.sources_degraded == ["clawhub"]
    assert time.monotonic() - t < 0.9
