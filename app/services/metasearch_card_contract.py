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
  (``deploy_to_fleet`` for a deployable card, ``preview_install`` for an
  install-preview-able one, ``view_origin`` for a deep-link-only one — see P3.9
  below). A card is ``actionable`` iff it has a resolvable action; the merge
  layer already fail-closes unresolvable external cards, and this contract
  asserts the property.
- §5.4 **curated wins ties + carries the chip**: enforced upstream in
  ``metasearch.rank`` (curated boost) + here (the gold chip is curated-only).
- §5.1 **one ranked list**: the contract is per-card and source-agnostic — there
  is no ``internal``/``external`` split field anywhere in the shape.

**P3.9 (bundles_0811) — the deep-link surfacing decision, implemented.** The
open product question was: do DEEP_LINK-only results (github-oss with an
unresolved/absent license, a hermes-hub or well-known row whose license forbids
redistribution, a github-facet tap with an unlicensed skill) appear in
metasearch at all, or does indexing-without-surfacing quietly make them
invisible — "the same silent-failure class as the missing token" (plan P3.9)?

**Decision: (a) — they DO appear, unmissably labelled "not installable".**
Reach is the point of federation: finding a skill and going to its repo has
real value even when LoopSkill cannot resolve an install instruction for it
(no license, no coordinates, or a source that is deep-link-only by policy).
Before this change, `_ref_is_resolvable` required a registered origin fetcher
for every non-curated/non-clawhub source — a genuine github-oss deep-link row
had a non-empty `install_ref` but no fetcher, so `apply_card_contract` silently
dropped it. A source that indexed 30 rows and surfaced 0 of them is exactly
the "indexes but never surfaces" failure this phase closes.

Any DEEP_LINK card (other than ClawHub, which keeps its own richer inline
preview from its own API — see ``metasearch_install.resolve_clawhub_preview``)
now renders with ``primary_action=ACTION_VIEW_ORIGIN`` and an explicit
``action_label`` ("View on GitHub — not installable" etc.), is NEVER
``deployable`` (defense in depth — ``route_install`` already denies it),
and NEVER carries an ``installable=True`` claim. This is deliberately
DIFFERENT from a licence judgement (Q3, 2026-08-10: licence is recorded,
never enforced — no block, no warning, no "unverified licence" badge). The
label here is about INSTALLABILITY — we have no coordinates to resolve, or the
source is deep-link by policy — never about whether the licence is "safe".

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
ACTION_DEPLOY = "deploy_to_fleet"  # deployable card, fleet-deploy motion (the moat)
ACTION_PREVIEW = "preview_install"  # non-deployable (e.g. ClawHub) — preview + ad-hoc install
# P3.9 (bundles_0811) decision (a): a DEEP_LINK card (not ClawHub, which keeps
# its own richer inline preview) — reach without an install instruction. The
# card links to the origin and is unmissably labelled "not installable"; it is
# never deployable and never implies we have a resolvable install path.
ACTION_VIEW_ORIGIN = "view_origin"


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
    action_label: str  # P3.9: human-facing action copy (non-empty for view_origin)
    actionable: bool  # §5.3 has a resolvable action → renderable
    installable: bool  # P3.9: True iff the action resolves to a real install/preview,
    # False for a deep-link-only "view origin" card — the honest installability
    # signal, kept structurally distinct from `license` (Q3: licence is never a
    # gate, this field never reflects it).
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
            "action_label": self.action_label,
            "actionable": self.actionable,
            "installable": self.installable,
            "install_ref": self.install_ref,
            "deployable": self.deployable,
            "popularity": self.popularity,
            "rank_score": self.rank_score,
        }


def _decode_and_check(source: str, install_ref: str) -> tuple[str, str] | None:
    """Decode ``install_ref`` and verify its source matches the card's display
    ``source``. Returns ``(decoded_source, slug)`` or ``None`` on any malformed
    or spoofed/mismatched ref (council R2/R3: a mismatch fails closed under
    EVERY action type, not just the fetch-origin one — a deep-link card cannot
    borrow another source's identity either)."""
    if not install_ref or ":" not in install_ref:
        return None
    decoded_source, _, slug = install_ref.partition(":")
    decoded_source = decoded_source.strip()
    slug = slug.strip()
    if not decoded_source or not slug:
        return None
    if decoded_source != source:
        return None
    return decoded_source, slug


