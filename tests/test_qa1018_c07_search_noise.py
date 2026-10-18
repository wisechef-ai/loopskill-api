"""qa1018 C07 / BRK-5 — junk queries must not produce fuzzy "results".

Measured on prod 2026-10-18 (anon):

* ``GET /api/search?q=__`` (or ``%%``) returned the WHOLE public catalog
  (skills 20, loops 10, bundles 14, personalities 5, federated 20). Root cause:
  the user's query was interpolated raw into ``ILIKE '%{term}%'``, so ``_``
  and ``%`` acted as LIKE wildcards. The federated first_page leg already did
  a literal Python substring test, so the two legs disagreed.
* ``GET /api/skills/external?sources=...&q=' OR 1=1 --`` returned 20 unrelated
  skills.sh rows (sandbox-next, image-to-ui, ...). Root cause: the query was
  forwarded verbatim to fuzzy upstream search APIs, which treat 1-2 char /
  punctuation fragments (``or``, ``--``) as near-wildcards. Probed directly:
  skills.sh ``q=--`` and ``q=or`` both answer 100 popular rows.

Not bugs (left alone on purpose): ``<script>alert(1)</script>`` returned
github-oss SKILL.md files that literally contain that XSS payload, and
``../../etc/passwd`` returned path-traversal / LFI skills. Both are genuine
matches on real words (script, alert, etc, passwd).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.services.federation_live as fl
from app.models import Bundle, FederationHubSkill, Personality, Skill, Verifier
from app.services import unified_search as us
from app.services.external_fanout import run_external_fanout
from app.services.federation import ExternalSkill, InstallPath
from tests._app_factory import build_test_app

JUNK = ["__", "%%", "_%_", "' OR 1=1 --", "--", "''", "<>", "%"]


# ── the gate itself ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("q", JUNK + ["   ", "1=1", "'; --", "<!-- -->"])
def test_noise_queries_are_not_searchable(q):
    from app.services.federation_relevance import is_searchable_query

    assert is_searchable_query(q) is False


@pytest.mark.parametrize(
    "q",
    [
        "seo",
        "copywriting",
        "memory",
        "code review",
        # short real queries: 2-char words and language names must still search
        "ai",
        "qa",
        "ui",
        "c++",
        "c#",
        ".net",
        "渗透",
        # genuine words survive the surrounding markup / path syntax
        "<script>alert(1)</script>",
        "../../etc/passwd",
        "snake_case",
    ],
)
def test_real_queries_are_searchable(q):
    from app.services.federation_relevance import is_searchable_query

    assert is_searchable_query(q) is True


# ── /api/search: route + per-group LIKE escaping ────────────────────────────


@pytest.fixture()
def seeded(db_session):
    db = db_session
    for slug, title, desc in [
        ("seo-audit", "SEO Audit", "Audit a site for search engine optimisation."),
        ("copywriting", "Copywriting", "Write landing page copy."),
        ("agent-memory", "Agent Memory", "Persistent memory for agents."),
        ("snake_case-lint", "snake_case lint", "Lint identifiers."),
    ]:
        db.add(
            Skill(
                id=uuid4(),
                slug=slug,
                title=title,
                description=desc,
                category="marketing",
                is_public=True,
                is_archived=False,
            )
        )
    db.add(
        Verifier(
            id=uuid4(),
            slug="seo-loop",
            title="SEO Loop",
            description="Iterate on memory and copywriting.",
            success_condition="ok",
            verification_script="pytest -q",
            system_prompt="go",
            max_turns=5,
            stopping_criteria={"success": "a", "failure": "b", "budget": "c"},
            tool_allowlist=["terminal"],
            is_public=True,
            is_archived=False,
            run_count=1,
        )
    )
    db.add(
        Bundle(
            id=uuid4(), slug="seo-pack", name="SEO Pack", description="Copywriting kit", visibility="public"
        )
    )
    db.add(
        Personality(
            id=uuid4(),
            slug="memory-coach",
            title="Memory Coach",
            description="SEO-minded copywriting persona.",
            system_prompt="x",
            is_public=True,
            is_archived=False,
        )
    )
    for slug, title, desc in [
        ("owner--repo--seo", "seo", "SEO helper"),
        ("mem--repo--memory-bank", "memory-bank", "Memory bank"),
    ]:
        db.add(
            FederationHubSkill(
                slug=slug,
                title=title,
                description=desc,
                source="hermes-hub",
                origin_url=f"https://x.test/{slug}",
            )
        )
    db.flush()
    return db


@pytest.fixture()
def search_client(seeded, monkeypatch) -> TestClient:
    us._fed_cache.update({"data": None, "sources": None, "t": 0.0})
    app = build_test_app(db_session=seeded, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


_GROUPS = ("skills", "loops", "bundles", "personalities", "connectors", "federated")


def _slug_sets(body):
    return {g: sorted(r.get("slug") for r in body[g]) for g in _GROUPS}


@pytest.mark.parametrize("q", [j for j in JUNK if len(j) >= 2])  # 1-char already 422s
def test_junk_query_returns_no_results_in_any_group(search_client, q):
    res = search_client.get("/api/search", params={"q": q, "limit": 20})
    assert res.status_code == 200
    body = res.json()
    assert {g: body[g] for g in _GROUPS} == {g: [] for g in _GROUPS}, body
    assert body["federated_cache_status"] in ("warm", "cold")


# Baseline captured from the pre-fix code path on this exact fixture.
_BASELINE = {
    "seo": {
        "skills": ["seo-audit"],
        "loops": ["seo-loop"],
        "bundles": ["seo-pack"],
        "personalities": ["memory-coach"],
        "connectors": [],
        "federated": ["owner--repo--seo"],
    },
    "copywriting": {
        "skills": ["copywriting"],
        "loops": ["seo-loop"],
        "bundles": ["seo-pack"],
        "personalities": ["memory-coach"],
        "connectors": [],
        "federated": [],
    },
    "memory": {
        "skills": ["agent-memory"],
        "loops": ["seo-loop"],
        "bundles": [],
        "personalities": ["memory-coach"],
        "connectors": [],
        "federated": ["mem--repo--memory-bank"],
    },
}


@pytest.mark.parametrize("q", sorted(_BASELINE))
def test_real_queries_unchanged(search_client, q):
    res = search_client.get("/api/search", params={"q": q, "limit": 20})
    assert res.status_code == 200
    assert _slug_sets(res.json()) == _BASELINE[q]


def test_real_query_order_unchanged(search_client):
    """Ranking, not just membership: exact-prefix title still sorts first."""
    body = search_client.get("/api/search", params={"q": "seo", "limit": 20}).json()
    assert [r["slug"] for r in body["skills"]] == ["seo-audit"]
    assert [r["slug"] for r in body["federated"]] == ["owner--repo--seo"]


def test_underscore_is_literal_not_a_wildcard(seeded):
    """``snake_case`` must find the row that literally contains it, and ``s_o``
    must NOT match ``seo`` through a ``_`` single-char wildcard."""
    assert [r["slug"] for r in us.search_skills_group(seeded, "snake_case", 10)] == ["snake_case-lint"]
    assert us.search_skills_group(seeded, "s_o", 10) == []
    rows, _status = us.search_federated_group(seeded, "s_o", 10)
    assert rows == []


def test_percent_is_literal_in_every_group(seeded):
    for fn in (
        us.search_skills_group,
        us.search_loops_group,
        us.search_bundles_group,
        us.search_personalities_group,
        us.search_connectors_group,
    ):
        assert fn(seeded, "%e%", 10) == [], fn.__name__
    rows, _status = us.search_federated_group(seeded, "%e%", 10)
    assert rows == []


# ── /api/skills/external live fan-out: never forward noise upstream ─────────


class _Recorder:
    def __init__(self):
        self.calls: list[str] = []

    def search(self, query: str, limit: int = 20):
        self.calls.append(query)
        return [
            ExternalSkill(
                slug="sandbox-next",
                title="sandbox-next",
                source="skills-sh",
                install_path=InstallPath.DEEP_LINK,
                origin_url="https://skills.sh/x",
            )
        ]


@pytest.fixture(autouse=True)
def _clear_fanout_cache():
    fl._cache.clear()
    yield
    fl._cache.clear()


@pytest.mark.parametrize("q", ["' OR 1=1 --", "--", "%%", "''"])
def test_fanout_does_not_forward_noise_query_upstream(q):
    adapter = _Recorder()
    out = run_external_fanout([("skills-sh", adapter)], query=q, limit=20, per_source_deadline_s=2.0)
    assert adapter.calls == []
    res = out["skills-sh"]
    # Not a degradation: the source is healthy, the query simply has nothing to search.
    assert res.ok is True and res.skills == []


@pytest.mark.parametrize("q", ["seo", "ai", "<script>alert(1)</script>", "../../etc/passwd"])
def test_fanout_still_forwards_real_queries(q):
    adapter = _Recorder()
    out = run_external_fanout([("skills-sh", adapter)], query=q, limit=20, per_source_deadline_s=2.0)
    assert len(adapter.calls) == 1
    assert [s.slug for s in out["skills-sh"].skills] == ["sandbox-next"]


def test_fanout_empty_query_unchanged():
    """An empty query (first-boot browse) is not noise — it still reaches the adapter."""
    adapter = _Recorder()
    run_external_fanout([("skills-sh", adapter)], query="", limit=20, per_source_deadline_s=2.0)
    assert adapter.calls == [""]
