"""spotify_2607/0 — ClawHub owner backfill: resolution, persistence, DURABILITY.

Issue #141. PR #140 fixed the ClawHub URL MINT path but could not repair the
69,150 rows already carrying the bare ``/skills/<slug>`` soft-404 form. This
module covers the repair, and — more importantly — the thing that makes the
repair last.

THE LOAD-BEARING TEST IN THIS FILE
----------------------------------
``TestOwnerHandleSurvivesReindex``.

``bulk_upsert_skills`` DELETES every ``federation_hub_skills`` row and re-inserts
from the Hub snapshot. The snapshot carries no owner handle. So a backfill on its
own has a lifetime of **less than 24 hours** — the 03:00 ``federation_reindex``
cron would wipe all 69,150 resolutions and hand three quarters of the federated
index back its browse-page fallback, silently, with every link answering HTTP 200
the whole time.

That is the same shape as the bug being fixed (a soft failure nothing alarms on),
so it gets an explicit RED-provable test rather than a comment.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.services.clawhub_owner_bulk import (
    _record,
    owners_from_feed,
    owners_from_search,
    resolve_owner_via_detail,
)
from app.services.clawhub_url import CLAWHUB_BROWSE_URL, clawhub_skill_url
from app.services.hub_snapshot import (
    apply_resolved_owners,
    map_hub_row,
)

BARE_FORM = "https://clawhub.ai/skills/"


# ── Bulk harvest ─────────────────────────────────────────────────────────


class TestRecord:
    """`_record` is the single validation choke point for upstream data."""

    def test_stores_safe_pair(self) -> None:
        out: dict[str, str] = {}
        assert _record(out, "my-skill", "alice") is True
        assert out == {"my-skill": "alice"}

    def test_first_write_wins(self) -> None:
        out = {"s": "first"}
        assert _record(out, "s", "second") is False
        assert out["s"] == "first"

    @pytest.mark.parametrize(
        "slug,handle",
        [
            ("ok", "bad/owner"),      # path traversal into the URL
            ("ok", "a?x=1"),          # query injection
            ("ok", "a#frag"),         # fragment injection
            ("ok", "has space"),
            ("ok", ""),
            ("ok", None),
            ("bad/slug", "alice"),
            (None, "alice"),
            ("ok", "x" * 200),        # exceeds the is_safe_token ceiling
        ],
    )
    def test_rejects_unsafe(self, slug: Any, handle: Any) -> None:
        out: dict[str, str] = {}
        assert _record(out, slug, handle) is False
        assert out == {}


class TestOwnersFromSearch:
    def test_harvests_owner_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "results": [
                {"slug": "alpha", "ownerHandle": "ann"},
                {"slug": "beta", "ownerHandle": "bob"},
            ]
        }
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: payload)
        out = owners_from_search(terms=["a"], delay=0)
        assert out == {"alpha": "ann", "beta": "bob"}

    def test_transport_failure_yields_empty_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fail-safe: a dead upstream must leave rows unresolved (browse-page
        # fallback, a working link), never abort the backfill.
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: None)
        assert owners_from_search(terms=["a", "b"], delay=0) == {}

    def test_drops_unsafe_rows_but_keeps_good_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "results": [
                {"slug": "good", "ownerHandle": "ann"},
                {"slug": "evil", "ownerHandle": "../../etc"},
                {"slug": "noowner"},
            ]
        }
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: payload)
        assert owners_from_search(terms=["a"], delay=0) == {"good": "ann"}


class TestOwnersFromFeed:
    def test_parses_packed_owner_slug_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "entries": [
                {"id": "@alipay/alipay-wallet", "title": "alipay-wallet",
                 "publisher": {"id": "alipay", "trust": "official"}},
            ]
        }
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: payload)
        assert owners_from_feed() == {"alipay-wallet": "alipay"}

    def test_feed_failure_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: None)
        assert owners_from_feed() == {}


class TestResolveOwnerViaDetail:
    def test_reads_top_level_owner_handle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `owner` is a TOP-LEVEL key, NOT nested under `skill` — verified live
        # 2026-07-26. Reading it from the wrong place returns None for every
        # row and the backfill silently resolves nothing.
        payload = {
            "skill": {"slug": "ai-humanizer-2-1-0"},
            "owner": {"handle": "hades4501", "userId": "abc"},
        }
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: payload)
        assert resolve_owner_via_detail("ai-humanizer-2-1-0") == "hades4501"

    def test_owner_nested_under_skill_is_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guards the exact misread above.
        payload = {"skill": {"slug": "x", "owner": {"handle": "wrongplace"}}}
        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", lambda *a, **k: payload)
        assert resolve_owner_via_detail("x") is None

    def test_unsafe_slug_never_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[str] = []

        def _spy(url: str, **k: Any) -> None:
            called.append(url)
            return None

        monkeypatch.setattr("app.services.clawhub_owner_bulk._get_json", _spy)
        assert resolve_owner_via_detail("../../etc/passwd") is None
        assert called == [], "hostile slug reached the network layer"


# ── Row upgrade ──────────────────────────────────────────────────────────


class TestApplyResolvedOwners:
    def _row(self, **over: Any) -> dict[str, Any]:
        row = {
            "upstream_source": "clawhub",
            "identifier": "aigate",
            "origin_url": CLAWHUB_BROWSE_URL,
            "owner_handle": None,
        }
        row.update(over)
        return row

    def test_upgrades_browse_fallback_to_deep_link(self) -> None:
        rows = [self._row()]
        assert apply_resolved_owners(rows, {"aigate": "psyb0t"}) == 1
        assert rows[0]["owner_handle"] == "psyb0t"
        assert rows[0]["origin_url"] == "https://clawhub.ai/psyb0t/skills/aigate"
        assert not rows[0]["origin_url"].startswith(BARE_FORM)

    def test_never_overwrites_an_owner_the_snapshot_supplied(self) -> None:
        # If upstream ever ships handles inline, that value is fresher than our
        # cached resolution and must win.
        rows = [self._row(owner_handle="fromsnapshot")]
        assert apply_resolved_owners(rows, {"aigate": "stale"}) == 0
        assert rows[0]["owner_handle"] == "fromsnapshot"

    def test_ignores_non_clawhub_rows(self) -> None:
        rows = [self._row(upstream_source="skills-sh", origin_url="https://github.com/x/y")]
        assert apply_resolved_owners(rows, {"aigate": "psyb0t"}) == 0
        assert rows[0]["origin_url"] == "https://github.com/x/y"

    def test_empty_map_is_a_noop(self) -> None:
        rows = [self._row()]
        assert apply_resolved_owners(rows, {}) == 0
        assert rows[0]["owner_handle"] is None

    def test_unknown_identifier_keeps_working_fallback(self) -> None:
        # Partial state is VALID: never invent a handle. A guessed deep link
        # 404s confidently; the browse page actually renders.
        rows = [self._row(identifier="never-seen")]
        assert apply_resolved_owners(rows, {"other": "x"}) == 0
        assert rows[0]["origin_url"] == CLAWHUB_BROWSE_URL


class TestMapHubRowCarriesOwnerHandle:
    def test_emits_owner_handle_key(self) -> None:
        # Without this key in the mapping, bulk_insert_mappings silently drops
        # the column and every reindex writes NULL.
        mapped = map_hub_row({"source": "clawhub", "identifier": "aigate", "name": "aigate"})
        assert "owner_handle" in mapped

    def test_owner_handle_none_when_snapshot_has_none(self) -> None:
        mapped = map_hub_row({"source": "clawhub", "identifier": "aigate", "name": "aigate"})
        assert mapped["owner_handle"] is None
        assert mapped["origin_url"] == CLAWHUB_BROWSE_URL

    def test_picks_up_owner_if_snapshot_starts_shipping_one(self) -> None:
        mapped = map_hub_row(
            {"source": "clawhub", "identifier": "aigate", "name": "aigate", "owner": "psyb0t"}
        )
        assert mapped["owner_handle"] == "psyb0t"
        assert mapped["origin_url"] == "https://clawhub.ai/psyb0t/skills/aigate"


# ── THE DURABILITY TEST ──────────────────────────────────────────────────


class TestOwnerHandleSurvivesReindex:
    """The backfill must outlive the nightly reindex that deletes every row.

    ``bulk_upsert_skills`` does ``DELETE FROM federation_hub_skills`` then
    re-inserts from a snapshot that has no owner field. Without the
    carry-forward, the 03:00 cron silently reverts all 69,150 repaired links
    within a day — and every one of them keeps answering HTTP 200 while broken,
    so nothing alarms.

    RED-PROOF: pass ``preserve_owner_handles=False`` (simulating the pre-fix
    code path) and ``test_red_proof_without_carry_forward_the_fix_is_lost``
    shows the resolution being destroyed.
    """

    @pytest.fixture()
    def seeded_db(self, db_session: Any) -> Any:
        from app.models import FederationHubSkill

        db_session.add(
            FederationHubSkill(
                slug="clawhub-aigate",
                title="aigate",
                source="hermes-hub",
                upstream_source="clawhub",
                identifier="aigate",
                origin_url="https://clawhub.ai/psyb0t/skills/aigate",
                owner_handle="psyb0t",
                install_path="deep_link",
            )
        )
        db_session.commit()
        return db_session

    def _snapshot_rows(self) -> list[dict[str, Any]]:
        """Rows as a fresh snapshot parse produces them — no owner anywhere."""
        row = map_hub_row({"source": "clawhub", "identifier": "aigate", "name": "aigate"})
        row["slug"] = "clawhub-aigate"
        assert row["owner_handle"] is None, "fixture invalid: snapshot must not carry an owner"
        return [row]

    def test_owner_handle_survives_a_reindex(self, seeded_db: Any) -> None:
        from app.models import FederationHubSkill
        from app.services.hub_snapshot import bulk_upsert_skills

        bulk_upsert_skills(seeded_db, self._snapshot_rows())
        seeded_db.commit()

        row = (
            seeded_db.query(FederationHubSkill)
            .filter(FederationHubSkill.identifier == "aigate")
            .one()
        )
        assert row.owner_handle == "psyb0t", "reindex destroyed the resolved owner"
        assert row.origin_url == "https://clawhub.ai/psyb0t/skills/aigate"
        assert not row.origin_url.startswith(BARE_FORM)

    def test_red_proof_without_carry_forward_the_fix_is_lost(self, seeded_db: Any) -> None:
        """Neutralising the carry-forward must visibly destroy the repair.

        This is the RED half of the proof, asserted rather than described: it
        pins WHY `preserve_owner_handles` exists, so a future refactor that
        drops it fails here instead of in production a day later.
        """
        from app.models import FederationHubSkill
        from app.services.hub_snapshot import bulk_upsert_skills

        bulk_upsert_skills(seeded_db, self._snapshot_rows(), preserve_owner_handles=False)
        seeded_db.commit()

        row = (
            seeded_db.query(FederationHubSkill)
            .filter(FederationHubSkill.identifier == "aigate")
            .one()
        )
        assert row.owner_handle is None
        assert row.origin_url == CLAWHUB_BROWSE_URL

    def test_load_resolved_owner_handles_rejects_unsafe_stored_values(self, db_session: Any) -> None:
        """Re-validate on the way OUT of the DB, not just on the way in.

        A row written by an older, looser code path must not be interpolated
        into a URL we publish.
        """
        from app.models import FederationHubSkill
        from app.services.hub_snapshot import load_resolved_owner_handles

        db_session.add(
            FederationHubSkill(
                slug="clawhub-evil",
                title="evil",
                source="hermes-hub",
                upstream_source="clawhub",
                identifier="evil",
                origin_url=CLAWHUB_BROWSE_URL,
                owner_handle="../../etc",
                install_path="deep_link",
            )
        )
        db_session.commit()

        assert "evil" not in load_resolved_owner_handles(db_session)


# ── Targeted sweep (the coverage fix) ────────────────────────────────────


class TestTokensFromIdentifiers:
    """Terms derived from the slugs we NEED, not from a blind guess.

    Measured on the real prod set: the generic seed list covered only 20.1% of
    69,150 identifiers, because ClawHub search saturates any one query at ~1,026
    results — broad terms keep returning the same popular skills and never reach
    the long tail. Identifier-derived terms resolve ~300 rows per call.
    """

    def test_orders_by_frequency(self) -> None:
        from app.services.clawhub_owner_bulk import tokens_from_identifiers

        ids = ["openclaw-a", "openclaw-b", "openclaw-c", "feishu-x", "feishu-y", "solo-z"]
        assert tokens_from_identifiers(ids)[:2] == ["openclaw", "feishu"]

    def test_takes_the_leading_segment(self) -> None:
        from app.services.clawhub_owner_bulk import tokens_from_identifiers

        assert tokens_from_identifiers(["polymarket-price-feed"]) == ["polymarket"]

    def test_drops_unsafe_and_too_short_tokens(self) -> None:
        from app.services.clawhub_owner_bulk import tokens_from_identifiers

        out = tokens_from_identifiers(["../evil-x", "a-short", "good-one"])
        assert "good" in out
        assert "../evil" not in out
        assert "a" not in out  # below min_length

    def test_handles_non_string_input(self) -> None:
        from app.services.clawhub_owner_bulk import tokens_from_identifiers

        assert tokens_from_identifiers(["ok-x", None, 42]) == ["ok"]  # type: ignore[list-item]


class TestTargetedOwnerSweep:
    def test_resolves_wanted_slugs_and_stops_when_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import clawhub_owner_bulk as mod

        calls: list[str] = []

        def _fake(url: str, **k: Any) -> dict[str, Any]:
            calls.append(url)
            return {"results": [
                {"slug": "openclaw-a", "ownerHandle": "oc"},
                {"slug": "openclaw-b", "ownerHandle": "oc"},
            ]}

        monkeypatch.setattr(mod, "_get_json", _fake)
        out = mod.targeted_owner_sweep({"openclaw-a", "openclaw-b"}, delay=0)

        assert out == {"openclaw-a": "oc", "openclaw-b": "oc"}
        # Everything wanted was resolved by the first term, so it must stop
        # rather than keep burning calls on an empty remainder.
        assert len(calls) == 1

    def test_respects_the_term_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        calls: list[str] = []

        def _fake(url: str, **k: Any) -> dict[str, Any]:
            calls.append(url)
            return {"results": []}  # never resolves anything

        monkeypatch.setattr(mod, "_get_json", _fake)
        wanted = {f"fam{i}-x" for i in range(50)}
        mod.targeted_owner_sweep(wanted, max_terms=5, delay=0)
        assert len(calls) == 5, "term budget not enforced — would hammer upstream"

    def test_keeps_previously_known_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(mod, "_get_json", lambda *a, **k: {"results": []})
        out = mod.targeted_owner_sweep({"x-1"}, known={"prior": "owner"}, delay=0)
        assert out["prior"] == "owner"

    def test_upstream_failure_degrades_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(mod, "_get_json", lambda *a, **k: None)
        out = mod.targeted_owner_sweep({"a-1", "b-2"}, max_terms=3, delay=0)
        assert out == {}, "a dead upstream must yield nothing, not raise"

    def test_never_records_unsafe_owner_from_upstream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(
            mod, "_get_json",
            lambda *a, **k: {"results": [{"slug": "evil-1", "ownerHandle": ".."}]},
        )
        out = mod.targeted_owner_sweep({"evil-1"}, delay=0)
        assert out == {}, "dot-only owner would mint the soft-404 bare form"


# ── Parallel tail resolution (the 99% chase) ─────────────────────────────


class TestResolveTailParallel:
    """Concurrency is the only lever left once the slug families are exhausted.

    Measured live 2026-07-26: exact-slug search and the detail endpoint BOTH
    yield ~1.0 resolutions per call on the long tail, so the endpoint is not the
    bottleneck — serialisation is. Throughput measured on a 24-slug sample:

        workers=1   4.01 s/call
        workers=6   0.46 s/call
        workers=12  0.20 s/call   (zero failures at every level)

    ~20x, i.e. a ~20k tail drops from ~22 hours to ~35 minutes.
    """

    def _stub(self, mapping: dict[str, str]):
        def _fake(url: str, **k: Any) -> dict[str, Any]:
            import urllib.parse as up

            q = up.parse_qs(up.urlparse(url).query).get("q", [""])[0]
            owner = mapping.get(q)
            if owner is None:
                return {"results": []}
            return {"results": [{"slug": q, "ownerHandle": owner}]}

        return _fake

    def test_resolves_every_known_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        mapping = {f"slug-{i}": f"owner{i}" for i in range(25)}
        monkeypatch.setattr(mod, "_get_json", self._stub(mapping))
        out = mod.resolve_tail_parallel(list(mapping), workers=4)
        assert out == mapping

    def test_unresolvable_slugs_are_absent_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Absent => caller demotes to the browse fallback, a WORKING link.
        # Inventing a handle would mint a confident 404 instead.
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(mod, "_get_json", self._stub({"known-a": "ann"}))
        out = mod.resolve_tail_parallel(["known-a", "ghost-b"], workers=2)
        assert out == {"known-a": "ann"}

    def test_requires_an_exact_slug_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A near-miss row must NOT be attributed to the slug we asked for.

        Search is fuzzy: querying `paystack` also returns `paystack-payments`.
        Accepting the first row would assign the wrong owner and mint a deep
        link to somebody else's page — a wrong answer is worse than none.
        """
        from app.services import clawhub_owner_bulk as mod

        def _fuzzy(url: str, **k: Any) -> dict[str, Any]:
            return {"results": [{"slug": "something-else", "ownerHandle": "wrong"}]}

        monkeypatch.setattr(mod, "_get_json", _fuzzy)
        assert mod.resolve_tail_parallel(["wanted"], workers=2) == {}

    def test_rejects_unsafe_owner_from_upstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(
            mod, "_get_json",
            lambda *a, **k: {"results": [{"slug": "x", "ownerHandle": ".."}]},
        )
        assert mod.resolve_tail_parallel(["x"], workers=2) == {}

    def test_hostile_slugs_never_reach_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import clawhub_owner_bulk as mod

        called: list[str] = []
        monkeypatch.setattr(
            mod, "_get_json", lambda url, **k: (called.append(url), {"results": []})[1]
        )
        assert mod.resolve_tail_parallel(["../../etc/passwd", "a b", ".."], workers=2) == {}
        assert called == []

    def test_upstream_outage_yields_nothing_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(mod, "_get_json", lambda *a, **k: None)
        assert mod.resolve_tail_parallel(["a", "b", "c"], workers=3) == {}

    def test_one_failure_does_not_abort_the_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single bad slug must not lose the other 19,999 results."""
        from app.services import clawhub_owner_bulk as mod

        def _one_explodes(url: str, **k: Any) -> Any:
            if "boom" in url:
                return None
            import urllib.parse as up

            q = up.parse_qs(up.urlparse(url).query).get("q", [""])[0]
            return {"results": [{"slug": q, "ownerHandle": "ok"}]}

        monkeypatch.setattr(mod, "_get_json", _one_explodes)
        out = mod.resolve_tail_parallel(["good-1", "boom", "good-2"], workers=3)
        assert out == {"good-1": "ok", "good-2": "ok"}

    def test_worker_count_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never let a caller point an unbounded thread pool at someone else's API."""
        from app.services import clawhub_owner_bulk as mod

        monkeypatch.setattr(mod, "_get_json", self._stub({"a": "x"}))
        # Absurd values must not raise or spawn thousands of threads.
        assert mod.resolve_tail_parallel(["a"], workers=99999) == {"a": "x"}
        assert mod.resolve_tail_parallel(["a"], workers=0) == {"a": "x"}
        assert mod.MAX_TAIL_WORKERS <= 16

    def test_empty_input_makes_no_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import clawhub_owner_bulk as mod

        called: list[str] = []
        monkeypatch.setattr(
            mod, "_get_json", lambda url, **k: (called.append(url), None)[1]
        )
        assert mod.resolve_tail_parallel([], workers=4) == {}
        assert called == []


