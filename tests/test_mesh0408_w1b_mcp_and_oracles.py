"""mesh_0408 W1b — the attack surface the first W1 pass did not cover.

A codex ``gpt-5.6-sol`` adversarial review of PR #202 found the W1 fix
"complete across that list, incomplete across the real attack surface". This
module is the behavioural half of the closure. It covers four things the
REST-shaped suite in ``test_mesh0408_p0_crossorg_isolation.py`` cannot see:

1. **MCP is the universal path** (lock #31). ``app/mcp/auth.py`` re-derived the
   caller's org from ``_resolve_org_membership`` — the OLDEST OrgMembership —
   discarding the layer-2 fleet resolution on every MCP call. The whole P0 was
   still exploitable over MCP while REST was clean.
2. **Unrequested response decoration.** ``call_tool_sync`` appends a
   ``bundle_status`` block to EVERY tool result, selected on a bare
   ``bundle_owner == user_id``. No authorization site was involved and no id
   had to be guessed: client A's bundle names and skill slugs simply arrived in
   the response to whatever tool client B's agent called.
3. **Existence oracles.** A 403/400/"you do not own this" that an absent id
   answers differently confirms to an unauthorized caller that the id is real.
4. **Layer 2 failing OPEN.** A DEACTIVATED fleet member fell through to the
   oldest-org fallback instead of failing closed.

TRAP V2 COMPLIANCE is inherited: the ``tenants`` fixture builds ONE account
running TWO client orgs, so both principals share a ``user_id`` on purpose and
differ only in resolved tenant. ``test_mcp_v2_precondition`` re-asserts that on
the MCP resolution path specifically — the REST module's precondition proves
nothing about ``validate_key``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

# The production-shaped fixtures live with the REST suite; re-using them is
# deliberate, so a drift in what "two tenants" means cannot make these two
# modules disagree about the thing they are both testing.
from tests.test_mesh0408_p0_crossorg_isolation import (  # noqa: F401  (pytest fixtures)
    _mk_bundle,
    _mk_fleet,
    _mk_key,
    _mk_org,
    _mk_org_membership,
    _mk_user,
    tenant_app,
    tenants,
)


def _mcp_caller(db, key: str) -> dict:
    """The caller dict the real SSE/HTTP transports build (app/mcp/auth.py)."""
    from app.mcp.auth import validate_key

    return validate_key(key, db)


def _order_bundles_a_then_b(db, tenants) -> None:
    """Give the two bundles DISTINCT creation times: A oldest, B newest.

    Required by every "implicit target" test below, and not cosmetic. The
    fixture inserts both bundles inside the same second, and
    ``Bundle.created_at`` has second resolution — so ``order_by(created_at)``
    was a TIE broken by insertion order, and the pre-fix "pick the account's
    oldest/newest bundle" query happened to return the SAME row as the fixed
    tenant-scoped one. The RED-proof harness caught
    ``test_list_bundle_implicit_target_stays_in_tenant`` passing with the guard
    reverted for exactly this reason (trap V1: green is not a gate).

    With A strictly older than B:
      * an ``asc`` (oldest-first) picker returns A  -> a key in tenant B that
        gets A has crossed the boundary;
      * a ``desc`` (newest-first) picker returns B -> a key in tenant A that
        gets B has crossed the boundary.
    Both directions now discriminate whatever the insertion order.
    """
    from datetime import datetime

    from app.models import Bundle

    for bundle, when in (
        (tenants.bundle_a, datetime(2026, 1, 1, 0, 0, 0)),
        (tenants.bundle_b, datetime(2026, 6, 1, 0, 0, 0)),
    ):
        db.query(Bundle).filter(Bundle.id == bundle.id).update(
            {"created_at": when}, synchronize_session=False
        )
    db.commit()
    db.refresh(tenants.bundle_a)
    db.refresh(tenants.bundle_b)
    assert tenants.bundle_a.created_at < tenants.bundle_b.created_at, (
        "the two bundles still share a creation time — every implicit-target "
        "assertion below would be a coin flip, not a test"
    )


# ══════════════════════════════════════════════════════════════════════════
# FINDING 2 — the MCP path must resolve the tenant like the REST path
# ══════════════════════════════════════════════════════════════════════════


def test_mcp_v2_precondition(tenants, db_session):
    """Trap V2, restated for MCP: same identity, different resolved tenant.

    Every assertion below this one is void if the two MCP callers do not share
    a ``user_id`` (that shared identity IS the defect) or do share an
    ``org_id`` (then they would be denied for the wrong reason).
    """
    a = _mcp_caller(db_session, tenants.key_a)["auth_ctx"]
    b = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]

    print(f"MCP principal A: user_id={a.user_id} org_id={a.org_id}")
    print(f"MCP principal B: user_id={b.user_id} org_id={b.org_id}")

    assert a.scope == "user" and b.scope == "user"
    assert a.user_id == b.user_id == tenants.user.id, (
        "the two MCP principals stopped sharing a user_id — the fixture no "
        "longer reproduces the defect and every probe below is vacuous"
    )
    assert a.org_id != b.org_id, "both member keys resolved to the SAME org over MCP — this module is VOID"


def test_mcp_member_key_resolves_its_fleets_org_not_the_oldest(tenants, db_session):
    """FINDING 2, the root: validate_key must not re-derive from membership.

    org_a is the account's OLDEST membership. A key enrolled into client B's
    fleet must resolve to org_b over MCP exactly as it does over REST; falling
    back to the oldest membership is the layer-2 defect, reintroduced.
    """
    assert _mcp_caller(db_session, tenants.key_a)["auth_ctx"].org_id == tenants.org_a.id
    assert _mcp_caller(db_session, tenants.key_b)["auth_ctx"].org_id == tenants.org_b.id, (
        "the MCP auth path resolved client B's member key to the account's "
        "OLDEST org — the layer-2 fix is REST-only, and lock #31 says MCP is "
        "the universal path"
    )


def test_mcp_org_id_agrees_with_the_rest_path(tenant_app, tenants, db_session):
    """REST and MCP must answer the same question the same way.

    Pinned as its own test because the failure mode is silent: two resolvers
    that agree today and diverge on the next change produce a surface where
    the same key is two different tenants.
    """
    for key in (tenants.key_a, tenants.key_b):
        rest = tenant_app.get("/_probe/whoami", headers={"x-api-key": key}).json()["org_id"]
        mcp = str(_mcp_caller(db_session, key)["auth_ctx"].org_id)
        assert rest == mcp, f"REST resolved {rest}, MCP resolved {mcp} for the same key"


class TestMcpBundleStatusDecoration:
    """FINDING 2 — "automatic MCP response decoration leaks cross-org data"."""

    @staticmethod
    def _make_outdated(db, bundle, *, slug):
        """A skill in ``bundle`` pinned BELOW its latest version.

        ``get_bundle_status`` only reports bundles that have something to
        update, so without this the decoration is absent for a benign reason
        and the test would pass while leaking.
        """
        from app.models import BundleSkill, Skill, SkillVersion

        skill = Skill(id=uuid.uuid4(), slug=slug, title=slug, category="ops", is_public=False)
        db.add(skill)
        db.flush()
        db.add(SkillVersion(id=uuid.uuid4(), skill_id=skill.id, semver="1.0.0"))
        db.add(SkillVersion(id=uuid.uuid4(), skill_id=skill.id, semver="2.0.0"))
        db.add(
            BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added", pinned_version="1.0.0")
        )
        db.flush()
        return skill

    def test_status_block_never_names_the_other_tenant(self, tenants, db_session):
        from app.mcp.server import call_tool_sync

        skill_a = self._make_outdated(db_session, tenants.bundle_a, slug="w1b-outdated-a")
        skill_b = self._make_outdated(db_session, tenants.bundle_b, slug="w1b-outdated-b")
        db_session.commit()

        for key, mine, theirs, my_skill, their_skill in (
            (tenants.key_a, tenants.bundle_a, tenants.bundle_b, skill_a, skill_b),
            (tenants.key_b, tenants.bundle_b, tenants.bundle_a, skill_b, skill_a),
        ):
            out = call_tool_sync(
                "loopskill_search",
                {"query": "anything"},
                caller=_mcp_caller(db_session, key),
                db=db_session,
            )
            status = out.get("bundle_status")
            assert status is not None, (
                "CONTROL FAILED: the caller's OWN tenant has an outdated skill "
                "but no status block was produced — this test is void, not green"
            )
            ids = {cb["id"] for cb in status["your_cookbooks"]}
            names = {cb["name"] for cb in status["your_cookbooks"]}
            slugs = {s["slug"] for cb in status["your_cookbooks"] for s in cb["outdated_skills"]}

            assert str(mine.id) in ids, "CONTROL FAILED: own bundle missing from status — void, not green"
            assert my_skill.slug in slugs, "CONTROL FAILED: own outdated skill missing — void, not green"
            assert str(theirs.id) not in ids, (
                f"MCP response decoration leaked the OTHER tenant's bundle id: {ids}"
            )
            assert theirs.name not in names, (
                f"MCP response decoration leaked the OTHER tenant's bundle NAME: {names}"
            )
            assert their_skill.slug not in slugs, (
                f"MCP response decoration leaked the OTHER tenant's private skill slug: {slugs}"
            )

    def test_status_fails_closed_without_a_resolved_tenant(self, tenants, db_session):
        """A caller whose tenant cannot be resolved sees only personal-scope rows.

        ``get_bundle_status`` takes ``org_id`` keyword-only and defaults it to
        None; that default must mean "personal scope", not "every org this
        account runs".
        """
        from app.mcp.bundle_status import get_bundle_status

        self._make_outdated(db_session, tenants.bundle_a, slug="w1b-failclosed-a")
        db_session.commit()

        assert get_bundle_status(db_session, tenants.user.id) is None, (
            "an unresolved tenant was shown org-scoped bundles — the default "
            "must fail CLOSED to personal scope"
        )


class TestMcpToolsAreTenantScoped:
    """FINDING 2 — every MCP tool that resolves a bundle, not just the two named."""

    def test_list_bundle_explicit_id_is_denied_cross_tenant(self, tenants, db_session):
        from app.mcp.server import call_tool_sync

        out = call_tool_sync(
            "loopskill_list_bundle",
            {"cookbook_id": str(tenants.bundle_a.id)},
            caller=_mcp_caller(db_session, tenants.key_b),
            db=db_session,
        )
        assert out.get("error") == "cookbook_not_found", f"cross-tenant MCP bundle read: {out!r}"

    def test_list_bundle_implicit_target_stays_in_tenant(self, tenants, db_session):
        """The no-argument call had no authz site at all — it just picked one.

        ``loopskill_list_bundle()`` with no ``cookbook_id`` returned the
        account's most recently created bundle across ALL its orgs, with the
        owner, the name and the full skill list including pinned versions.

        This picker is ``created_at DESC``, so B is what an unscoped query
        returns and key_a is the principal that must not receive it.
        """
        from app.mcp.server import call_tool_sync

        _order_bundles_a_then_b(db_session, tenants)

        out = call_tool_sync(
            "loopskill_list_bundle",
            {},
            caller=_mcp_caller(db_session, tenants.key_a),
            db=db_session,
        )
        cb = out.get("cookbook")
        assert cb is not None, "CONTROL FAILED: the caller's own tenant has a bundle — void, not green"
        assert cb["id"] != str(tenants.bundle_b.id), (
            f"the implicit MCP bundle target resolved to the OTHER tenant: {cb!r}"
        )
        assert cb["id"] == str(tenants.bundle_a.id)

    @pytest.mark.parametrize(
        "key_attr,bundle_attr,denied",
        [
            ("key_a", "bundle_a", False),  # CONTROL
            ("key_b", "bundle_b", False),  # CONTROL
            ("key_a", "bundle_b", True),  # LEAK direction 1
            ("key_b", "bundle_a", True),  # LEAK direction 2 (the layer-2 half)
        ],
    )
    def test_bundle_install_is_denied_cross_tenant(self, tenants, db_session, key_attr, bundle_attr, denied):
        """The tool that PULLS a bundle's skill payloads onto an agent.

        Both directions are asserted separately: with the tenant PREDICATE
        alone, key_b still resolves to the account's oldest org and reaches
        bundle_a by accident, so direction 2 is the half a predicate-only fix
        would miss.
        """
        from app.mcp.server import call_tool_sync

        out = call_tool_sync(
            "loopskill_bundle_install",
            {"cookbook_id": str(getattr(tenants, bundle_attr).id)},
            caller=_mcp_caller(db_session, getattr(tenants, key_attr)),
            db=db_session,
        )
        if denied:
            assert out.get("code") == "cookbook_not_found", (
                f"cross-tenant MCP bundle install {key_attr} -> {bundle_attr}: {out!r}"
            )
        else:
            assert out.get("code") != "cookbook_not_found", (
                f"CONTROL FAILED ({key_attr} -> {bundle_attr}): the owning tenant "
                f"can no longer install its own bundle — void, not green: {out!r}"
            )

    def test_configure_feedback_implicit_target_stays_in_tenant(self, tenants, db_session):
        """The cross-tenant WRITE with no id to guess.

        ``loopskill_configure_feedback(repo=...)`` with no ``cookbook_id``
        resolved "the account's oldest non-base bundle" — i.e. its FIRST
        client's. Issued from an agent at client B it repointed client A's
        feedback stream at client B's GitHub repo.
        """
        from unittest.mock import patch

        from app.mcp.tools.configure_feedback import loopskill_configure_feedback

        # This picker is ``created_at ASC``, so A is what an unscoped query
        # returns and key_b is the principal that must not reach it.
        _order_bundles_a_then_b(db_session, tenants)

        # AuthContext is frozen; the tier gate needs Pro to reach the
        # bundle-resolution step at all.
        ctx_b = replace(_mcp_caller(db_session, tenants.key_b)["auth_ctx"], tier="pro")

        with patch("app.mcp.tools.configure_feedback.verify_repo_access", return_value=True):
            with patch("app.feedback_cred_vault.encrypt_pat", return_value="enc"):
                loopskill_configure_feedback(
                    db_session, repo="client-b/their-repo", mode="pat", pat="ghp_x", ctx=ctx_b
                )

        db_session.refresh(tenants.bundle_a)
        db_session.refresh(tenants.bundle_b)
        assert tenants.bundle_a.feedback_repo is None, (
            "an agent at client B repointed client A's feedback routing — the "
            "implicit target crossed the tenant boundary"
        )
        assert tenants.bundle_b.feedback_repo == "client-b/their-repo", (
            "CONTROL FAILED: the call did not configure the caller's OWN bundle "
            "either, so the assertion above would pass vacuously — void, not green"
        )

    def test_configure_feedback_explicit_id_is_denied_cross_tenant(self, tenants, db_session):
        from app.mcp.tools.configure_feedback import loopskill_configure_feedback

        ctx_b = replace(_mcp_caller(db_session, tenants.key_b)["auth_ctx"], tier="pro")

        out = loopskill_configure_feedback(
            db_session,
            repo="client-b/their-repo",
            mode="pat",
            pat="ghp_x",
            cookbook_id=str(tenants.bundle_a.id),
            ctx=ctx_b,
        )
        assert out["ok"] is False
        # ...and indistinguishable from an id that never existed (FINDING 3).
        absent = loopskill_configure_feedback(
            db_session,
            repo="client-b/their-repo",
            mode="pat",
            pat="ghp_x",
            cookbook_id=str(uuid.uuid4()),
            ctx=ctx_b,
        )
        assert out == absent, f"existence oracle: {out!r} vs {absent!r}"
        db_session.refresh(tenants.bundle_a)
        assert tenants.bundle_a.feedback_repo is None

    def test_skillify_implicit_target_stays_in_tenant(self, tenants, db_session):
        """The MCP twin of the REST recipify fix (same bug, unfixed surface)."""
        from app.mcp.server import call_tool_sync

        _order_bundles_a_then_b(db_session, tenants)

        content = (
            "---\nname: w1b-mcp-probe\n"
            "description: A probe skill authored from one tenant at another tenant.\n"
            "---\nProbe body.\n"
        )
        out = call_tool_sync(
            "loopskill_skillify",
            {"slug": "w1b-mcp-probe", "content": content},
            caller=_mcp_caller(db_session, tenants.key_b),
            db=db_session,
        )
        landed = out.get("cookbook_id")
        assert landed is not None, (
            f"CONTROL FAILED: skillify did not author at all, so the assertion "
            f"below would pass vacuously — void, not green: {out!r}"
        )
        assert landed != str(tenants.bundle_a.id), (
            f"a draft skillified from client B landed in client A's bundle: {out!r}"
        )
        assert landed == str(tenants.bundle_b.id), (
            f"it should have landed in client B's own bundle; got {landed}"
        )


# ══════════════════════════════════════════════════════════════════════════
# FINDING 1 + 3 — bundle-lock routes, end to end over HTTP
# ══════════════════════════════════════════════════════════════════════════


class TestBundleLockRoutesOverHttp:
    """The unit test in test_mesh0408_w1_bundle_lock_tenant_scope.py pins the
    predicate; this pins the ROUTES, which is what an attacker actually calls.
    """

    @pytest.mark.parametrize(
        "key_attr,bundle_attr,expect",
        [
            ("key_a", "bundle_a", 200),  # CONTROL
            ("key_b", "bundle_b", 200),  # CONTROL
            ("key_a", "bundle_b", 404),  # LEAK direction 1
            ("key_b", "bundle_a", 404),  # LEAK direction 2
        ],
    )
    def test_mint_lock(self, tenant_app, tenants, key_attr, bundle_attr, expect):
        """POST /lock FREEZES a bundle — a cross-tenant write on a client's deploy."""
        r = tenant_app.post(
            f"/api/bundles/{getattr(tenants, bundle_attr).id}/lock",
            headers={"x-api-key": getattr(tenants, key_attr)},
        )
        assert r.status_code == expect, f"{key_attr} -> {bundle_attr}: {r.status_code} {r.text}"

    def test_lock_history_is_tenant_scoped(self, tenant_app, tenants):
        """GET /lock/history returns every skill+version+checksum ever locked."""
        minted = tenant_app.post(
            f"/api/bundles/{tenants.bundle_a.id}/lock", headers={"x-api-key": tenants.key_a}
        )
        assert minted.status_code == 200, f"CONTROL FAILED (mint): {minted.text}"

        r = tenant_app.get(
            f"/api/bundles/{tenants.bundle_a.id}/lock/history", headers={"x-api-key": tenants.key_b}
        )
        assert r.status_code == 404, f"cross-tenant lock history read: {r.status_code} {r.text}"

        r = tenant_app.get(
            f"/api/bundles/{tenants.bundle_a.id}/lock/history", headers={"x-api-key": tenants.key_a}
        )
        assert r.status_code == 200, f"CONTROL FAILED: {r.text} — void, not green"

    def test_drift_is_tenant_scoped(self, tenant_app, tenants):
        """The lock MUST be minted first, or this test cannot fail.

        Caught by the RED-proof harness (finding 5): without a minted lock the
        route 404s with ``no_lock_minted`` for everyone, so asserting the bare
        status code passed identically with the tenant guard reverted. The
        detail is what discriminates.
        """
        minted = tenant_app.post(
            f"/api/bundles/{tenants.bundle_a.id}/lock", headers={"x-api-key": tenants.key_a}
        )
        assert minted.status_code == 200, f"CONTROL FAILED (mint): {minted.text}"

        r = tenant_app.post(
            f"/api/bundles/{tenants.bundle_a.id}/drift",
            headers={"x-api-key": tenants.key_b},
            json={"installed": []},
        )
        assert r.status_code == 404, f"cross-tenant drift computation: {r.status_code} {r.text}"
        assert r.json()["detail"] == "bundle_not_found", (
            "the cross-tenant caller got past the authorization gate and was "
            f"denied for an unrelated reason instead: {r.text}"
        )

        # CONTROL — the owning tenant still gets its drift verdict.
        own = tenant_app.post(
            f"/api/bundles/{tenants.bundle_a.id}/drift",
            headers={"x-api-key": tenants.key_a},
            json={"installed": []},
        )
        assert own.status_code == 200, f"CONTROL FAILED: {own.text} — void, not green"

    def test_no_existence_oracle(self, tenant_app, tenants):
        """FINDING 3: 403 not_bundle_owner told the caller the id was real."""
        # Mint first, so a caller that gets past the gate reads real lock
        # contents rather than an unrelated no_lock_minted 404.
        minted = tenant_app.post(
            f"/api/bundles/{tenants.bundle_a.id}/lock", headers={"x-api-key": tenants.key_a}
        )
        assert minted.status_code == 200, f"CONTROL FAILED (mint): {minted.text}"

        real = tenant_app.get(
            f"/api/bundles/{tenants.bundle_a.id}/lock", headers={"x-api-key": tenants.key_b}
        )
        absent = tenant_app.get(f"/api/bundles/{uuid.uuid4()}/lock", headers={"x-api-key": tenants.key_b})
        assert real.status_code == absent.status_code == 404
        assert real.json() == absent.json(), (
            f"the lock route distinguishes an existing bundle in another tenant "
            f"from an absent one: {real.json()!r} vs {absent.json()!r}"
        )


