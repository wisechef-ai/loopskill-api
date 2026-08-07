"""Authorization predicates — pure functions for centralized access control.

All predicates are pure functions: they take an AuthContext and a model object,
and return True/False without side effects (with the targeted exception of
can_read_skill/can_install, which accept an optional Session for the
cookbook-scope clause described below — see Rationale).

100% line coverage required (see test_secfix_1905_a_authz.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.auth_ctx import AuthContext


def _is_master(ctx: Any) -> bool:
    """True for a master-scope caller, for either context shape.

    ``AuthContext`` carries ``scope="master"``; ``bundle_routes.CookbookCtx``
    carries ``is_master=True``. The tenant predicates below are called with
    both, so they duck-type the master signal in one place.
    """
    return getattr(ctx, "scope", None) == "master" or bool(getattr(ctx, "is_master", False))


def crosses_tenant(ctx: Any, obj: Any) -> bool:
    """True when ``obj`` belongs to a tenant (org) that ``ctx`` is NOT inside.

    mesh_0408 W1 (P0). LoopSkill's whole wedge is "one account, N isolated
    client orgs" — so an owner-match is NOT sufficient authorization. A single
    account owns every org it runs, which means ``obj.bundle_owner == ctx.user_id``
    is true across every tenant boundary that account spans. This predicate is
    the missing half.

    The rule, exhaustively:

    ==================  ==================  ==========================
    ``obj.org_id``      ``ctx.org_id``      result
    ==================  ==================  ==========================
    NULL                anything            False — obj is not tenant-scoped
    non-NULL            NULL                True  — personal-scope caller may
                                                    not reach an org artifact
    non-NULL            equal               False — same tenant
    non-NULL            different           True  — CROSS-TENANT, deny
    ==================  ==================  ==========================

    NULL ``obj.org_id`` is personal scope and stays reachable by its owner
    regardless of which tenant the caller's key currently resolves to: those
    artifacts predate tenancy and belong to the account, not to a client org.
    Master scope is never considered cross-tenant.
    """
    if _is_master(ctx):
        return False
    obj_org = getattr(obj, "org_id", None)
    if obj_org is None:
        return False
    ctx_org = getattr(ctx, "org_id", None)
    return ctx_org is None or ctx_org != obj_org


def owner_match_within_tenant(ctx: Any, obj: Any) -> bool:
    """True when ctx owns ``obj`` AND does not cross a tenant boundary to reach it.

    mesh_0408 W1 (P0) — the shared replacement for the bare
    ``obj.bundle_owner == ctx.user_id`` test that every bundle-authorization
    site used to spell inline. That bare test short-circuits BEFORE any org
    constraint, so for an account running two client orgs it granted full
    cross-tenant read/write on bundles, connectors, reconcile diffs and
    promotion reports.

    Routing every site through one predicate is deliberate: nine inline copies
    is how the tenth site ships next month without the org clause.

    Master scope keeps its existing unconditional bypass.

    ``ctx`` may be an :class:`~app.auth_ctx.AuthContext` or a
    ``bundle_routes.CookbookCtx`` — only ``user_id`` / ``org_id`` and the
    master signal are read. ``obj`` is any row exposing ``bundle_owner`` and
    ``org_id`` (i.e. :class:`~app.models.Bundle`).

    See :func:`owner_match_within_tenant_clause` for the SQL-filter twin used
    by the list/query sites, which MUST stay semantically identical.
    """
    if _is_master(ctx):
        return True
    user_id = getattr(ctx, "user_id", None)
    owner = getattr(obj, "bundle_owner", None)
    if user_id is None or owner is None or user_id != owner:
        return False
    return not crosses_tenant(ctx, obj)


def owner_match_within_tenant_clause(ctx: Any, model: Any) -> Any:
    """SQL twin of :func:`owner_match_within_tenant`, for query filters.

    mesh_0408 W1 (P0). The list/query authorization sites cannot call the
    row-level predicate — they need a WHERE clause. Keeping the two in one
    module (and one docstring pair) is what stops them drifting apart, which
    is exactly how a "fixed" detail route ends up beside a still-leaking list
    route.

    Emits, for ``ctx.org_id`` non-NULL::

        bundle_owner = :uid AND (org_id IS NULL OR org_id = :org)

    and, for a personal-scope caller (``ctx.org_id`` IS NULL)::

        bundle_owner = :uid AND org_id IS NULL

    A caller with no ``user_id`` matches nothing (``false()``) rather than
    degrading to ``bundle_owner IS NULL``, which would hand every ownerless
    row to an unauthenticated caller.
    """
    from sqlalchemy import and_, false, or_

    user_id = getattr(ctx, "user_id", None)
    if user_id is None:
        return false()

    owner_col = model.bundle_owner
    org_col = model.org_id
    ctx_org = getattr(ctx, "org_id", None)
    if ctx_org is None:
        return and_(owner_col == user_id, org_col.is_(None))
    return and_(owner_col == user_id, or_(org_col.is_(None), org_col == ctx_org))


def can_read_skill(ctx: AuthContext, skill: Any, db: "Session | None" = None) -> bool:
    """Return True if ctx may read/view the given skill.

    Access rules:
    - Public skills: always readable
    - Master scope: always readable
    - User scope: readable if the user owns the skill
    - Bundle-scope (cbt_token) tokens: readable IF the skill belongs to the
      cookbook the token is scoped to. Requires ``db`` so we can look up the
      cookbook→skill association. When ``db`` is None for a cbt_token caller
      we fail closed (return False) — callers in private-skill paths MUST
      thread the Session through. The public-skill clause runs first so
      public skills do not require ``db``.
    - All other cases: False

    Rationale (cookbook-scope DB lookup): a cbt_token's scope is "this token
    can access exactly the skills in cookbook X." That set is not derivable
    from the signed token alone (it grows/shrinks as the cookbook owner
    adds/removes skills), so authz must consult the CookbookSkill join. No
    signal-only path exists — by design.
    """
    if skill.is_public:
        return True
    if ctx.scope == "master":
        return True
    skill_owner = getattr(skill, "skill_owner", None)
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == skill_owner:
        return True
    # loopclose_3005 Phase C — user-scope bundle-ownership clause.
    # A user may read/install a private skill that lives in a bundle they
    # own. This is symmetric with the cbt_token clause below (delegated access
    # to a bundle's skills) and matches the canonical REST bundle-install
    # path, which authorizes via bundle ownership rather than skill ownership.
    # Required so an agent can install its OWN tailored fork after
    # loopskill_bundle_attach promotes it into a bundle (the dogfood loop).  # compat-alias
    # Fails closed without ``db`` — callers in private-skill paths thread it.
    if ctx.scope == "user" and ctx.user_id is not None and db is not None:
        from app.models import Bundle, BundleSkill

        owns_via_cookbook = (
            db.query(BundleSkill)
            .join(Bundle, Bundle.id == BundleSkill.bundle_id)  # compat-alias
            .filter(
                BundleSkill.skill_id == skill.id,
                BundleSkill.source != "disabled",
                # mesh_0408 W1: owner-match alone spans every org the account
                # runs — a private skill in one client's bundle was readable
                # from another client's key. Tenant-scoped now.
                owner_match_within_tenant_clause(ctx, Bundle),
            )
            .first()
            is not None
        )
        if owns_via_cookbook:
            return True
    # cookbook_share_2105 Phase C — cbt_token scope clause.
    if ctx.scope == "cbt_token" and ctx.bundle_scope is not None and db is not None:
        # Local import: app.models imports authz indirectly via app.database in
        # some test fixtures; deferring keeps the import graph acyclic.
        from app.models import BundleSkill

        exists = (
            db.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == ctx.bundle_scope,  # compat-alias
                BundleSkill.skill_id == skill.id,
                BundleSkill.source != "disabled",
            )
            .first()
            is not None
        )
        return exists
    return False


def can_install(ctx: AuthContext, skill: Any, db: "Session | None" = None) -> bool:
    """Return True if ctx may install the given skill.

    Same rules as can_read_skill — install requires at least read access.
    ``db`` is forwarded to can_read_skill so cbt_token callers can resolve
    cookbook-scope skill membership.
    """
    return can_read_skill(ctx, skill, db=db)


def tier_rank_allows_install(caller_tier: str | None, skill_tier: str | None) -> bool:
    """Return True if a caller of ``caller_tier`` may install a skill of ``skill_tier``.

    portal_0610 R1 (P0 paywall-bypass closure, §6.6 / §6.7-L10).

    The visibility predicates above only distinguish public/private — they do
    NOT stop a FREE authenticated key from pulling a PRO skill's tarball. This
    pure rank comparison is the missing tier gate, applied on EVERY install
    surface (direct /api/skills/install, both cookbook install routes, and the
    MCP loopskill_bundle_install tool).

    Rank source is the single canonical ``ranking.TIER_RANK`` (free=1, pro=2,
    pro_plus=3, plus the 30-day legacy aliases). A ``None`` or unknown tier on
    either side floors to free (rank 1): an anonymous / lapsed / unresolved
    caller may only reach free skills, and a skill with no tier is free.

    Master callers resolve to ``pro_plus`` upstream (rank 3 ≥ everything), so
    they need no special case here. This keeps the predicate pure and testable
    in isolation (no Session, no request).
    """
    from app.ranking import TIER_RANK

    caller_rank = TIER_RANK.get((caller_tier or "free").lower(), 1)
    skill_rank = TIER_RANK.get((skill_tier or "free").lower(), 1)
    return caller_rank >= skill_rank


def can_read_personality(ctx: AuthContext, personality: Any, db: "Session | None" = None) -> bool:
    """Return True if ctx may read/like the given personality.

    spotify_2607 Phase B. Mirrors can_read_skill's shape at the scale
    personalities actually need: public personalities are always readable;
    master scope always readable; the creator (via Creator.user_id) may
    read/like their own private personality. There is no bundle-ownership
    clause here (unlike can_read_skill) because personalities are not gated
    by BundleSkill/authz.can_install — that join only exists for skills.
    """
    if getattr(personality, "is_public", True):
        return True
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and db is not None:
        from app.models import Creator

        creator_id = getattr(personality, "creator_id", None)
        if creator_id is not None:
            owns = (
                db.query(Creator).filter(Creator.id == creator_id, Creator.user_id == ctx.user_id).first()
                is not None
            )
            if owns:
                return True
    return False


def can_read_verifier(ctx: AuthContext, verifier: Any, db: "Session | None" = None) -> bool:
    """Return True if ctx may read/pull the given verifier (aka Loop).

    Same shape as can_read_personality/can_read_composite_loop — Verifier
    (``app.models.Loop`` is a compat-alias for ``app.models.Verifier``) also
    carries a nullable creator_id -> Creator.user_id ownership chain.
    """
    if getattr(verifier, "is_public", True):
        return True
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and db is not None:
        from app.models import Creator

        creator_id = getattr(verifier, "creator_id", None)
        if creator_id is not None:
            owns = (
                db.query(Creator).filter(Creator.id == creator_id, Creator.user_id == ctx.user_id).first()
                is not None
            )
            if owns:
                return True
    return False


def can_read_composite_loop(ctx: AuthContext, loop: Any, db: "Session | None" = None) -> bool:
    """Return True if ctx may read/like the given composite loop.

    spotify_2607 Phase B. Same shape as can_read_personality — CompositeLoop
    also carries a nullable creator_id -> Creator.user_id ownership chain.
    """
    if getattr(loop, "is_public", True):
        return True
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and db is not None:
        from app.models import Creator

        creator_id = getattr(loop, "creator_id", None)
        if creator_id is not None:
            owns = (
                db.query(Creator).filter(Creator.id == creator_id, Creator.user_id == ctx.user_id).first()
                is not None
            )
            if owns:
                return True
    return False


def can_write_cookbook(ctx: AuthContext, cookbook: Any) -> bool:
    """Return True if ctx may write (modify) the given cookbook.

    Access rules:
    - Master scope: always allowed
    - User scope: allowed if ctx owns the cookbook WITHIN its tenant
      (mesh_0408 W1 — see owner_match_within_tenant)
    - Bundle-scoped key: additionally restricted to the specific cookbook
      (ctx.bundle_scope must match cookbook.id)  # compat-alias
    - All other cases: False
    """
    # If this is a bundle-scoped key restricted to a different bundle → deny
    if ctx.bundle_scope is not None and ctx.bundle_scope != cookbook.id:  # compat-alias
        return False

    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and owner_match_within_tenant(ctx, cookbook):
        return True
    return False


def can_read_cookbook(ctx: AuthContext, cookbook: Any, *, allow_org_read: bool = False) -> bool:
    """Return True if ctx may read the given cookbook (bundle).

    Mirrors app.bundle_routes._resolve_owned_cookbook's ownership rules so
    REST and MCP agree on who may read a given bundle:
    - Bundle-scoped key/token: only the one bundle it is scoped to
    - Master scope: always allowed
    - User scope: allowed if ctx owns the cookbook WITHIN its tenant
      (mesh_0408 W1 — see owner_match_within_tenant)
    - allow_org_read=True and ctx.org_id matches cookbook.org_id: allowed
      (mirrors the REST detail route's ``allow_org_read=True`` clause)
    - All other cases: False
    """
    if ctx.bundle_scope is not None and ctx.bundle_scope != cookbook.id:  # compat-alias
        return False

    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and owner_match_within_tenant(ctx, cookbook):
        return True
    if (
        allow_org_read
        and ctx.org_id is not None
        and getattr(cookbook, "org_id", None) is not None
        and ctx.org_id == cookbook.org_id
    ):
        return True
    return False


def can_reconcile_cookbook(ctx: AuthContext, cookbook: Any, db: "Session | None" = None) -> bool:
    """Return whether a caller may read a bundle's desired state for deploy.

    Following is deliberately narrower than ownership: a follower of a public
    bundle may obtain its reconciliation plan, but may never use an apply path
    that writes the bundle. Callers must still enforce read-only operation for
    followers before invoking a mutating reconcile mode.
    """
    if can_write_cookbook(ctx, cookbook):
        return True
    if (
        ctx.scope != "user"
        or ctx.user_id is None
        or db is None
        or getattr(cookbook, "visibility", None) != "public"
    ):
        return False
    from app.models import FollowedBundle

    return (
        db.query(FollowedBundle)
        .filter(
            FollowedBundle.user_id == ctx.user_id,
            FollowedBundle.bundle_id == cookbook.id,
        )
        .first()
        is not None
    )


def can_call_admin_mcp_tool(ctx: AuthContext) -> bool:
    """Return True if ctx may call an admin-level MCP tool.

    Only master-scope callers may use admin tools.
    """
    return ctx.scope == "master"


def can_run_sandbox(ctx: AuthContext) -> bool:
    """Return True if ctx may execute sandbox runs.

    Access rules:
    - Master scope: always allowed
    - is_sandbox_operator flag: allowed regardless of scope
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.is_sandbox_operator:
        return True
    return False


def can_use_fleet(ctx: AuthContext, fleet: Any) -> bool:
    """Return True if ctx may operate on the given fleet.

    Access rules:
    - Master scope: always allowed
    - User scope: allowed if ctx.user_id == fleet.owner_user_id
    - Fleet scope: allowed if ctx.fleet_id == fleet.id (key matches this fleet)
    - activate_0701/TEN: org-scoped access — same-org members can access
      org fleets even if not the direct owner.
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == fleet.owner_user_id:
        return True
    # activate_0701/TEN: org-scoped fleet access — a member of the same org
    # as the fleet can operate on it.
    fleet_org = getattr(fleet, "org_id", None)
    if ctx.scope == "user" and ctx.org_id is not None and fleet_org is not None and ctx.org_id == fleet_org:
        return True
    if ctx.scope == "fleet" and ctx.fleet_id is not None and ctx.fleet_id == fleet.id:
        return True
    return False


def can_publish_connector(ctx: AuthContext) -> bool:
    """Return True if ctx may publish a connector (activate_0701 Phase B).

    Publishing is a creator action tied to a user identity — anonymous,
    cbt_token, bdl_token, and fleet scopes may NOT publish.
    """
    return ctx.scope in ("user", "master")


def can_write_fleet(ctx: AuthContext, fleet: Any) -> bool:
    """Return True if ctx may write (create/modify/delete) the given fleet.

    activate_0701/TEN: org-scoped fleet write authorization.

    Access rules:
    - Master scope: always allowed
    - User scope + fleet owner: allowed if ctx.user_id == fleet.owner_user_id
    - User scope + same org: allowed if fleet.org_id == ctx.org_id (both non-NULL)
    - NULL org_id = personal scope (backward compat): only the direct owner
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == fleet.owner_user_id:
        return True
    fleet_org = getattr(fleet, "org_id", None)
    # Org boundary: same org → allowed. NULL=personal scope, org check skipped.
    if ctx.scope == "user" and ctx.org_id is not None and fleet_org is not None and ctx.org_id == fleet_org:
        return True
    return False


def can_access_bundle(ctx: AuthContext, bundle: Any) -> bool:
    """Return True if ctx may access (subscribe/read) the given bundle.

    activate_0701/TEN: org-scoped bundle access authorization.

    Access rules:
    - Master scope: always allowed
    - User scope + bundle owner: allowed if ctx owns the bundle WITHIN its
      tenant (mesh_0408 W1 — the bare owner-match this replaced short-circuited
      BEFORE the org clause below, so it granted access across every org the
      owning account runs, making that clause unreachable for its own owner)
    - User scope + same org: allowed if bundle.org_id == ctx.org_id (both non-NULL)
    - NULL org_id = personal scope (backward compat): only the direct owner
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and owner_match_within_tenant(ctx, bundle):
        return True
    bundle_org = getattr(bundle, "org_id", None)
    # Org boundary: same org → allowed. NULL=personal scope, org check skipped.
    if ctx.scope == "user" and ctx.org_id is not None and bundle_org is not None and ctx.org_id == bundle_org:
        return True
    return False


def can_manage_fleet(ctx: AuthContext, fleet: Any) -> bool:
    """Return True if ctx holds the FLEET-MANAGER capability over ``fleet``.

    fleetos_1607 Phase A / §0 #9. The manager surface (placement writes —
    assign / evacuate / force_move — and the fleet-state read) is a KEY
    CAPABILITY, not an agent identity. It is granted to:

    - Master scope: always.
    - The fleet OWNER (user scope, ctx.user_id == fleet.owner_user_id).
    - An org member of the fleet's org (user scope, same non-NULL org_id).
    - An OPERATOR-scope key (the appointed fleet manager — Tori is the first
      holder; any client can mint an operator key over THEIR fleet).

    Critically it is NOT granted to a bare fleet-MEMBER key. A ``fleet``-scope
    key identifies an enrolled agent (one member); such a key can report its own
    runs and read its own drift, but MUST NOT drive placements for the whole
    fleet. This is the authz boundary the concurrency suite RED-proofs
    (member-key → manager tool = 403).
    """
    if ctx.scope == "master":
        return True
    # Operator-scope key = the appointed manager capability. It must still be
    # bound to this fleet's owner/org to prevent a cross-tenant operator key
    # from managing an unrelated fleet.
    if ctx.scope == "operator":
        owner = getattr(fleet, "owner_user_id", None)
        fleet_org = getattr(fleet, "org_id", None)
        if ctx.user_id is not None and owner is not None and ctx.user_id == owner:
            return True
        if ctx.org_id is not None and fleet_org is not None and ctx.org_id == fleet_org:
            return True
        # A master-appointed operator with neither binding is not auto-trusted
        # over an arbitrary fleet — fall through to False.
        return False
    if (
        ctx.scope == "user"
        and ctx.user_id is not None
        and ctx.user_id == getattr(fleet, "owner_user_id", None)
    ):
        return True
    fleet_org = getattr(fleet, "org_id", None)
    if ctx.scope == "user" and ctx.org_id is not None and fleet_org is not None and ctx.org_id == fleet_org:
        return True
    # Everything else — including a bare fleet-member (scope="fleet") key — is
    # denied the manager capability.
    return False