# ── Transient-failure retry ──────────────────────────────────────────────


class TestTransientRetry:
    """Observed live: 3 of 600 sweep calls returned 503 under sustained load.

    Each sweep term is visited exactly once, so a dropped call means that term's
    whole slug family stays unresolved — silently, since the fallback still
    renders. Cheap to retry; invisible if you don't.
    """

    def test_retries_once_on_503_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        from app.services import clawhub_owner_bulk as mod

        calls = {"n": 0}

        def _flaky(req: Any, timeout: int = 0) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

            class _R:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
                def read(self_inner): return b'{"results": []}'

            return _R()

        monkeypatch.setattr(mod.urllib.request, "urlopen", _flaky)
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

        assert mod._get_json("https://example.invalid/x") == {"results": []}
        assert calls["n"] == 2, "did not retry the transient 503"

    def test_gives_up_after_the_retry_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        from app.services import clawhub_owner_bulk as mod

        calls = {"n": 0}

        def _always_503(req: Any, timeout: int = 0) -> Any:
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(mod.urllib.request, "urlopen", _always_503)
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

        assert mod._get_json("https://example.invalid/x") is None
        assert calls["n"] == 2, "retry budget not bounded"

    def test_does_not_retry_a_permanent_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        from app.services import clawhub_owner_bulk as mod

        calls = {"n": 0}

        def _404(req: Any, timeout: int = 0) -> Any:
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(mod.urllib.request, "urlopen", _404)
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

        assert mod._get_json("https://example.invalid/x") is None
        assert calls["n"] == 1, "wasted a retry on a permanent status"


