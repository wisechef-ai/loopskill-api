"""superset_0606 Phase D — Giants depth crawl: ClawHub cursor-walk + skills.sh sitemap-walk.

The two sources that are 99% of the Hub's headline number, walked to exhaustion
in the background reindex cron. All offline — every HTTP fetch is injected via a
fake getter matching ``guarded_get``'s contract (url, *, timeout) → Response|None.

Covers:
  - app/services/giants_walk.py        : clawhub_walk + skills_sh_walk + helpers
  - scripts/federation_reindex.py      : deep-walker preference + honest cache write

Reality pinned by these tests (verified live 2026-06-06):
  - ClawHub nextCursor is an OPAQUE STRING passed back VERBATIM as cursor=… .
  - skills.sh sitemap index → sitemap-skills-1/-2 (10k each = 20k slugs).
  - ClawHub install path = DEEP_LINK only → installable == 0 (decision #6).
  - skills.sh bulk walk does NOT resolve licenses → installable is None.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse


from app.services import giants_walk as gw


# ─────────────────────────── fake HTTP plumbing ─────────────────────────────


class FakeResp:
    """Minimal stand-in for httpx.Response (the bits the walkers touch)."""

    def __init__(self, status_code: int = 200, *, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _cursor_param(url: str) -> str | None:
    """Pull the ``cursor`` query param back out of a built URL (decoded)."""
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("cursor")
    return vals[0] if vals else None


# ───────────────────────────── helper units ─────────────────────────────────


class TestHelpers:
    def test_build_url_folds_params(self):
        url = gw._build_url(gw.CLAWHUB_SKILLS_URL, {"limit": 200, "cursor": "abc"})
        assert "limit=200" in url and "cursor=abc" in url

    def test_extract_locs(self):
        xml = "<urlset><url><loc>https://a/x</loc></url><url><loc> https://a/y </loc></url></urlset>"
        assert gw._extract_locs(xml) == ["https://a/x", "https://a/y"]

    def test_extract_locs_empty(self):
        assert gw._extract_locs("") == []
        assert gw._extract_locs("<urlset></urlset>") == []

    def test_skills_sh_slug_maps_path(self):
        assert (
            gw._skills_sh_slug("https://www.skills.sh/vercel-labs/skills/find-skills")
            == "vercel-labs/skills/find-skills"
        )
        assert (
            gw._skills_sh_slug("https://skills.sh/anthropics/skills/frontend-design")
            == "anthropics/skills/frontend-design"
        )

    def test_skills_sh_slug_rejects_non_skill(self):
        assert gw._skills_sh_slug("https://www.skills.sh/") is None  # no path
        assert gw._skills_sh_slug("https://www.skills.sh/owner") is None  # single segment
        assert gw._skills_sh_slug("https://example.com/a/b") is None  # wrong host


# ───────────────────────────── ClawHub walk ─────────────────────────────────


class TestClawhubWalk:
    def _pager(self, pages: list[dict]):
        """Build a fake getter that serves successive pages keyed by cursor.

        pages[i] = {"items": [...], "nextCursor": "<str>"|None}. The walker starts
        with no cursor (page 0); each returned nextCursor selects the next page.
        """
        by_cursor: dict[str | None, dict] = {None: pages[0]}
        for p in pages:
            nc = p.get("nextCursor")
            if nc is not None:
                # the page whose nextCursor == nc is followed by the NEXT page
                idx = pages.index(p)
                if idx + 1 < len(pages):
                    by_cursor[nc] = pages[idx + 1]

        calls = {"n": 0}

        def _get(url, *, timeout=None):
            calls["n"] += 1
            cur = _cursor_param(url)
            page = by_cursor.get(cur, {"items": []})
            return FakeResp(200, json_body=page)

        _get.calls = calls  # type: ignore[attr-defined]
        return _get

    def test_walks_to_exhaustion_dedups(self):
        pages = [
            {"items": [{"slug": "a"}, {"slug": "b"}], "nextCursor": "C1"},
            {"items": [{"slug": "b"}, {"slug": "c"}], "nextCursor": "C2"},  # b dup
            {"items": [{"slug": "d"}], "nextCursor": None},  # exhausted
        ]
        res = gw.clawhub_walk(_get=self._pager(pages))
        assert res.indexed == 4  # a,b,c,d deduped
        assert res.installable == 0  # decision #6 DEEP_LINK only
        assert res.exhausted is True
        assert res.pages_walked == 3

    def test_cursor_passed_verbatim(self):
        """The opaque nextCursor must go back BYTE-FOR-BYTE (the live bug fix)."""
        opaque = json.dumps({"v": 1, "index": "by_active_updated", "key": [{"__undef": 1}, 123]})
        seen_cursors: list[str | None] = []

        def _get(url, *, timeout=None):
            seen_cursors.append(_cursor_param(url))
            if _cursor_param(url) is None:
                return FakeResp(200, json_body={"items": [{"slug": "a"}], "nextCursor": opaque})
            return FakeResp(200, json_body={"items": [{"slug": "b"}], "nextCursor": None})

        res = gw.clawhub_walk(_get=_get)
        assert res.indexed == 2
        # page 2's cursor must equal the opaque string verbatim (decoded by parse_qs)
        assert seen_cursors[1] == opaque

    def test_page_cap_bounds_walk(self):
        # A source that never stops returning a cursor must still terminate.
        def _get(url, *, timeout=None):
            n = _cursor_param(url) or "0"
            nxt = str(int(n) + 1)
            return FakeResp(200, json_body={"items": [{"slug": f"s{nxt}"}], "nextCursor": nxt})

        res = gw.clawhub_walk(max_pages=5, _get=_get)
        assert res.pages_walked == 5
        assert res.exhausted is False  # cap hit, not true exhaustion
        assert res.indexed == 5

    def test_stall_guard_breaks_on_zero_new(self):
        # Cursor keeps changing but every page repeats the same slug → stall guard.
        def _get(url, *, timeout=None):
            n = int(_cursor_param(url) or "0")
            return FakeResp(200, json_body={"items": [{"slug": "same"}], "nextCursor": str(n + 1)})

        res = gw.clawhub_walk(max_pages=100, _get=_get)
        assert res.indexed == 1
        assert res.pages_walked == 2  # page 1 found "same"; page 2 repeated it → stall guard fires

    def test_empty_items_is_exhaustion(self):
        res = gw.clawhub_walk(_get=lambda url, *, timeout=None: FakeResp(200, json_body={"items": []}))
        assert res.indexed == 0
        assert res.exhausted is True
        assert res.partial_error is None

    def test_http_error_is_partial_not_crash(self):
        res = gw.clawhub_walk(_get=lambda url, *, timeout=None: FakeResp(503, json_body=None))
        assert res.indexed == 0
        assert res.partial_error is not None and "status=503" in res.partial_error

    def test_exception_is_caught(self):
        def _boom(url, *, timeout=None):
            raise RuntimeError("network down")

        res = gw.clawhub_walk(_get=_boom)
        assert res.indexed == 0
        assert res.partial_error is not None and "network down" in res.partial_error

    def test_bad_json_stops_gracefully(self):
        res = gw.clawhub_walk(_get=lambda url, *, timeout=None: FakeResp(200, json_body=None))
        assert res.indexed == 0
        assert res.partial_error is not None and "bad json" in res.partial_error

    def test_first_page_capped_and_deep_link(self):
        pages = [{"items": [{"slug": f"s{i}"} for i in range(50)], "nextCursor": None}]
        res = gw.clawhub_walk(first_page_cap=10, _get=self._pager(pages))
        assert res.indexed == 50
        assert len(res.first_page) == 10
        # every cached row is a ClawHub deep-link, never rehosted
        assert all(r["install_path"] == "deep_link" for r in res.first_page)
        assert all(r["source"] == "clawhub" for r in res.first_page)


# ───────────────────────────── skills.sh walk ───────────────────────────────


class TestSkillsShWalk:
    def _sitemap_getter(self, *, sub1_locs, sub2_locs, index_subs=None):
        index_subs = index_subs or [
            "https://www.skills.sh/sitemap-skills-1.xml",
            "https://www.skills.sh/sitemap-skills-2.xml",
        ]
        index_xml = (
            "<sitemapindex>"
            + "".join(
                f"<sitemap><loc>{u}</loc></sitemap>"
                for u in (
                    ["https://www.skills.sh/sitemap-misc.xml", "https://www.skills.sh/sitemap-owners.xml"]
                    + index_subs
                )
            )
            + "</sitemapindex>"
        )

        def _loc_xml(locs):
            return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in locs) + "</urlset>"

        def _get(url, *, timeout=None):
            if url == gw.SKILLS_SH_SITEMAP_URL:
                return FakeResp(200, text=index_xml)
            if "sitemap-skills-1" in url:
                return FakeResp(200, text=_loc_xml(sub1_locs))
            if "sitemap-skills-2" in url:
                return FakeResp(200, text=_loc_xml(sub2_locs))
            return FakeResp(404, text="")

        return _get

    def test_walks_both_sub_sitemaps(self):
        sub1 = [f"https://www.skills.sh/owner{i}/skills/s{i}" for i in range(5)]
        sub2 = [f"https://www.skills.sh/orgx/agent-skills/a{i}" for i in range(7)]
        res = gw.skills_sh_walk(_get=self._sitemap_getter(sub1_locs=sub1, sub2_locs=sub2))
        assert res.indexed == 12  # 5 + 7
        assert res.installable is None  # bulk walk doesn't resolve licenses
        assert res.pages_walked == 2  # two sub-sitemaps walked
        assert res.exhausted is True

    def test_dedups_across_sitemaps(self):
        shared = "https://www.skills.sh/owner/skills/dup"
        res = gw.skills_sh_walk(
            _get=self._sitemap_getter(
                sub1_locs=[shared, "https://www.skills.sh/owner/skills/u1"],
                sub2_locs=[shared, "https://www.skills.sh/owner/skills/u2"],
            )
        )
        assert res.indexed == 3  # dup counted once

    def test_skips_non_skill_locs(self):
        res = gw.skills_sh_walk(
            _get=self._sitemap_getter(
                sub1_locs=["https://www.skills.sh/", "https://www.skills.sh/owner/skills/real"],
                sub2_locs=[],
            )
        )
        assert res.indexed == 1  # home page loc skipped

    def test_first_page_capped_and_origin_shape(self):
        sub1 = [f"https://www.skills.sh/owner/skills/s{i}" for i in range(30)]
        res = gw.skills_sh_walk(first_page_cap=5, _get=self._sitemap_getter(sub1_locs=sub1, sub2_locs=[]))
        assert res.indexed == 30
        assert len(res.first_page) == 5
        assert all(r["source"] == "skills-sh" for r in res.first_page)

    def test_index_fetch_failure_is_empty(self):
        res = gw.skills_sh_walk(_get=lambda url, *, timeout=None: FakeResp(500, text=""))
        assert res.indexed == 0
        assert res.installable is None
        assert res.partial_error is not None

    def test_index_exception_is_caught(self):
        def _boom(url, *, timeout=None):
            raise RuntimeError("dns fail")

        res = gw.skills_sh_walk(_get=_boom)
        assert res.indexed == 0 and "dns fail" in res.partial_error

    def test_no_sub_sitemaps_found(self):
        def _get(url, *, timeout=None):
            return FakeResp(200, text="<sitemapindex></sitemapindex>")

        res = gw.skills_sh_walk(_get=_get)
        assert res.indexed == 0
        assert "no sitemap-skills" in res.partial_error

    def test_one_sub_sitemap_fails_other_survives(self):
        def _get(url, *, timeout=None):
            if url == gw.SKILLS_SH_SITEMAP_URL:
                return FakeResp(
                    200,
                    text=(
                        "<sitemapindex>"
                        "<sitemap><loc>https://www.skills.sh/sitemap-skills-1.xml</loc></sitemap>"
                        "<sitemap><loc>https://www.skills.sh/sitemap-skills-2.xml</loc></sitemap>"
                        "</sitemapindex>"
                    ),
                )
            if "sitemap-skills-1" in url:
                return FakeResp(
                    200, text="<urlset><url><loc>https://www.skills.sh/o/skills/a</loc></url></urlset>"
                )
            return FakeResp(503, text="")  # sub-2 down

        res = gw.skills_sh_walk(_get=_get)
        assert res.indexed == 1  # sub-1's skill survived
        assert res.partial_error is not None  # sub-2 failure recorded honestly


# ───────────────────── reindex driver: deep-walker preference ────────────────


class TestReindexDeepWalkerWiring:
    def test_deep_walker_registered_for_both_giants(self):
        assert set(gw.DEEP_WALKERS) == {"clawhub", "skills-sh"}

    @staticmethod
    def _patch_cache_writes(monkeypatch):
        """Record write_source_cache calls on the REAL module (import-order safe —
        a sys.modules swap pollutes sibling suites; patching the function does not)."""
        from app.services import federation_cache as fcache

        writes: list[dict] = []

        def _rec(db, source, **kw):
            writes.append({"source": source, **kw})
            return None

        monkeypatch.setattr(fcache, "write_source_cache", _rec)
        return writes

    def test_reindex_prefers_deep_walker(self, monkeypatch):
        import scripts.federation_reindex as fr

        fake = gw.WalkResult(
            indexed=50321, installable=0, first_page=[{"slug": "x"}], pages_walked=252, exhausted=True
        )
        monkeypatch.setitem(gw.DEEP_WALKERS, "clawhub", lambda: fake)
        writes = self._patch_cache_writes(monkeypatch)

        report = fr.reindex_source(db=None, source_id="clawhub")
        assert report["status"] == "ok"
        assert report["indexed"] == 50321
        assert report["installable"] == 0
        assert writes and writes[0]["indexed_count"] == 50321
        assert writes[0]["installable_count"] == 0

    def test_reindex_deep_walk_zero_with_error_is_failure(self, monkeypatch):
        import scripts.federation_reindex as fr

        fake = gw.WalkResult(indexed=0, installable=None, partial_error="sitemap index status=500")
        monkeypatch.setitem(gw.DEEP_WALKERS, "skills-sh", lambda: fake)
        writes = self._patch_cache_writes(monkeypatch)

        report = fr.reindex_source(db=None, source_id="skills-sh")
        assert report["status"] == "error"
        assert report["indexed"] is None
        assert writes[0]["indexed_count"] is None  # NULL → omitted from sum

    def test_reindex_deep_walk_partial_keeps_real_count(self, monkeypatch):
        """A walk that gathered rows but hit a LATE partial error records the real
        count it reached (honest partial), not NULL."""
        import scripts.federation_reindex as fr

        fake = gw.WalkResult(indexed=10000, installable=None, partial_error="sub-2 down", pages_walked=1)
        monkeypatch.setitem(gw.DEEP_WALKERS, "skills-sh", lambda: fake)
        writes = self._patch_cache_writes(monkeypatch)

        report = fr.reindex_source(db=None, source_id="skills-sh")
        assert report["status"] == "ok"
        assert report["indexed"] == 10000
        assert writes[0]["indexed_count"] == 10000
        assert writes[0]["last_error"] == "sub-2 down"

    def test_reindex_deep_walk_exception_records_null(self, monkeypatch):
        import scripts.federation_reindex as fr

        def _boom():
            raise RuntimeError("walker crashed")

        monkeypatch.setitem(gw.DEEP_WALKERS, "clawhub", _boom)
        writes = self._patch_cache_writes(monkeypatch)
        report = fr.reindex_source(db=None, source_id="clawhub")
        assert report["status"] == "error"
        assert writes[0]["indexed_count"] is None
