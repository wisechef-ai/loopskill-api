"""mesh_0408 T0-D — domain exceptions for the mesh credential path."""

from __future__ import annotations


class MeshTenantUnassignedError(Exception):
    """Spec §2.4 — Fleet.org_id IS NULL. Minting fails closed, HTTP 409.

    Never silently mint `org: null` — the nullable-org_id hole (null == null
    comparing true at every verifier) is the cross-tenant bypass this
    exception exists to prevent.
    """

    def __init__(self, fleet_id: str):
        self.fleet_id = fleet_id
        super().__init__(f"fleet {fleet_id} has no assigned org (org_id IS NULL); mesh mint refused")


class MeshMintRaceError(Exception):
    """Spec §4.9 — the mint transaction observed a moved/revoked member mid-read."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"mesh mint aborted: {reason}")


class MeshKeyRingError(Exception):
    """The mesh Ed25519 signing key ring is missing, unreadable, or misconfigured."""


class MeshVerifyError(Exception):
    """mesh_0408 T3-A — a mesh credential failed admission at a LoopSkill

    control-plane endpoint (bad signature, wrong aud/class, expired, revoked
    member, unassigned org, etc). Deliberately a single exception type with a
    human-readable reason string — the caller maps it to one HTTP status
    (401), matching spec §3.3's "no degraded mode": every verification
    failure rejects, there is no partial-success branch to distinguish.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