# ══════════════════════════════════════════════════════════════════════════
# FINDING 3 — the remaining existence oracles
# ══════════════════════════════════════════════════════════════════════════


def _oracle_probe(db, tenants, call):
    """Run ``call(bundle_id)`` for a cross-tenant bundle and for an absent id.

    Returns the two denial IDENTIFIERS. Payloads legitimately echo back the
    ``cookbook_id`` the caller supplied — that is the caller's own input, not
    new information — so only the error/code identifier is compared.
    """

    def _ident(out):
        return (out.get("error"), out.get("code"))

    return _ident(call(str(tenants.bundle_a.id))), _ident(call(str(uuid.uuid4())))


class TestMcpToolsHaveNoExistenceOracle:
    """FINDING 3, applied to the CLASS rather than the sites codex listed.

    Hard rule #7. Codex named the bundle-lock, follow, configure_feedback and
    engagement oracles; the same 'forbidden here, not-found there' pair was
    spelled out in five more MCP tools that resolve a bundle
    (``loopskill_sync``, ``share_*``, ``harvest``, ``bundle_handoff``, and the
    reconcile engine). Every one of them is reachable cross-tenant, because one
    account owns every client org it runs — 'unauthorized' on these surfaces is
    routinely a sibling tenant, not a stranger.

    ``fork_deploy`` already collapsed both cases to ``cookbook_not_found``; it
    is the shape the rest now match.
    """

    def test_loopskill_sync(self, tenants, db_session):
        from app.mcp.tools.loopskill_sync import loopskill_sync

        ctx = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]
        real, absent = _oracle_probe(
            db_session, tenants, lambda cid: loopskill_sync(db_session, cookbook_id=cid, ctx=ctx)
        )
        assert real == absent, f"loopskill_sync oracle: {real} vs {absent}"

    def test_share_revoke(self, tenants, db_session):
        from app.mcp.tools.share import loopskill_share_revoke

        ctx = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]
        real, absent = _oracle_probe(
            db_session,
            tenants,
            lambda cid: loopskill_share_revoke(
                db_session, cookbook_id=cid, token_id=str(uuid.uuid4()), ctx=ctx
            ),
        )
        assert real == absent, f"share_revoke oracle: {real} vs {absent}"

    def test_harvest(self, tenants, db_session):
        from app.mcp.tools import harvest as htool

        ctx = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]
        real, absent = _oracle_probe(
            db_session,
            tenants,
            lambda bid: htool.loopskill_harvest(db_session, bid, str(uuid.uuid4()), ctx=ctx),
        )
        assert real == absent, f"harvest oracle: {real} vs {absent}"

    def test_bundle_handoff(self, tenants, db_session):
        from app.mcp.tools.bundle_handoff import loopskill_bundle_handoff

        ctx = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]
        real, absent = _oracle_probe(
            db_session,
            tenants,
            lambda cid: loopskill_bundle_handoff(
                db_session, cookbook_id=cid, new_owner_email="nobody@example.com", ctx=ctx
            ),
        )
        assert real == absent, f"bundle_handoff oracle: {real} vs {absent}"

    def test_reconcile_engine(self, tenants, db_session):
        from app.services.reconcile import recipes_reconcile

        ctx = _mcp_caller(db_session, tenants.key_b)["auth_ctx"]
        real, absent = _oracle_probe(
            db_session,
            tenants,
            lambda cid: recipes_reconcile(db_session, cookbook_id=cid, local=[], ctx=ctx),
        )
        assert real == absent, f"reconcile oracle: {real} vs {absent}"

    def test_the_probe_is_not_vacuous(self, tenants, db_session):
        """CONTROL: the cross-tenant bundle really does exist, and is denied.

        Without this, every assertion above would pass if both probes were
        simply hitting the not-found path for the same benign reason.
        """
        from app.models import Bundle
        from app.mcp.tools.loopskill_sync import loopskill_sync

        assert db_session.query(Bundle).filter(Bundle.id == tenants.bundle_a.id).first() is not None

        owner_ctx = _mcp_caller(db_session, tenants.key_a)["auth_ctx"]
        allowed = loopskill_sync(db_session, cookbook_id=str(tenants.bundle_a.id), ctx=owner_ctx)
        assert allowed.get("error") != "not_found", (
            f"CONTROL FAILED: the owning tenant is denied its own bundle — void, not green: {allowed!r}"
        )


