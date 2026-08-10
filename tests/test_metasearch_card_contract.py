"""Tests for the §5 unified render contract (metasearch_0710 P2).

Pins: badge/chip derivation, one-action-per-card, dead-card drop (fail-closed),
no catalog count, curated-gold vs community-neutral honesty invariant, latency
meta."""

from __future__ import annotations

from app.services.metasearch_card_contract import (
    ACTION_DEPLOY,
    ACTION_PREVIEW,
    ACTION_VIEW_ORIGIN,
    RenderContractMeta,
    apply_card_contract,
    card_from_unified,
)


def _card(**over) -> dict:
    base = {
        "canonical_id": "skills-sh:o/r/s",
        "title": "Agent Browser",
        "description": "Browser automation",
        "source": "skills-sh",
        "install_ref": "skills-sh:o--r--s",
        "quality": "community",
        "deployable": True,
        "popularity": 42,
        "rank_score": 0.8,
    }
    base.update(over)
    return base


# ── badge + chip derivation (§5.2) ───────────────────────────────────────────


def test_curated_gets_gold_chip():
    c = card_from_unified(_card(source="recipes", quality="curated"))
    assert c.quality_chip_tone == "gold"
    assert c.quality_chip_label == "Curated"
    assert c.source_badge == "LoopSkill"


def test_community_gets_neutral_chip():
    c = card_from_unified(_card(source="skills-sh", quality="community"))
    assert c.quality_chip_tone == "neutral"
    assert c.source_badge == "skills.sh"


def test_chip_is_derived_not_freeform():
    """§5.4 honesty invariant: a community card can NOT claim the gold chip even
    if the input dict tries — the chip is derived from quality only."""
    c = card_from_unified(_card(source="skills-sh", quality="community", quality_chip={"tone": "gold"}))
    assert c.quality_chip_tone == "neutral", "community can never render as gold"


def test_github_facet_badge():
    c = card_from_unified(_card(source="github-anthropic"))
    assert c.source_badge == "GitHub · Anthropic"


def test_unknown_source_badge_titlecased_not_raw_slug():
    c = card_from_unified(_card(source="new-source"))
    assert c.source_badge == "New Source"


# ── one action per card (§5.3) ───────────────────────────────────────────────


def test_deployable_card_action_is_deploy():
    c = card_from_unified(_card(deployable=True))
    assert c.primary_action == ACTION_DEPLOY
    assert c.actionable is True


def test_non_deployable_card_action_is_preview():
    c = card_from_unified(_card(deployable=False, source="clawhub", install_ref="clawhub:x"))
    assert c.primary_action == ACTION_PREVIEW


def test_card_with_no_install_ref_is_not_actionable():
    c = card_from_unified(_card(install_ref=""))
    assert c.actionable is False


# ── dead-card drop (§5.3 fail-closed) ────────────────────────────────────────


def test_apply_contract_drops_dead_cards():
    cards = [
        _card(source="skills-sh", install_ref="skills-sh:a--b--c"),
        _card(source="skills-sh", install_ref=""),
        _card(source="clawhub", install_ref="clawhub:x"),
    ]
    out = apply_card_contract(cards)
    assert len(out) == 2, "the card with no install_ref must be dropped (no dead cards)"
    assert all(c["actionable"] for c in out)


def test_apply_contract_preserves_order_and_adds_fields():
    cards = [_card(canonical_id="a", rank_score=0.9), _card(canonical_id="b", rank_score=0.5)]
    out = apply_card_contract(cards)
    assert [c["canonical_id"] for c in out] == ["a", "b"]
    # contract fields added, P0 fields kept
    assert "source_badge" in out[0] and "quality_chip" in out[0] and "primary_action" in out[0]
    assert "popularity" in out[0]  # P0 field survives


# ── render meta (§5.1, Q2, §5.5) ─────────────────────────────────────────────


def test_render_meta_no_catalog_count():
    m = RenderContractMeta(latency_ms=120.0)
    d = m.to_dict()
    assert d["catalog_count_shown"] is False, "Spotify model — never a stored count"
    assert d["one_ranked_list"] is True


def test_render_meta_within_budget():
    assert RenderContractMeta(latency_ms=200.0).to_dict()["within_budget"] is True
    assert RenderContractMeta(latency_ms=2000.0).to_dict()["within_budget"] is False


