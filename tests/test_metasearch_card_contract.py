"""Tests for the §5 unified render contract (metasearch_0710 P2).

Pins: badge/chip derivation, one-action-per-card, dead-card drop (fail-closed),
no catalog count, curated-gold vs community-neutral honesty invariant, latency
meta."""

from __future__ import annotations

from app.services.metasearch_card_contract import (
    ACTION_DEPLOY,
    ACTION_PREVIEW,
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
    c = card_from_unified(_card(deployable=False, source="clawhub"))
    assert c.primary_action == ACTION_PREVIEW


def test_card_with_no_install_ref_is_not_actionable():
    c = card_from_unified(_card(install_ref=""))
    assert c.actionable is False


# ── dead-card drop (§5.3 fail-closed) ────────────────────────────────────────


def test_apply_contract_drops_dead_cards():
    cards = [_card(install_ref="skills-sh:a--b--c"), _card(install_ref=""), _card(install_ref="clawhub:x")]
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