def _ref_is_resolvable(source: str, install_ref: str) -> bool:
    """§5.3 TRUE resolvability predicate for the fetch-origin/curated/ClawHub
    paths — a card is actionable iff the P1 install route can actually resolve
    it, NOT merely because it carries a ref string.

    Council P2 MUST: ``bool(install_ref)`` was wrong — ``github-oss`` mints a
    deployable card with a non-empty ref whose origin fetcher is deliberately
    absent (needs a prod token), so it would render a deploy button that 404s.

    Council P2 R2: route the decision off the source DECODED FROM ``install_ref``
    (exactly as ``resolve_install`` does), not the card's display ``source`` —
    handled by ``_decode_and_check`` above.

    Note: this function does NOT know about ``install_path`` and therefore does
    NOT cover the P3.9 deep-link-only path (``ACTION_VIEW_ORIGIN``) — that is
    decided separately in ``card_from_unified`` because it needs the
    ``install_path``/``origin_url`` fields this function isn't given. Kept as a
    standalone predicate because it is the exact check the P1 install route
    performs, so tests can assert contract/route agreement directly.
    """
    decoded = _decode_and_check(source, install_ref)
    if decoded is None:
        return False
    decoded_source, _slug = decoded
    if decoded_source == "recipes":  # curated — resolved from the internal catalog
        return True
    if decoded_source == "clawhub":  # preview from ClawHub's own API (decision #6)
        return True
    # Fetch-origin sources: actionable iff a resolver is registered (P1 registry).
    from app.services.federation_install import get_origin_fetcher

    return get_origin_fetcher(decoded_source) is not None


def _is_deep_link_reach_card(install_path: str, decoded_source: str) -> bool:
    """P3.9 (bundles_0811) decision (a): True iff this card is DEEP_LINK-only
    AND is neither ``recipes`` (never deep-link) nor ``clawhub`` (which keeps
    its own richer inline-preview action rather than a bare origin link).
    A deep-link card in this state gets ``ACTION_VIEW_ORIGIN`` — reach without
    an install instruction — instead of being silently dropped."""
    return install_path == "deep_link" and decoded_source not in ("recipes", "clawhub")


def _view_origin_label(source: str) -> str:
    """P3.9: the unmissable, honest action copy for a deep-link-only card. This
    is an INSTALLABILITY statement, never a licence judgement (Q3 supersedes
    the old 'unverified licence' badge idea — no licence wording belongs here)."""
    return f"View on {_source_badge(source)} — not installable"


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
    install_path = str(card.get("install_path") or "")
    origin_url = str(card.get("origin_url") or "")

    decoded = _decode_and_check(source, install_ref)
    actionable = False
    primary_action = ACTION_PREVIEW
    action_label = ""
    installable = False

    if decoded is not None:
        decoded_source, _slug = decoded
        if decoded_source == "recipes":
            actionable = True
            primary_action = ACTION_DEPLOY if deployable else ACTION_PREVIEW
            installable = True
        elif decoded_source == "clawhub":
            actionable = True
            primary_action = ACTION_PREVIEW
            installable = True
        elif _is_deep_link_reach_card(install_path, decoded_source) and origin_url.startswith(
            ("http://", "https://")
        ):
            # P3.9 decision (a): deep-link-only reach — labelled, never
            # deployable, never claimed installable. See module docstring.
            actionable = True
            primary_action = ACTION_VIEW_ORIGIN
            action_label = _view_origin_label(decoded_source)
            deployable = False  # defense in depth — route_install already denies this
            installable = False
        else:
            # §5.3: actionable iff the P1 install route can genuinely RESOLVE
            # this ref (not just because a string is present — that let
            # github-oss render a dead deploy card). This is the true
            # no-dead-cards gate for the fetch-origin/curated/ClawHub paths.
            from app.services.federation_install import get_origin_fetcher

            if get_origin_fetcher(decoded_source) is not None:
                actionable = True
                primary_action = ACTION_DEPLOY if deployable else ACTION_PREVIEW
                installable = True

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
        action_label=action_label,
        actionable=actionable,
        installable=installable,
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
