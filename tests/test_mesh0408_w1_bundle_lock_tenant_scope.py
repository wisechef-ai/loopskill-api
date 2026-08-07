"""RED-proof: bundle-lock routes are not tenant-scoped (codex review of PR #202).

Found by the codex `gpt-5.6-sol` adversarial review, which concluded the W1 fix
was "complete across that list, incomplete across the real attack surface".

``app/bundle_lock_routes.py::_require_owner_or_master`` authorizes on a bare
``ctx.user_id == bundle.bundle_owner`` with no tenant predicate at all — the
exact short-circuit mesh_0408 W1 exists to close, on a route that appeared in
neither the orchestrator's briefed site list nor the worker's own sweep.

Because ONE operator owns every org they run, that comparison is true across
every tenant boundary the account spans.

The same-org case is the CONTROL: if it does not stay allowed, this file proves
nothing and the failure is in the harness, not the product.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import bundle_lock_routes as blr
from app.authz import crosses_tenant

# The two real production orgs, one operator owning both (mesh_0408 lock #28).
ORG_OLDER = uuid.UUID("ab130efd-3ff0-482a-835b-8b5793297219")  # WiseChef AI
ORG_NEWER = uuid.UUID("601130f9-6ef8-453d-a998-7819820af606")  # Astrovita
OPERATOR = uuid.uuid4()


def _ctx(org_id):
    return SimpleNamespace(scope="user", user_id=OPERATOR, org_id=org_id, is_org_owner=False)


def _bundle(org_id):
    return SimpleNamespace(org_id=org_id, bundle_owner=OPERATOR, id=uuid.uuid4())


def _allows(ctx, bundle) -> bool:
    try:
        blr._require_owner_or_master(ctx, bundle)
        return True
    except HTTPException:
        return False


class TestBundleLockTenantScope:
    def test_control_same_org_owner_is_allowed(self):
        """CONTROL. If this fails the cross-tenant assertion below is VOID."""
        assert _allows(_ctx(ORG_OLDER), _bundle(ORG_OLDER)), (
            "The owner must keep access to their own same-org bundle. "
            "A failure here means the harness is broken, not that isolation holds."
        )

    def test_the_two_principals_really_do_cross_a_tenant_boundary(self):
        """Trap V2: assert the precondition before trusting the probe."""
        ctx = _ctx(ORG_OLDER)
        foreign = _bundle(ORG_NEWER)
        assert ctx.user_id == foreign.bundle_owner, "same identity — the wedge"
        assert ctx.org_id != foreign.org_id, "different tenants"
        assert crosses_tenant(ctx, foreign), (
            "crosses_tenant must agree this is a tenant crossing, otherwise the "
            "test below is asserting something other than isolation."
        )

    def test_cross_org_bundle_lock_access_is_denied(self):
        """The finding. Owner-match must not grant across a tenant boundary."""
        assert not _allows(_ctx(ORG_OLDER), _bundle(ORG_NEWER)), (
            "bundle_lock_routes._require_owner_or_master granted access to a "
            "bundle in a DIFFERENT org purely on owner-match. One operator owns "
            "every org they run, so this is true across every client boundary."
        )

    def test_personal_scope_bundles_stay_reachable(self):
        """Back-compat: NULL org_id is personal scope and must not be denied."""
        assert _allows(_ctx(ORG_OLDER), _bundle(None))
        assert _allows(_ctx(None), _bundle(None))

    @pytest.mark.parametrize("bundle_org", [ORG_OLDER, ORG_NEWER, None])
    def test_master_scope_is_never_tenant_denied(self, bundle_org):
        master = SimpleNamespace(scope="master", user_id=None, org_id=None)
        assert _allows(master, _bundle(bundle_org))