# ── The invariant the whole sprint exists to hold ────────────────────────


#: The acceptance gate for issue #141. The plan states it as:
#:   select ... where origin_url ~ '^https://clawhub\.ai/skills/' -> 0
#:
#: That BROAD form has a false positive, found by running it across all 69,150
#: real prod identifiers: ClawHub has a publisher whose handle is literally
#: ``skills`` (verified live — `GET /api/v1/skills/9` returns
#: `owner.handle == "skills"` with a real userId). Its legitimate owner-scoped
#: deep link is ``https://clawhub.ai/skills/skills/9``, which the broad regex
#: flags as broken.
#:
#: Evidence it is a REAL page, not a soft-404 (body size discriminates, since
#: every ClawHub 404 answers HTTP 200):
#:     known-good  /hades4501/skills/ai-humanizer-2-1-0 -> 104,243 B, 0 redirects
#:     known-bad   /skills/ai-humanizer-2-1-0           ->  63,638 B, 1 redirect
#:     disputed    /skills/skills/9                     ->  83,449 B, 0 redirects
#:
#: The precise discriminator is SEGMENT COUNT, not prefix: the bare soft-404
#: form is exactly two path segments (``/skills/<slug>``), while every
#: owner-scoped link has three (``/<owner>/skills/<slug>``). Anchoring the end
#: of the pattern encodes that and drops the false positive — verified to trip
#: 0 times across all 69,150 prod rows.
GATE_RX = re.compile(r"^https://clawhub\.ai/skills/[^/]+$")


