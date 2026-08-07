"""Tenant (org) resolution for authenticated callers.

Extracted from ``app.middleware.api_key`` so that module stays under the 600-line
god-object cap (see tests/test_w0_2_pyfile_size_discipline.py) — same rationale
as ``_public_paths.py`` and ``_token_auth.py``. Both names remain importable
from ``app.middleware.api_key`` for the existing callers.

There are exactly TWO ways a caller acquires a tenant, and telling them apart is
the whole point of this module:

  * a HUMAN resolves it from their :class:`~app.models.OrgMembership` rows;
  * a FLEET-MEMBER KEY resolves it from the fleet the key is enrolled in.

Conflating the two was the mesh_0408 W1 P0 — see :func:`resolve_org_for_key`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def resolve_org_membership(db: Any, user_id: UUID) -> tuple[UUID | None, bool]:
    """Resolve (org_id, is_org_owner) from a user's org membership.

    activate_0701/TEN. Ties break on the OLDEST membership. That tie-break is
    only meaningful for a HUMAN caller, who is one person with one session; for
    a machine key it is arbitrary, which is why fleet-member keys must not
    reach this function (see :func:`resolve_org_for_key`).
    """
    from app.models import OrgMembership

    m = (
        db.query(OrgMembership)
        .filter(OrgMembership.user_id == user_id)
        .order_by(OrgMembership.created_at.asc())
        .first()
    )
    return (m.org_id, m.role == "owner") if m else (None, False)


def resolve_org_for_key(db: Any, api_key_id: UUID | None, user_id: UUID) -> tuple[UUID | None, bool]:
    """Resolve (org_id, is_org_owner) for an authenticated API key.

    mesh_0408 W1 (P0) — a fleet-MEMBER key resolves its tenant from the FLEET
    it belongs to, never from org membership.

    Why this exists: ``fleet_member_routes.enroll_member`` mints a member key
    with ``user_id = fleet.owner_user_id``, i.e. the identity of the ACCOUNT
    that runs the fleet, not a per-client identity. Falling through to
    :func:`resolve_org_membership` therefore resolved that account's OLDEST org
    membership for every one of its member keys — so an account running N
    client orgs had all N collapsed onto whichever org it happened to create
    first, and a key deployed at client B authenticated as tenant A. That is
    the leak, and no downstream org predicate can repair it: a wrongly resolved
    tenant compares equal to the wrong bundles.

    ``fleet_members.api_key_id`` is UNIQUE and NOT NULL, so one key identifies
    at most one member; a single join off that index is enough (this runs on
    every authenticated request — do not split it into two queries).

    Fails CLOSED: when the key IS a member key but its fleet carries no
    ``org_id``, this returns personal scope ``(None, False)`` rather than
    falling back to membership — falling back is precisely the bug.

    ``is_org_owner`` is always False for a member key: a deployed agent is not
    the org payer, and ``authz.can_manage_fleet`` keys off that flag.
    """
    from app.models import Fleet, FleetMember

    if api_key_id is not None:
        row = (
            db.query(Fleet.org_id)
            .join(FleetMember, FleetMember.fleet_id == Fleet.id)
            .filter(FleetMember.api_key_id == api_key_id)
            .first()
        )
        if row is not None:
            return (row[0], False)
    return resolve_org_membership(db, user_id)
