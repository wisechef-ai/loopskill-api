"""Unified render contract for metasearch cards (metasearch_0710 P2).

Plan §5 is "the platform must look intact" — external results render
INDISTINGUISHABLE in polish from curated. The frontend (Astro portal) renders the
cards, but the *contract* must be enforced SERVER-SIDE so the UI cannot diverge:
a card can only render what the API returns, so the API is where "one card,
source badge, quality chip, one action, no dead cards" is guaranteed.

This module maps each ``UnifiedSkill`` to a ``CardContract`` — the exact fields a
single unified card component needs — and enforces the §5 invariants:

- §5.2 **identical card + badge + chip**: every card carries a ``source_badge``
  (the source label) and a ``quality_chip`` (``curated`` gold / ``community``
  neutral). The chip is DERIVED from quality, never free-form — curated is always
  gold, external always neutral, so honesty can't be faked and curated can't be
  disguised as community or vice-versa.
- §5.3 **no dead cards**: every card exposes exactly ONE ``primary_action``
  (``deploy_to_fleet`` for a deployable card, ``preview_install`` otherwise). A
  card is ``actionable`` iff it has a resolvable action; the merge layer already
  fail-closes unresolvable external cards, and this contract asserts the property.
- §5.4 **curated wins ties + carries the chip**: enforced upstream in
  ``metasearch.rank`` (curated boost) + here (the gold chip is curated-only).
- §5.1 **one ranked list**: the contract is per-card and source-agnostic — there
  is no ``internal``/``external`` split field anywhere in the shape.

The card contract is additive metadata layered onto the existing UnifiedSkill
dict, so P0's response stays backward-compatible.
"""

from __future__ import annotations

from dataclasses import dataclass

# The two quality chips — DERIVED from UnifiedSkill.quality, never free-form.
# Curated is our gate (gold); community is surfaced honestly (neutral). This is
# the §5.4 honesty invariant expressed as a closed vocabulary.
_QUALITY_CHIP = {
    "curated": {"label": "Curated", "tone": "gold"},
    "community": {"label": "Community", "tone": "neutral"},
}

# Human-facing source badge labels (§5.2). A source with no explicit label falls
# back to a title-cased id so a new source never renders a raw slug.
_SOURCE_BADGE = {
    "recipes": "LoopSkill",
    "skills-sh": "skills.sh",
    "clawhub": "ClawHub",
    "github-oss": "GitHub",
    "hermes-hub": "Hermes Hub",
    "well-known": "Well-Known",
    "browse-sh": "browse.sh",
    "lobehub": "LobeHub",
}

# Primary action vocabulary (§5.3). Exactly one per card.
ACTION_DEPLOY = "deploy_to_fleet"  # deployable card, operator motion (the moat)
ACTION_PREVIEW = "preview_install"  # non-deployable (e.g. ClawHub) — preview + ad-hoc install


def _source_badge(source: str) -> str:
    if source in _SOURCE_BADGE:
        return _SOURCE_BADGE[source]
    if source.startswith("github"):
        # github-<facet> → "GitHub · <Facet>"
        facet = source.split("-", 1)[1] if "-" in source else ""
        return f"GitHub · {facet.title()}" if facet else "GitHub"
    return source.replace("-", " ").title()


@dataclass(frozen=True)
class CardContract:
    """The §5 render contract for ONE card. Every source maps into this shape;
    the frontend renders exactly these fields, so the API owns 'looks intact'."""

    canonical_id: str
    title: str
    description: str
    source: str
    source_badge: str  # §5.2 human label
    quality: str  # curated | community
    quality_chip_label: str  # §5.2 chip text (derived)
    quality_chip_tone: str  # gold | neutral (derived)
    primary_action: str  # §5.3 exactly one action
    actionable: bool  # §5.3 has a resolvable action → renderable
    install_ref: str
    deployable: bool
    popularity: int | None
    rank_score: float

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "source_badge": self.source_badge,
            "quality": self.quality,
            "quality_chip": {"label": self.quality_chip_label, "tone": self.quality_chip_tone},
            "primary_action": self.primary_action,
            "actionable": self.actionable,
            "install_ref": self.install_ref,
            "deployable": self.deployable,
            "popularity": self.popularity,
            "rank_score": self.rank_score,
        }


