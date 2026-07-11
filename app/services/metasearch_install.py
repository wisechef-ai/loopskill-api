"""Metasearch install resolution (metasearch_0710 P1).

P0 shipped the unified search surface (one ranked list, funnel events). P1 makes
every card's install actually resolve — turning a search result into an
installable SKILL.md body via the EXISTING per-source origin resolvers
(``federation_install.get_origin_fetcher``), plus fail-closed semantics so a card
that can't resolve is dropped rather than shown as a dead button (plan §5.3).

Two resolution modes, matching the two persistence models (§6):

1. **Installable body (fetch-origin)** — skills.sh (id→github-raw), github taps
   (Contents→raw CDN), well-known, hermes-hub, browse-sh, lobehub. Streamed from
   origin at install time, zero rehost. This is the ad-hoc/direct-install path
   AND the body P3 will pin at deploy time.

2. **ClawHub preview-only (no rehost, decision #6)** — ClawHub is DEEP_LINK: its
   origin resolver is deliberately absent so we NEVER rehost supply-chain-unvetted
   content. BUT ClawHub's own API returns the full SKILL.md inline in the
   ``description`` field (verified 2026-07-10). So for PREVIEW we surface that
   API-provided body via a clearly-marked deep-link path — the user reads it,
   installs from ClawHub's origin themselves. It is NOT deployable (P0 already
   set deployable=False) and its body is never stored by us.

``install_ref`` is the opaque token minted by ``metasearch.unify_external`` /
``unify_curated`` — shape ``"{source}:{slug}"``. This module decodes it and
routes to the right resolver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.federation_install import get_origin_fetcher

logger = logging.getLogger(__name__)

# Max SKILL.md body we'll surface for preview (defensive — an origin could return
# a huge blob). Matches the reconcile fetch tarball spirit (bounded).
_MAX_PREVIEW_BYTES = 256 * 1024


@dataclass
class ResolvedInstall:
    """The result of resolving a metasearch install_ref.

    ``resolved`` False + ``body`` None = fail-closed (drop the card). ``rehosted``
    marks whether the body came from origin (installable) or from ClawHub's own
    API (preview-only, deep-link — never stored)."""

    resolved: bool
    source: str
    slug: str
    body: str | None = None
    origin_url: str | None = None
    preview_only: bool = False  # True for ClawHub (deep-link, no rehost)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "source": self.source,
            "slug": self.slug,
            "body": self.body,
            "origin_url": self.origin_url,
            "preview_only": self.preview_only,
            "reason": self.reason,
        }


def _decode_ref(install_ref: str) -> tuple[str, str] | None:
    """Decode ``"{source}:{slug}"`` → (source, slug). The slug itself may contain
    no colon after the FIRST split (source ids never contain ':'). Returns None
    on a malformed ref (fail-closed)."""
    if not install_ref or ":" not in install_ref:
        return None
    source, _, slug = install_ref.partition(":")
    source = source.strip()
    slug = slug.strip()
    if not source or not slug:
        return None
    return source, slug


def _clip(body: str) -> str:
    if len(body.encode("utf-8", "ignore")) <= _MAX_PREVIEW_BYTES:
        return body
    # Clip on a char boundary under the byte cap.
    return body.encode("utf-8", "ignore")[:_MAX_PREVIEW_BYTES].decode("utf-8", "ignore")


def resolve_clawhub_preview(slug: str) -> ResolvedInstall:
    """Preview-only body for a ClawHub skill — from ClawHub's OWN API, NOT rehosted.

    decision #6: we never fetch-origin ClawHub. But its /api/v1/skills/{slug}
    returns the SKILL.md inline in ``description``, so a user can preview it before
    installing from ClawHub themselves. The body is surfaced, never stored, and
    the card stays non-deployable.
    """
    from app.services import federation_live as fl

    real_slug = (slug or "").replace("--", "/").strip("/")
    if not real_slug:
        return ResolvedInstall(False, "clawhub", slug, reason="empty_slug")
    detail_url = f"https://clawhub.ai/api/v1/skills/{real_slug}"
    data = fl._safe_json_get(detail_url)
    # ClawHub wraps the skill under a "skill" key (verified 2026-07-10).
    skill = data.get("skill") if isinstance(data, dict) else None
    if not isinstance(skill, dict):
        # Some responses return the skill at the top level.
        skill = data if isinstance(data, dict) else None
    body = None
    if isinstance(skill, dict):
        body = skill.get("description") or skill.get("readme") or skill.get("summary")
    if not isinstance(body, str) or not body.strip():
        return ResolvedInstall(
            False,
            "clawhub",
            slug,
            reason="no_inline_body",
            origin_url=f"https://clawhub.ai/skills/{real_slug}",
        )
    return ResolvedInstall(
        resolved=True,
        source="clawhub",
        slug=slug,
        body=_clip(body),
        origin_url=f"https://clawhub.ai/skills/{real_slug}",
        preview_only=True,
        reason="clawhub_inline_preview",
    )


def resolve_install(install_ref: str) -> ResolvedInstall:
    """Resolve a metasearch install_ref to an installable/previewable SKILL.md body.

    Fail-closed: a malformed ref, a source with no resolver, or an origin outage
    returns ``resolved=False`` so the caller drops the card (§5.3 — no dead cards).

    - curated (``recipes:*``) is resolved by the internal catalog, NOT here (the
      route already has the curated body); we return resolved=True with no body so
      the caller keeps the curated card and serves its own body.
    - ClawHub → preview-only path (its own API body, never rehosted).
    - everything else → the per-source origin fetcher (fetch-origin install).
    """
    decoded = _decode_ref(install_ref)
    if decoded is None:
        return ResolvedInstall(False, "", "", reason="malformed_ref")
    source, slug = decoded

    # Curated skills are internal — the route owns their body; nothing to resolve.
    if source == "recipes":
        return ResolvedInstall(True, source, slug, reason="curated_internal")

    # ClawHub: preview from its own API, never fetch-origin (decision #6).
    if source == "clawhub":
        return resolve_clawhub_preview(slug)

    fetcher = get_origin_fetcher(source)
    if fetcher is None:
        # No installable resolver for this source (e.g. github-oss without a
        # token, or a quarantined source) → fail-closed.
        return ResolvedInstall(False, source, slug, reason="no_origin_fetcher")

    try:
        result = fetcher(slug)
    # Rationale: an origin outage / bad payload must fail-closed, never 500.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch install resolve failed for %s:%s", source, slug, exc_info=True)
        return ResolvedInstall(False, source, slug, reason="resolve_error")

    if not result:
        return ResolvedInstall(False, source, slug, reason="unresolvable")
    origin_url, body = result
    if not isinstance(body, str) or not body.strip():
        return ResolvedInstall(False, source, slug, origin_url=origin_url, reason="empty_body")
    return ResolvedInstall(
        resolved=True,
        source=source,
        slug=slug,
        body=_clip(body),
        origin_url=origin_url,
        preview_only=False,
        reason="fetch_origin",
    )
