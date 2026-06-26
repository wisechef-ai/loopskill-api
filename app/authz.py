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
    # recipes_cookbook_attach promotes it into a bundle (the dogfood loop).  # compat-alias
    # Fails closed without ``db`` — callers in private-skill paths thread it.
    if ctx.scope == "user" and ctx.user_id is not None and db is not None:
        from app.models import Bundle, BundleSkill

        owns_via_cookbook = (
            db.query(BundleSkill)
            .join(Bundle, Bundle.id == BundleSkill.bundle_id)  # compat-alias
            .filter(
                BundleSkill.skill_id == skill.id,
                BundleSkill.source != "disabled",
                Bundle.bundle_owner == ctx.user_id,  # compat-alias
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
    MCP recipes_cookbook_install tool).

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


def can_write_cookbook(ctx: AuthContext, cookbook: Any) -> bool:
    """Return True if ctx may write (modify) the given cookbook.

    Access rules:
    - Master scope: always allowed
    - User scope: allowed if ctx.user_id == cookbook.bundle_owner  # compat-alias
    - Bundle-scoped key: additionally restricted to the specific cookbook
      (ctx.bundle_scope must match cookbook.id)  # compat-alias
    - All other cases: False
    """
    # If this is a bundle-scoped key restricted to a different bundle → deny
    if ctx.bundle_scope is not None and ctx.bundle_scope != cookbook.id:  # compat-alias
        return False

    if ctx.scope == "master":
        return True
    if (
        ctx.scope == "user"
        and ctx.user_id is not None
        and ctx.user_id == cookbook.bundle_owner  # compat-alias
    ):
        return True
    return False


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
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == fleet.owner_user_id:
        return True
    if ctx.scope == "fleet" and ctx.fleet_id is not None and ctx.fleet_id == fleet.id:
        return True
    return False
