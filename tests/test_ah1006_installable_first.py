"""ah_1006 — installable rows rank above link-only rows of equal relevance.

Prod 2026-10-06, GET /api/skills/metasearch?q=pdf: ranks 0-24 were hermes-hub
rows (install_path=deep_link, deployable:false; metasearch/install -> 404) and
the installable ``gh:anthropics/skills/pdf`` / ``gh:openai/skills/pdf`` rows
(fetch_origin, install 200) sat at 26-27 of 30. ``rank()`` sorted
tier -> slug length -> popularity and never read install_path, so an agent sent
by the keyless install funnel saw only rows it could not install.

Contract: install_path is the SECOND key, right after the relevance tier.
  * same tier  -> installable first;
  * better tier always wins, even when it is link-only (relevance is primary);
  * the no-query browse path is unchanged.
"""

from __future__ import annotations

from app.services import unified_search as us
from app.services.metasearch import UnifiedSkill, rank


def _s(canonical_id: str, slug: str, *, source: str, install_path: str) -> UnifiedSkill:
    return UnifiedSkill(
        canonical_id=canonical_id,
        slug=slug,
        title=slug,
        description="",
        source=source,
        origin_url=f"https://example.com/{slug}",
        install_ref=f"{source}:{slug}",
        quality="community",
        deployable=install_path == "fetch_origin",
        install_path=install_path,
        popularity=None,
        license=None,
        updated_at=None,
    )


def _pdf_slate() -> list[UnifiedSkill]:
    hub = [
        _s(f"hermes-hub:{slug}", slug, source="hermes-hub", install_path="deep_link")
        for slug in ("pdf", "pdfs", "pdfmd", "pdf-cn", "pdf-merge")
    ]
    installable = [
        _s(
            "gh:anthropics/skills/pdf",
            "anthropics--skills--pdf",
            source="skills-sh",
            install_path="fetch_origin",
        ),
        _s("gh:openai/skills/pdf", "openai--skills--pdf", source="skills-sh", install_path="fetch_origin"),
    ]
    return [*hub, *installable]


def test_link_only_key_alone_same_tier_same_length_installable_first():
    # Isolates the install_path key: identical slug/title/score, so on the old
    # key the hub row won on source priority / title order or kept input order.
    link = _s("hermes-hub:pdf", "pdf", source="hermes-hub", install_path="deep_link")
    inst = _s("well-known:pdf", "pdf", source="hermes-hub", install_path="fetch_origin")
    assert [s.canonical_id for s in rank([link, inst], query="pdf")][0] == "well-known:pdf"


def test_q_pdf_exact_installable_rows_outrank_link_only_exact_row():
    ids = [s.canonical_id for s in rank(_pdf_slate(), query="pdf")]
    hub_exact = ids.index("hermes-hub:pdf")
    assert ids.index("gh:anthropics/skills/pdf") < hub_exact
    assert ids.index("gh:openai/skills/pdf") < hub_exact
    # Both exact-name installable rows lead the WHOLE slate, ahead of every hub
    # pdf* slug-prefix row (prod had them at 26-27 of 30). Popularity orders them.
    assert set(ids[:2]) == {"gh:anthropics/skills/pdf", "gh:openai/skills/pdf"}


def test_installable_rows_are_never_below_a_same_tier_link_only_row():
    from app.services.federation_relevance import relevance_tier
    from app.services.metasearch import _leaf_slug

    def tier(s: UnifiedSkill) -> int:
        return min(
            relevance_tier("pdf", slug=s.slug, title=s.title, description=s.description),
            relevance_tier("pdf", slug=_leaf_slug(s.slug), title=s.title, description=s.description),
        )

    ranked = rank(_pdf_slate(), query="pdf")
    for i, a in enumerate(ranked):
        for b in ranked[i + 1 :]:
            same_tier = tier(a) == tier(b)
            if same_tier:
                assert not (a.install_path == "deep_link" and b.install_path != "deep_link"), (
                    a.canonical_id,
                    b.canonical_id,
                )


def test_relevance_stays_primary_link_only_better_match_beats_installable_weaker_match():
    better = _s("hermes-hub:pdf", "pdf", source="hermes-hub", install_path="deep_link")
    weaker = _s("gh:x/skills/office-docs", "office-docs", source="skills-sh", install_path="fetch_origin")
    weaker = UnifiedSkill(**{**weaker.__dict__, "description": "convert pdf files"})
    ids = [s.canonical_id for s in rank([weaker, better], query="pdf")]
    assert ids == ["hermes-hub:pdf", "gh:x/skills/office-docs"]


def test_browse_path_without_query_is_unchanged():
    slate = _pdf_slate()
    # No query: popularity-only path, which ties on score, then source priority, then title.
    # It must not read install_path, so it must match the pre-fix key exactly.
    from app.services.metasearch import _source_priority

    expected = sorted(rank(slate), key=lambda s: (-s.rank_score, _source_priority(s.source), s.title.lower()))
    assert [s.canonical_id for s in rank(slate)] == [s.canonical_id for s in expected]


def test_leaf_slug_only_strips_namespace_escapes():
    from app.services.metasearch import _leaf_slug

    assert _leaf_slug("anthropics--skills--pdf") == "pdf"
    assert _leaf_slug("pdf-merge") == "pdf-merge"
    assert _leaf_slug("pdf") == "pdf"


def test_federated_relevance_prefers_deployable_within_bucket_only():
    hub = {"title": "pdf", "description": "", "slug": "pdf", "deployable": False}
    cached = {"title": "pdf", "description": "", "slug": "pdf", "deployable": True}
    assert us._federated_relevance(cached, "pdf") < us._federated_relevance(hub, "pdf")
    weaker = {"title": "office docs", "description": "pdf", "slug": "o", "deployable": True}
    assert us._federated_relevance(hub, "pdf") < us._federated_relevance(weaker, "pdf")