def test_follow_has_no_cross_tenant_existence_oracle(tenant_app, tenants):
    """400 cannot_follow_own_bundle (exists) vs 404 (absent) — the same shape
    already closed in artifact_like_routes, on the route it was copied from."""
    real = tenant_app.post(f"/api/bundles/{tenants.bundle_b.id}/follow", headers={"x-api-key": tenants.key_a})
    absent = tenant_app.post(f"/api/bundles/{uuid.uuid4()}/follow", headers={"x-api-key": tenants.key_a})
    assert real.status_code == 404, f"cross-tenant follow leaked existence: {real.status_code} {real.text}"
    assert real.json() == absent.json(), f"oracle: {real.json()!r} vs {absent.json()!r}"

    # CONTROL — inside its own tenant the self-follow guard still speaks plainly.
    own = tenant_app.post(f"/api/bundles/{tenants.bundle_a.id}/follow", headers={"x-api-key": tenants.key_a})
    assert own.status_code == 400 and own.json()["detail"] == "cannot_follow_own_bundle", (
        f"CONTROL FAILED: same-tenant self-follow no longer reports plainly — {own.text}"
    )


def test_unfollow_has_no_cross_tenant_existence_oracle(tenant_app, tenants):
    real = tenant_app.delete(
        f"/api/bundles/{tenants.bundle_b.id}/follow", headers={"x-api-key": tenants.key_a}
    )
    absent = tenant_app.delete(f"/api/bundles/{uuid.uuid4()}/follow", headers={"x-api-key": tenants.key_a})
    assert real.status_code == 404, f"{real.status_code} {real.text}"
    assert real.json() == absent.json()

    # CONTROL — unfollowing your own tenant's bundle is still idempotent-200.
    own = tenant_app.delete(
        f"/api/bundles/{tenants.bundle_a.id}/follow", headers={"x-api-key": tenants.key_a}
    )
    assert own.status_code == 200, f"CONTROL FAILED: {own.text} — void, not green"


