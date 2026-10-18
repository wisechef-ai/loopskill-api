"""qa1018 C18 — honest servability for the public bundle well-known surface.

Persona QA on prod (2026-10-18) found the public bundle index advertising
every member of ``seo-and-search-growth`` as installable while 4 of its 8
SKILL.md downloads were NOT skills: three were the ~750-byte locked stub the
SKILL.md route falls back to when a federated member's live origin fetch
fails, and one was an upstream "``<name>`` has moved" signpost. ``install.sh``
wrote all 8 to disk and printed "installed 8 skill(s)".

This module holds the small, pure-ish helpers the well-known routes use so
the index, the SKILL.md route and ``install.sh`` agree on ONE vocabulary:

``installable`` (bool, always present on an index entry) and, when False,
``skip_reason``:

- ``locked``      — tier-gated body (paid internal skill, or a federated
                    member whose license does not permit redistribution).
                    Deterministic from the DB row.
- ``unavailable`` — the body cannot be served right now: a free local skill
                    with an empty body, or a redistributable federated member
                    whose live origin fetch FAILED the last time it was
                    served (recorded below).
- ``moved``       — the origin serves an upstream "has moved" signpost
                    instead of a skill (recorded below).

What the server knows at INDEX time without fetching N origins (bulk surfaces
must never fan out to origin — see ``install_descriptor_for``): the tier /
redistribution gate, the local body, and the LAST OBSERVED serve outcome of a
federated member, which the SKILL.md route records on the member's
descriptor (``external_resources['public_serve']``) only when it changes. A
member never served yet carries no verdict, so the index is a HINT for
federated members; the SKILL.md response headers are the truth at fetch time
(``X-LoopSkill-Installable`` / ``X-LoopSkill-Skip-Reason`` /
``X-LoopSkill-SHA256``), and ``install.sh`` re-checks them, which is also what
heals a recorded ``unavailable`` once the origin recovers.

``sha256`` on an index entry is emitted ONLY when it is exactly the hash of
the bytes the SKILL.md route will serve: a free local skill's stored body.
A federated body is fetched live and may change upstream between the two
requests, so its index entry carries no ``sha256`` (the response header still
carries the hash of the bytes actually sent, for truncation checks).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SKIP_LOCKED = "locked"
SKIP_UNAVAILABLE = "unavailable"
SKIP_MOVED = "moved"
SKIP_REASONS = (SKIP_LOCKED, SKIP_UNAVAILABLE, SKIP_MOVED)

#: Descriptor key the SKILL.md route writes the last observed serve outcome to.
SERVE_KEY = "public_serve"

#: Response headers ``install.sh`` reads to decide per member at fetch time.
HDR_INSTALLABLE = "X-LoopSkill-Installable"
HDR_SKIP_REASON = "X-LoopSkill-Skip-Reason"
HDR_SHA256 = "X-LoopSkill-SHA256"

# An upstream signpost is SHORT, has no skill frontmatter, and says so in its
# first heading ("# `on-page-seo-auditor` has moved") or calls itself a
# signpost repo. A real SKILL.md starts with ``---`` frontmatter, so requiring
# its absence keeps a long skill that merely mentions "has moved" safe.
_MOVED_MAX_BYTES = 4096
_MOVED_HEADING = re.compile(r"^\s*#{1,3}\s+.*\b(?:has|have)\s+(?:been\s+)?moved\b", re.IGNORECASE)
_SIGNPOST = re.compile(r"\bis now a \**signpost\** repo\b", re.IGNORECASE)


def body_sha256(text: str) -> str:
    """sha256 of the exact bytes a ``PlainTextResponse(text)`` sends (utf-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_moved_signpost(text: str) -> bool:
    """True iff ``text`` is an upstream "has moved" signpost, not a skill."""
    if not text or len(text.encode("utf-8")) > _MOVED_MAX_BYTES:
        return False
    stripped = text.lstrip()
    if stripped.startswith("---"):
        return False
    first_line = next((ln for ln in stripped.splitlines() if ln.strip()), "")
    return bool(_MOVED_HEADING.match(first_line) or _SIGNPOST.search(text))


def recorded_skip_reason(skill: Any) -> str | None:
    """The last observed serve outcome for a federated member (None = served OK / never served)."""
    desc = getattr(skill, "external_resources", None) or {}
    verdict = desc.get(SERVE_KEY) or {}
    reason = verdict.get("skip_reason") if isinstance(verdict, dict) else None
    return reason if reason in SKIP_REASONS else None


def record_serve_outcome(db: "Session", skill: Any, skip_reason: str | None) -> None:
    """Persist a federated member's serve outcome — only when it CHANGES.

    Same write-on-read precedent as ``rescan_pending_external``: bounded to a
    verdict flip, so steady-state reads never write. Never raises: a failed
    write must not turn a served SKILL.md into a 500.
    """
    if recorded_skip_reason(skill) == skip_reason:
        return
    try:
        desc = dict(getattr(skill, "external_resources", None) or {})
        if skip_reason is None:
            desc.pop(SERVE_KEY, None)
        else:
            desc[SERVE_KEY] = {
                "skip_reason": skip_reason,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        skill.external_resources = desc
        db.add(skill)
        db.commit()
    # Rationale: observability-only write on a public GET; the response body
    # (and its headers) already carry the truth for this request.
    except Exception:  # noqa: BLE001
        logger.warning("could not record serve outcome for %s", getattr(skill, "slug", "?"), exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def index_flags(skill: Any, *, servable_tier: bool, is_external: bool) -> dict[str, Any]:
    """The additive servability keys for one well-known index entry.

    ``servable_tier`` = the existing free / redistributable-external predicate.
    Legacy ``locked`` + ``tier`` keys are emitted by the caller unchanged.
    """
    if not servable_tier:
        return {"installable": False, "skip_reason": SKIP_LOCKED}
    if is_external:
        reason = recorded_skip_reason(skill)
        if reason is not None:
            return {"installable": False, "skip_reason": reason}
        return {"installable": True}
    body = getattr(skill, "readme", None) or ""
    if not body.strip():
        return {"installable": False, "skip_reason": SKIP_UNAVAILABLE}
    if is_moved_signpost(body):
        return {"installable": False, "skip_reason": SKIP_MOVED}
    return {"installable": True, "sha256": body_sha256(body)}


def serve_headers(text: str, skip_reason: str | None) -> dict[str, str]:
    """Response headers that tell an installer what this SKILL.md response IS."""
    headers = {
        HDR_INSTALLABLE: "false" if skip_reason else "true",
        HDR_SHA256: body_sha256(text),
    }
    if skip_reason:
        headers[HDR_SKIP_REASON] = skip_reason
    return headers