# ── council P2 MUST: resolvability, not string-presence (github-oss dead card) ──


def test_github_oss_deployable_but_unresolvable_falls_back_to_view_origin():
    """P3.9 (bundles_0811): github-oss can be deployable=True with a non-empty
    install_ref but has NO origin fetcher (needs prod token). Before P3.9 this
    card was dropped entirely — the deep-link surfacing decision (a) means it
    now renders as a labelled, non-deployable 'view origin' card instead of
    vanishing (github-oss's install_path is deep_link when no origin fetcher
    exists / no license is known)."""
    card = _card(
        source="github-oss",
        install_ref="github-oss:owner--repo",
        deployable=True,
        install_path="deep_link",
        origin_url="https://github.com/owner/repo",
    )
    c = card_from_unified(card)
    assert c.actionable is True, "P3.9: deep-link-only must surface, not disappear"
    assert c.primary_action == ACTION_VIEW_ORIGIN
    assert c.installable is False, "reach without a resolvable install instruction"
    assert c.deployable is False, "never deployable — defense in depth"
    assert "not installable" in c.action_label
    out = apply_card_contract([card])
    assert len(out) == 1
    assert out[0]["primary_action"] == ACTION_VIEW_ORIGIN


def test_github_oss_fetch_origin_path_still_dropped_without_fetcher():
    """A github-oss card whose install_path is fetch_origin (a real license was
    found) but has no registered origin fetcher must still fail closed — P3.9
    only changes the DEEP_LINK case, not fetch_origin resolvability."""
    card = _card(
        source="github-oss",
        install_ref="github-oss:owner--repo",
        deployable=True,
        install_path="fetch_origin",
        origin_url="https://github.com/owner/repo",
    )
    c = card_from_unified(card)
    assert c.actionable is False
    assert apply_card_contract([card]) == []


def test_resolvable_sources_stay_actionable():
    from app.services.metasearch_card_contract import _ref_is_resolvable

    assert _ref_is_resolvable("skills-sh", "skills-sh:o--r--s") is True
    assert _ref_is_resolvable("clawhub", "clawhub:x") is True
    assert _ref_is_resolvable("recipes", "recipes:mine") is True


def test_none_string_ref_is_not_actionable():
    """install_ref=None coerced to 'None' must NOT survive (council MUST tail)."""
    c = card_from_unified(_card(install_ref=None))
    assert c.actionable is False
    assert c.install_ref == "", "None ref must normalize to empty, not the string 'None'"


# ── council P2 R2: source/install_ref mismatch must fail closed ───────────────


def test_source_ref_mismatch_fails_closed():
    """Council R2: _ref_is_resolvable routes off the source DECODED FROM install_ref
    and requires it to match the display source, so a mismatch (display recipes,
    ref github-oss) is a dead card and must be dropped — not rendered."""
    from app.services.metasearch_card_contract import _ref_is_resolvable

    # false-positive dead card: display recipes but ref points at unresolvable github-oss
    assert _ref_is_resolvable("recipes", "github-oss:x") is False
    # malformed clawhub ref (blank slug) fails closed
    assert _ref_is_resolvable("clawhub", "clawhub:") is False
    # display github-oss but ref recipes: decoded=recipes but != display source → fail closed (spoof)
    assert _ref_is_resolvable("github-oss", "recipes:existing") is False
    # matching pair resolves
    assert _ref_is_resolvable("recipes", "recipes:existing") is True


def test_mismatched_card_is_dropped():
    card = _card(source="recipes", install_ref="github-oss:x", deployable=True)
    assert apply_card_contract([card]) == [], "source/ref mismatch dead card must be dropped"


def test_empty_display_source_fails_closed():
    """Council R3: an empty display source must NOT bypass the equality check —
    a recipes:x ref with an empty badge is still a source-spoofed card."""
    from app.services.metasearch_card_contract import _ref_is_resolvable

    assert _ref_is_resolvable("", "recipes:x") is False
    assert _ref_is_resolvable("", "skills-sh:o--r--s") is False
    # a card dict with empty source but a real ref must be dropped
    card = _card(source="", install_ref="recipes:x", deployable=True)
    assert apply_card_contract([card]) == []