# ══════════════════════════════════════════════════════════════════════════
# FINDING 4 — layer 2 must fail CLOSED, not fall back to the oldest org
# ══════════════════════════════════════════════════════════════════════════


def test_layer2_fails_closed_for_a_deactivated_member(tenant_app, tenants, db_session):
    """A DEACTIVATED member key must never inherit the account's oldest org.

    ``FleetMember.is_active`` was not consulted at all, so a member that had
    been stood down still authenticated as its fleet's tenant — and, had the
    row been filtered naively instead, would have fallen through to the
    oldest-membership lookup, which is the fail-OPEN this closes. Personal
    scope is the only safe answer for a key whose fleet binding is no longer
    live.
    """
    from app.models import APIKey, FleetMember

    # CONTROL — while active, key_b resolves to its own fleet's org.
    before = tenant_app.get("/_probe/whoami", headers={"x-api-key": tenants.key_b}).json()
    assert before["org_id"] == str(tenants.org_b.id), "CONTROL FAILED — void, not green"

    member = db_session.query(FleetMember).filter(FleetMember.fleet_id == tenants.fleet_b.id).one()
    member.is_active = False
    # The key itself stays ACTIVE: this is the state an out-of-band
    # deactivation leaves behind, and the one the resolver has to survive.
    db_session.query(APIKey).filter(APIKey.id == member.api_key_id).update({"is_active": True})
    db_session.commit()

    after = tenant_app.get("/_probe/whoami", headers={"x-api-key": tenants.key_b}).json()
    assert after["org_id"] == "None", (
        "a DEACTIVATED member key resolved to an org — it must fail closed to "
        f"personal scope, and above all must never inherit the account's oldest "
        f"org ({tenants.org_a.id}). Got {after['org_id']}"
    )
    assert after["org_id"] != str(tenants.org_a.id), "fell back to the OLDEST membership — fail-OPEN"


def test_deactivated_member_key_cannot_read_any_tenants_bundle(tenant_app, tenants, db_session):
    """The behavioural consequence: personal scope reaches no org bundle."""
    from app.models import FleetMember

    member = db_session.query(FleetMember).filter(FleetMember.fleet_id == tenants.fleet_b.id).one()
    member.is_active = False
    db_session.commit()

    for bundle in (tenants.bundle_a, tenants.bundle_b):
        r = tenant_app.get(f"/api/cookbooks/{bundle.id}", headers={"x-api-key": tenants.key_b})
        assert r.status_code == 404, f"deactivated member key read {bundle.name}: {r.status_code} {r.text}"