class TestAcceptanceGateIsReachable:
    """Every possible outcome must clear the issue-#141 gate.

    Measured coverage after a 600-term sweep is 71.1% — so ~20k rows will end a
    run WITHOUT a resolved owner. If those rows kept their original
    `/skills/<slug>` value they would (a) keep advertising the confirmed
    soft-404 and (b) make the gate permanently unreachable no matter how many
    times the backfill runs.

    The browse-page fallback has no trailing slash, so it does not match the
    gate regex — that is load-bearing, not incidental, and is asserted here.
    """

    def test_browse_fallback_clears_the_gate(self) -> None:
        assert GATE_RX.match(CLAWHUB_BROWSE_URL) is None

    def test_bare_form_trips_the_gate(self) -> None:
        # Sanity: the gate actually detects the thing it exists to detect.
        assert GATE_RX.match("https://clawhub.ai/skills/aigate") is not None

    def test_owner_literally_named_skills_is_not_a_false_positive(self) -> None:
        """ClawHub really has a publisher whose handle is ``skills``.

        Its legitimate deep link is ``/skills/skills/9``, which the plan's
        broad prefix regex flags as broken. Verified live: the URL serves
        83,449 B with zero redirects, versus 63,638 B and one redirect for a
        confirmed soft-404 — it is a real page. Segment count, not prefix, is
        the correct discriminator.
        """
        assert GATE_RX.match(clawhub_skill_url("9", "skills")) is None

    def test_resolved_deep_link_clears_the_gate(self) -> None:
        assert GATE_RX.match(clawhub_skill_url("aigate", "psyb0t")) is None

    @pytest.mark.parametrize(
        "slug,owner",
        [
            ("aigate", "psyb0t"),   # fully resolved
            ("aigate", None),       # unresolved -> browse fallback
            ("aigate", ".."),       # hostile owner -> browse fallback
            (None, "psyb0t"),       # missing identifier -> browse fallback
            ("bad/slug", "psyb0t"), # hostile slug -> browse fallback
        ],
    )
    def test_no_reachable_output_trips_the_gate(self, slug: Any, owner: Any) -> None:
        assert GATE_RX.match(clawhub_skill_url(slug, owner)) is None