def _ref_is_resolvable(source: str, install_ref: str) -> bool:
    """§5.3 TRUE resolvability predicate — a card is actionable iff the P1 install
    route can actually resolve it, NOT merely because it carries a ref string.

    Council P2 MUST: ``bool(install_ref)`` was wrong — ``github-oss`` mints a
    deployable card with a non-empty ref whose origin fetcher is deliberately
    absent (needs a prod token), so it would render a deploy button that 404s.

    Council P2 R2: route the decision off the source DECODED FROM ``install_ref``
    (exactly as ``resolve_install`` does), not the card's display ``source`` — a
    mismatch (display ``recipes`` but ref ``github-oss:x``) would otherwise be
    called actionable here yet 404 at the route. We additionally require the
    decoded source to EQUAL the display source, so a spoofed/mismatched card
    fails closed. This makes the contract and the install endpoint provably agree.
    """
    if not install_ref or ":" not in install_ref:
        return False
    decoded_source, _, slug = install_ref.partition(":")
    decoded_source = decoded_source.strip()
    slug = slug.strip()
    if not decoded_source or not slug:
        return False
    # The ref's own source must match the card's display source (no spoofing).
    # Council R3: strict — an EMPTY display source must also fail closed (an empty
    # badge with a recipes:x ref is still a spoofed/mismatched card).
    if decoded_source != source:
        return False
    if decoded_source == "recipes":  # curated — resolved from the internal catalog
        return True
    if decoded_source == "clawhub":  # preview from ClawHub's own API (decision #6)
        return True
    # Fetch-origin sources: actionable iff a resolver is registered (P1 registry).
    from app.services.federation_install import get_origin_fetcher

    return get_origin_fetcher(decoded_source) is not None


def card_from_unified(card: dict) -> CardContract:
    """Map a UnifiedSkill dict (from metasearch.UnifiedSkill.to_dict) into the
    §5 CardContract, deriving the badge, chip, and action from the card's own
    fields — never free-form, so the contract can't be faked."""
    quality = card.get("quality", "community")
    chip = _QUALITY_CHIP.get(quality, _QUALITY_CHIP["community"])
    deployable = bool(card.get("deployable", False))
    raw_ref = card.get("install_ref")
    install_ref = str(raw_ref) if isinstance(raw_ref, str) else ""
    source = str(card.get("source", ""))
    # §5.3: actionable iff the P1 install route can genuinely RESOLVE this ref
    # (not just because a string is present — that let github-oss render a dead
    # deploy card). This is the true no-dead-cards gate.
    actionable = _ref_is_resolvable(source, install_ref)
    primary_action = ACTION_DEPLOY if (deployable and actionable) else ACTION_PREVIEW
    return CardContract(
        canonical_id=str(card.get("canonical_id", "")),
        title=str(card.get("title", "")),
        description=str(card.get("description", "") or ""),
        source=source,
        source_badge=_source_badge(source),
        quality=quality,
        quality_chip_label=chip["label"],
        quality_chip_tone=chip["tone"],
        primary_action=primary_action,
        actionable=actionable,
        install_ref=install_ref,
        deployable=deployable,
        popularity=card.get("popularity"),
        rank_score=float(card.get("rank_score", 0.0)),
    )


def apply_card_contract(skills: list[dict]) -> list[dict]:
    """Layer the §5 card contract onto each card dict and DROP non-actionable
    cards (§5.3 fail-closed — no dead cards). Returns the rendered card dicts,
    ranking order preserved. This is the 'looks intact' gate as a data contract.
    """
    out: list[dict] = []
    for s in skills:
        contract = card_from_unified(s)
        if not contract.actionable:
            # §5.3: a card with no resolvable action is NOT shown.
            continue
        # Merge the contract fields onto the existing card dict (backward-compat:
        # P0 fields stay; the contract adds source_badge/quality_chip/action).
        merged = dict(s)
        merged.update(contract.to_dict())
        out.append(merged)
    return out


@dataclass
class RenderContractMeta:
    """The §5 acceptance metadata attached to a metasearch response so a test (or
    the frontend) can assert the contract held: one list, no count, latency."""

    one_ranked_list: bool = True  # §5.1 — always true (no namespace split)
    catalog_count_shown: bool = False  # §5-Q2 Spotify model — never a stored count
    cards_dropped_dead: int = 0  # §5.3 — how many non-actionable cards were filtered
    latency_ms: float = 0.0  # §5.5 — measured server render time
    latency_budget_ms: int = 1500  # §5.5 — the external-reflow budget

    def to_dict(self) -> dict:
        return {
            "one_ranked_list": self.one_ranked_list,
            "catalog_count_shown": self.catalog_count_shown,
            "cards_dropped_dead": self.cards_dropped_dead,
            "latency_ms": round(self.latency_ms, 1),
            "latency_budget_ms": self.latency_budget_ms,
            "within_budget": self.latency_ms <= self.latency_budget_ms,
        }
