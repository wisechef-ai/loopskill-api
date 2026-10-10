"""ah_1010: a 3+ subject-word query that every source answers with zero rows
retries with one word allowed to miss, and says so (``relaxed: true``).

Live defect (2026-10-10, 0.9.62): ``postgres index advisor`` returned 0 rows on
REST metasearch and MCP ``loopskill_search`` while ``postgres index``,
``index advisor`` and ``postgres advisor`` each returned 10+. Every source
required every word (hub index) or the whole phrase (adapters), and the empty
answer was then recorded as a "missing skill" demand signal it was not.
"""

from __future__ import annotations

import pytest

from app.services import mcp_federated_search as mfs
from app.services import metasearch_compute as mc
from app.services.hub_local_search import relaxable_tokens, search_hub_index, search_hub_index_relaxed


@pytest.fixture(autouse=True)
def _clean_cache():
    from app.services.metasearch_cache import get_cache

    get_cache().invalidate()
    yield
    get_cache().invalidate()


def _hub_row(db, slug: str, title: str, description: str):
    from app.models import FederationHubSkill

    db.add(
        FederationHubSkill(
            slug=slug,
            title=title,
            description=description,
            source="hermes-hub",
            upstream_source="skills-sh",
            identifier=f"skills-sh/o/r/{slug}",
            origin_url=f"https://www.skills.sh/o/r/{slug}",
            install_path="fetch_origin",
            repo="o/r",
            path=slug,
        )
    )


@pytest.fixture
def hub(db_session, monkeypatch):
    """Rows covering 2 of {postgres, index, advisor}, one covering 1, one none."""
    import app.database as database

    _hub_row(db_session, "phy-db-index-advisor", "index advisor", "suggests missing database indexes")
    _hub_row(db_session, "postgres-index-tuning", "postgres index tuning", "btree and gin helpers")
    _hub_row(db_session, "postgres-backup", "postgres backup", "nightly dumps")
    _hub_row(db_session, "kubernetes-helper", "k8s", "cluster helper")
    db_session.commit()
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    return db_session


def _empty_fanout(monkeypatch):
    from app.services import metasearch_fanout as fanout

    monkeypatch.setattr(
        fanout,
        "fan_out",
        lambda q, sources=None, **_: fanout.FanoutOutput(
            pairs=[], sources_ok=["skills-sh"], sources_degraded=[]
        ),
    )


# ── the gate: which queries may relax ────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("postgres index advisor", ["postgres", "index", "advisor"]),
        ("postgres index advisor skill", ["postgres", "index", "advisor"]),  # generic word dropped
        ("postgres index skill", []),  # 2 subject words: dropping one is a different search
        ("the postgres index", []),  # stopword does not count
        ("postgres postgres index", []),  # duplicates count once
        ("", []),
        (None, []),
    ],
)
def test_only_three_subject_words_or_more_relax(query, expected):
    assert relaxable_tokens(query) == expected


# ── hub index: strict stays strict, relaxed lets one word miss ───────────────


def test_strict_search_still_requires_every_word(hub):
    assert search_hub_index("postgres index advisor", limit=10) == []


def test_relaxed_search_returns_rows_missing_one_word_and_never_two(hub):
    slugs = {s.slug for s in search_hub_index_relaxed("postgres index advisor", limit=10)}
    assert slugs == {"phy-db-index-advisor", "postgres-index-tuning"}


def test_relaxed_search_ranks_rows_covering_more_words_first(hub):
    _hub_row(hub, "pg-index-advisor", "postgres index advisor", "all three words")
    hub.commit()
    rows = search_hub_index("postgres index advisor", limit=10, min_match=1)
    assert rows[0].slug == "pg-index-advisor"
    assert {r.slug for r in rows[1:3]} == {"phy-db-index-advisor", "postgres-index-tuning"}
    assert rows[-1].slug == "postgres-backup"


# ── the shared REST/MCP compute ──────────────────────────────────────────────


def test_build_unified_retries_relaxed_and_flags_every_card(hub, monkeypatch):
    _empty_fanout(monkeypatch)
    skills, ok, _ = mc.build_unified(hub, "postgres index advisor")
    assert {s["slug"] for s in skills} >= {"phy-db-index-advisor", "postgres-index-tuning"}
    assert "postgres-backup" not in {s["slug"] for s in skills}
    assert all(s["relaxed"] is True for s in skills)
    assert "skills-sh" in ok


def test_build_unified_does_not_relax_a_two_word_query(hub, monkeypatch):
    _empty_fanout(monkeypatch)
    skills, _, _ = mc.build_unified(hub, "kafka advisor")
    assert skills == []


def test_build_unified_exact_answers_are_never_marked_relaxed(hub, monkeypatch):
    from app.services import metasearch_fanout as fanout

    exact = search_hub_index("index advisor", limit=1)
    monkeypatch.setattr(
        fanout,
        "fan_out",
        lambda q, sources=None, **_: fanout.FanoutOutput(
            pairs=[(exact[0], {})], sources_ok=["hermes-hub"], sources_degraded=[]
        ),
    )
    skills, _, _ = mc.build_unified(hub, "postgres index advisor")
    assert skills and not any(s.get("relaxed") for s in skills)


def test_a_failing_relaxed_pass_keeps_the_empty_answer(hub, monkeypatch):
    import app.services.hub_local_search as hls

    _empty_fanout(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(hls, "search_hub_index_relaxed", boom)
    assert mc.build_unified(hub, "postgres index advisor")[0] == []


# ── MCP local floor + compact rows carry the label ──────────────────────────


def test_mcp_local_floor_relaxes_and_labels(hub):
    rows = mfs.local_floor("postgres index advisor", limit=5, exclude_slugs=set())
    assert {r["slug"] for r in rows} == {"phy-db-index-advisor", "postgres-index-tuning"}
    assert all(r["relaxed"] is True for r in rows)


def test_mcp_local_floor_exact_rows_carry_no_relaxed_key(hub):
    rows = mfs.local_floor("index advisor", limit=5, exclude_slugs=set())
    assert rows and all("relaxed" not in r for r in rows)


def test_compact_row_keeps_the_relaxed_label_from_a_cached_card():
    card = {"slug": "x", "title": "x", "source": "hermes-hub", "install_ref": "hermes-hub:x", "relaxed": True}
    labelled = mfs.compact_row(card)
    plain = mfs.compact_row({**card, "relaxed": False})
    assert labelled is not None and labelled["relaxed"] is True
    assert plain is not None and "relaxed" not in plain


# ── REST: response flag + no false demand signal ────────────────────────────


def test_rest_response_is_flagged_relaxed_and_records_no_demand(client, hub, monkeypatch):
    from app.models import MissingSkillQuery

    _empty_fanout(monkeypatch)
    body = client.get("/api/skills/metasearch", params={"q": "postgres index advisor"}).json()
    assert body["result_count"] >= 2
    assert body["relaxed"] is True
    assert hub.query(MissingSkillQuery).count() == 0

    again = client.get("/api/skills/metasearch", params={"q": "postgres index advisor"}).json()
    assert again["cache"]["cache_hit"] is True
    assert again["relaxed"] is True


def test_rest_exact_answer_is_not_flagged(client, hub, monkeypatch):
    _empty_fanout(monkeypatch)
    body = client.get("/api/skills/metasearch", params={"q": "kafka advisor"}).json()
    assert body["result_count"] == 0
    assert body["relaxed"] is False
