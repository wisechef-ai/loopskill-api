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
