"""spotify_0608 Ph E — install-provenance service (Sentry/npm pattern).

ONE seam for the whole provenance contract so no transport drifts:

  mint_provenance(db, install_event)            → provenance_id (random token)
  record_install_with_provenance(...)           → (InstallEvent, provenance_id)
       the canonical "stamp an install + return its provenance" call every
       transport uses (direct / cookbook single+bulk / MCP / external / public
       external). Records the InstallEvent + bumps the denormalised counter with
       the same is_test integrity rule as _record_install_event (Ph B §4.2),
       inlined here so it can also stamp cookbook_id + attribution + mint
       provenance in the same transaction.
  resolve_provenance(db, provenance_id)         → ResolvedProvenance | None
       server-side join provenance_id → install_event → (cookbook_id, skill_id,
       skill_slug, version_semver, attribution). NO client-readable metadata is
       ever embedded in the token (the token is random).
  route_targets_for_provenance(db, provenance_id)
       → list[FeedbackTarget] — the deterministic feedback-routing decision:
       the skill-author repo AND/OR the cookbook-curator repo, REPLACING
       feedback.py's "first cookbook the user owns" guess.

Design invariants (R2/R3/R4):
  - provenance_id = secrets.token_urlsafe(32): RANDOM, server-stored, opaque.
  - Deep-link / non-fetch installs are recorded as InstallEvent rows stamped
    ``attribution='unattributed'`` and STILL get a provenance_id (no hard-fail).
    Transient FETCH_ORIGIN failures are a DIFFERENT class — they never reach
    here (the caller raises before calling us).
  - Bulk envelopes carry provenance_id PER-SKILL, not cookbook-top-level.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.models import (
    Bundle,
    InstallEvent,
    ProvenanceRecord,
    Skill,
)

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ATTR_ATTRIBUTED = "attributed"
ATTR_UNATTRIBUTED = "unattributed"

# Default repo for feedback when no creator/bundle routing resolves.
DEFAULT_FEEDBACK_REPO = "wisechef-ai/recipes-api"


@dataclass(frozen=True)
class ResolvedProvenance:
    """Server-side resolution of a provenance_id."""

    provenance_id: str
    install_event_id: UUID
    skill_id: UUID | None
    skill_slug: str | None
    bundle_id: UUID | None
    version_semver: str | None
    attribution: str


@dataclass(frozen=True)
class FeedbackTarget:
    """A repo a feedback/skill-error report should be routed to.

    kind: 'curator' (cookbook owner's configured repo) | 'author' (skill
          creator's repo) | 'default' (the platform fallback).
    mode: 'pat' | 'github_app' | None (None = default dispatch_event path).
    """

    kind: str
    repo: str
    mode: str | None = None
    pat_enc: str | None = None


def mint_provenance(db: "Session", install_event: InstallEvent) -> str:
    """Mint a random provenance_id mapped to an install_event. Returns the id.

    Does NOT commit — composes into the caller's transaction with the
    InstallEvent insert (so provenance + event land atomically).
    """
    provenance_id = secrets.token_urlsafe(32)
    db.add(
        ProvenanceRecord(
            provenance_id=provenance_id,
            install_event_id=install_event.id,
        )
    )
    return provenance_id


def record_install_with_provenance(
    db: "Session",
    *,
    skill: Skill,
    version_semver: str,
    request=None,
    source: str = "cookbook",
    cookbook_id: UUID | str | None = None,
    attribution: str = ATTR_ATTRIBUTED,
    commit: bool = False,
) -> tuple[InstallEvent, str]:
    """Record an InstallEvent + mint its provenance_id in one call.

    The canonical entry point every install transport uses so the provenance
    contract can never drift. Records an InstallEvent with the same denormalised
    counter + is_test integrity rule as ``_record_install_event`` (Ph B §4.2),
    inlined so it can stamp cookbook_id + attribution on the SAME row and mint
    provenance atomically.

    Args:
        cookbook_id: the cookbook the install came from (None for direct).
        attribution: 'attributed' (default) | 'unattributed' (honest deep-link).
        commit: when True, commit before returning (single-skill paths). Bulk
            callers pass False and commit once after the loop.

    Returns (install_event, provenance_id). The event is flushed so its id is
    available for the ProvenanceRecord FK.
    """
    from app.models import APIKey

    api_key_id = None
    client_ip = None
    if request is not None:
        api_key_id = getattr(request.state, "api_key_id", None)
        try:
            from app.config import settings
            from app.utils.client_ip import _real_client_ip

            client_ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
        # Rationale: client_ip is observability-only; never fail an install on
        # an IP parse error (mirrors _record_install_event / Issue #22).
        except Exception:  # noqa: BLE001
            client_ip = None

    cb_uuid: UUID | None = None
    if cookbook_id is not None:
        cb_uuid = cookbook_id if isinstance(cookbook_id, UUID) else UUID(str(cookbook_id))

    event = InstallEvent(
        skill_id=skill.id,
        skill_slug=skill.slug,
        api_key_id=api_key_id,
        version_semver=version_semver,
        client_ip=client_ip,
        bundle_id=cb_uuid,
        attribution=attribution,
    )
    db.add(event)

    # Ph B §4.2 integrity: bump the denormalised public counter ONLY for organic
    # installs. A test/CI key (is_test) records the event but does NOT inflate
    # the public counter. Anonymous installs (no key) are organic and DO bump.
    is_test = False
    if api_key_id is not None:
        is_test = bool(db.query(APIKey.is_test).filter(APIKey.id == api_key_id).scalar())
    if not is_test:
        db.query(Skill).filter(Skill.id == skill.id).update(
            {Skill.install_count: Skill.install_count + 1},
            synchronize_session=False,
        )

    db.flush()  # need event.id before minting provenance
    provenance_id = mint_provenance(db, event)
    if commit:
        db.commit()
    return event, provenance_id


def resolve_provenance(db: "Session", provenance_id: str) -> ResolvedProvenance | None:
    """Resolve a provenance_id to its install artifact. None if unknown.

    Pure server-side join — the token itself carries no metadata.
    """
    if not provenance_id or not isinstance(provenance_id, str):
        return None
    rec = db.query(ProvenanceRecord).filter(ProvenanceRecord.provenance_id == provenance_id).first()
    if rec is None:
        return None
    ev = db.query(InstallEvent).filter(InstallEvent.id == rec.install_event_id).first()
    if ev is None:
        return None
    return ResolvedProvenance(
        provenance_id=provenance_id,
        install_event_id=ev.id,
        skill_id=ev.skill_id,
        skill_slug=ev.skill_slug,
        bundle_id=ev.bundle_id,
        version_semver=ev.version_semver,
        attribution=ev.attribution or ATTR_ATTRIBUTED,
    )


def _curator_target(db: "Session", cookbook_id: UUID | None) -> FeedbackTarget | None:
    """The cookbook curator's configured feedback repo, if any."""
    if cookbook_id is None:
        return None
    cb = db.query(Bundle).filter(Bundle.id == cookbook_id).first()
    if cb is None or not cb.feedback_repo:
        return None
    return FeedbackTarget(
        kind="curator",
        repo=cb.feedback_repo,
        mode=cb.feedback_mode,
        pat_enc=cb.feedback_pat_enc,
    )


def route_targets_for_provenance(db: "Session", provenance_id: str | None) -> list[FeedbackTarget]:
    """Deterministic feedback-routing for a provenance_id.

    REPLACES feedback.py's ``_resolve_feedback_target`` "first cookbook the user
    owns with a repo set" guess. Resolves the provenance to the ACTUAL cookbook
    used and routes to the cookbook-curator's configured repo. Returns an empty
    list when nothing custom resolves (caller falls back to the default repo).

    Routing target = the cookbook-curator repo bound to the cookbook the install
    actually came from. (The skill-author repo path keys on the same
    Cookbook.feedback_repo mechanism — a skill author who curates a cookbook
    configures routing there; we do not invent a separate Skill.repo column that
    does not exist in the schema.)
    """
    if not provenance_id:
        return []
    resolved = resolve_provenance(db, provenance_id)
    if resolved is None:
        return []
    targets: list[FeedbackTarget] = []
    curator = _curator_target(db, resolved.bundle_id)
    if curator is not None:
        targets.append(curator)
    return targets
