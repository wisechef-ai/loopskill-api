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
    - Cookbook-scope (cbt_token) tokens: readable IF the skill belongs to the
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
    # cookbook_share_2105 Phase C — cbt_token scope clause.
    if ctx.scope == "cbt_token" and ctx.cookbook_scope is not None and db is not None:
        # Local import: app.models imports authz indirectly via app.database in
        # some test fixtures; deferring keeps the import graph acyclic.
        from app.models import CookbookSkill

        exists = (
            db.query(CookbookSkill)
            .filter(
                CookbookSkill.cookbook_id == ctx.cookbook_scope,
                CookbookSkill.skill_id == skill.id,
                CookbookSkill.source != "disabled",
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


def can_write_cookbook(ctx: AuthContext, cookbook: Any) -> bool:
    """Return True if ctx may write (modify) the given cookbook.

    Access rules:
    - Master scope: always allowed
    - User scope: allowed if ctx.user_id == cookbook.cookbook_owner
    - Cookbook-scoped key: additionally restricted to the specific cookbook
      (ctx.cookbook_scope must match cookbook.id)
    - All other cases: False
    """
    # If this is a cookbook-scoped key restricted to a different cookbook → deny
    if ctx.cookbook_scope is not None and ctx.cookbook_scope != cookbook.id:
        return False

    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == cookbook.cookbook_owner:
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
