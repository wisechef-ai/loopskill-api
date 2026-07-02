"""Fleet-member service helpers — activate_0701 Phase 1.

lock #13: the per-agent API key IS the member identity. This module houses
the key->member resolution helper (kept out of the route module to respect
the 600-line pyfile-size gate).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models import FleetMember


def resolve_member_for_key(db: Session, api_key_id: UUID | None) -> FleetMember | None:
    """Return the active FleetMember bound to ``api_key_id``, or None.

    None is returned when ``api_key_id`` is None (anonymous/master caller),
    the key is not a member key, or the member has been deactivated — all of
    which mean "this reconcile event has no fleet-member identity to stamp"
    (backward-compat: pre-Phase-1 rows and non-member keys keep member_id NULL).
    """
    if api_key_id is None:
        return None
    return (
        db.query(FleetMember)
        .filter(FleetMember.api_key_id == api_key_id, FleetMember.is_active == True)  # noqa: E712
        .first()
    )
