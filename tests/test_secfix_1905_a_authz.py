"""secfix_1905 Phase A — app/authz.py predicate tests.

100% line coverage required on all 5 predicates.

Tests cover:
  - can_read_skill / can_install
  - can_write_cookbook (including cross-tenant and cookbook-scoped key)
  - can_call_admin_mcp_tool
  - can_run_sandbox
"""
import pytest
from uuid import uuid4
from unittest.mock import MagicMock

from app.auth_ctx import AuthContext
from app.authz import (
    can_read_skill,
    can_install,
    can_write_cookbook,
    can_call_admin_mcp_tool,
    can_run_sandbox,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_skill(is_public: bool = True, skill_owner=None):
    """Create a mock skill object."""
    skill = MagicMock()
    skill.is_public = is_public
    skill.skill_owner = skill_owner
    return skill


def make_cookbook(cookbook_owner=None, cookbook_id=None):
    """Create a mock cookbook object."""
    cb = MagicMock()
    cb.cookbook_owner = cookbook_owner
    cb.id = cookbook_id or uuid4()
    return cb


# ── can_read_skill ────────────────────────────────────────────────────────────

def test_can_read_public_skill_anonymous():
    """Public skills are readable by anyone including anonymous."""
    ctx = AuthContext.anonymous()
    skill = make_skill(is_public=True)
    assert can_read_skill(ctx, skill) is True


def test_can_read_public_skill_user():
    """Public skills are readable by any user."""
    ctx = AuthContext(scope="user", user_id=uuid4())
    skill = make_skill(is_public=True)
    assert can_read_skill(ctx, skill) is True


def test_can_read_private_skill_master():
    """Master scope can read any skill including private."""
    ctx = AuthContext(scope="master")
    skill = make_skill(is_public=False)
    assert can_read_skill(ctx, skill) is True


def test_can_read_private_skill_owner():
    """A user can read their own private skill."""
    uid = uuid4()
    ctx = AuthContext(scope="user", user_id=uid)
    skill = make_skill(is_public=False, skill_owner=uid)
    assert can_read_skill(ctx, skill) is True


def test_cannot_read_private_skill_other_user():
    """A user cannot read another user's private skill."""
    ctx = AuthContext(scope="user", user_id=uuid4())
    skill = make_skill(is_public=False, skill_owner=uuid4())
    assert can_read_skill(ctx, skill) is False


def test_cannot_read_private_skill_anonymous():
    """Anonymous callers cannot read private skills."""
    ctx = AuthContext.anonymous()
    skill = make_skill(is_public=False)
    assert can_read_skill(ctx, skill) is False


# ── can_install ───────────────────────────────────────────────────────────────

def test_can_install_mirrors_can_read():
    """can_install has the same rules as can_read_skill."""
    uid = uuid4()
    ctx = AuthContext(scope="user", user_id=uid)
    public_skill = make_skill(is_public=True)
    private_owned = make_skill(is_public=False, skill_owner=uid)
    private_other = make_skill(is_public=False, skill_owner=uuid4())

    assert can_install(ctx, public_skill) is True
    assert can_install(ctx, private_owned) is True
    assert can_install(ctx, private_other) is False


def test_can_install_master_all():
    """Master can install anything."""
    ctx = AuthContext(scope="master")
    skill = make_skill(is_public=False)
    assert can_install(ctx, skill) is True


# ── can_write_cookbook ────────────────────────────────────────────────────────

def test_can_write_cookbook_master():
    """Master can write any cookbook."""
    ctx = AuthContext(scope="master")
    cb = make_cookbook(cookbook_owner=uuid4())
    assert can_write_cookbook(ctx, cb) is True


def test_can_write_cookbook_owner():
    """User can write their own cookbook."""
    uid = uuid4()
    ctx = AuthContext(scope="user", user_id=uid)
    cb = make_cookbook(cookbook_owner=uid)
    assert can_write_cookbook(ctx, cb) is True


def test_cannot_write_cookbook_cross_tenant():
    """User cannot write another user's cookbook (cross-tenant deny)."""
    ctx = AuthContext(scope="user", user_id=uuid4())
    cb = make_cookbook(cookbook_owner=uuid4())
    assert can_write_cookbook(ctx, cb) is False


def test_cookbook_scoped_key_correct_cookbook():
    """Cookbook-scoped key on the correct cookbook → allowed."""
    uid = uuid4()
    cb_id = uuid4()
    ctx = AuthContext(scope="user", user_id=uid, cookbook_scope=cb_id)
    cb = make_cookbook(cookbook_owner=uid, cookbook_id=cb_id)
    assert can_write_cookbook(ctx, cb) is True


def test_cookbook_scoped_key_wrong_cookbook():
    """Cookbook-scoped key on a DIFFERENT cookbook → denied (even if owner matches)."""
    uid = uuid4()
    ctx = AuthContext(scope="user", user_id=uid, cookbook_scope=uuid4())
    cb = make_cookbook(cookbook_owner=uid, cookbook_id=uuid4())
    assert can_write_cookbook(ctx, cb) is False


def test_cookbook_scoped_master_key_different_cookbook():
    """Cookbook-scoped master key on a different cookbook → denied."""
    ctx = AuthContext(scope="master", cookbook_scope=uuid4())
    cb = make_cookbook(cookbook_id=uuid4())
    assert can_write_cookbook(ctx, cb) is False


def test_cannot_write_cookbook_anonymous():
    """Anonymous callers cannot write cookbooks."""
    ctx = AuthContext.anonymous()
    cb = make_cookbook(cookbook_owner=uuid4())
    assert can_write_cookbook(ctx, cb) is False


# ── can_call_admin_mcp_tool ───────────────────────────────────────────────────

def test_can_call_admin_mcp_tool_master_only():
    """Only master scope may call admin MCP tools."""
    assert can_call_admin_mcp_tool(AuthContext(scope="master")) is True
    assert can_call_admin_mcp_tool(AuthContext(scope="user", user_id=uuid4())) is False
    assert can_call_admin_mcp_tool(AuthContext.anonymous()) is False
    assert can_call_admin_mcp_tool(AuthContext(scope="operator")) is False
    assert can_call_admin_mcp_tool(AuthContext(scope="cbt_token")) is False


# ── can_run_sandbox ───────────────────────────────────────────────────────────

def test_can_run_sandbox_master():
    """Master scope can always run sandbox."""
    ctx = AuthContext(scope="master")
    assert can_run_sandbox(ctx) is True


def test_can_run_sandbox_operator_flag():
    """is_sandbox_operator=True allows sandbox regardless of scope."""
    ctx = AuthContext(scope="user", user_id=uuid4(), is_sandbox_operator=True)
    assert can_run_sandbox(ctx) is True


def test_cannot_run_sandbox_regular_user():
    """Regular user without is_sandbox_operator cannot run sandbox."""
    ctx = AuthContext(scope="user", user_id=uuid4(), is_sandbox_operator=False)
    assert can_run_sandbox(ctx) is False


def test_cannot_run_sandbox_anonymous():
    """Anonymous callers cannot run sandbox."""
    ctx = AuthContext.anonymous()
    assert can_run_sandbox(ctx) is False


def test_master_override_in_can_run_sandbox():
    """Master with is_sandbox_operator=False still allowed (master overrides)."""
    ctx = AuthContext(scope="master", is_sandbox_operator=False)
    assert can_run_sandbox(ctx) is True


# ── Import test ───────────────────────────────────────────────────────────────

def test_imports_cleanly():
    """Module imports cleanly from app.authz."""
    from app.authz import can_write_cookbook, can_run_sandbox  # noqa: F401
    assert can_write_cookbook is not None
    assert can_run_sandbox is not None