class TestNeverMintsTheBareForm:
    @pytest.mark.parametrize("owner", [None, "", "  ", "bad/owner", "..", "a?b", "a#b", "x" * 200])
    def test_no_owner_shape_produces_the_soft_404(self, owner: Any) -> None:
        url = clawhub_skill_url("aigate", owner)
        assert url == CLAWHUB_BROWSE_URL
        assert not url.startswith(BARE_FORM) or url.rstrip("/") == CLAWHUB_BROWSE_URL

    @pytest.mark.parametrize("dots", [".", "..", "...", "...."])
    def test_dot_only_owner_is_rejected(self, dots: str) -> None:
        """Regression: `..` collapsed the URL back into the bare soft-404 form.

        `is_safe_token` allows `.` because real handles contain it (llama.cpp,
        next.js). A DOT-ONLY token is a relative path segment, not a name:
        `clawhub_skill_url("aigate", "..")` produced
        `https://clawhub.ai/../skills/aigate`, which normalises to the bare form.
        Verified live 2026-07-26: that URL 307s to /skills/skills/aigate — the
        exact soft-404 this module exists to prevent, and it answers HTTP 200 so
        nothing downstream would have caught it.
        """
        from app.services.clawhub_url import is_safe_token

        assert is_safe_token(dots) is False
        assert clawhub_skill_url("aigate", dots) == CLAWHUB_BROWSE_URL

    @pytest.mark.parametrize("legit", ["llama.cpp", "next.js", "a..b", "user.name-1", "hades4501"])
    def test_legitimate_dotted_handles_still_work(self, legit: str) -> None:
        """The traversal fix must not break real handles that contain dots."""
        url = clawhub_skill_url("aigate", legit)
        assert url == f"https://clawhub.ai/{legit}/skills/aigate"
