"""Tests for Phase D — recipes_share_* MCP tools (app/mcp/tools/share.py).

TDD-first: these tests are written BEFORE the implementation. They should
be RED until the implementation is complete.

Required (≥6):
  test_recipes_share_create_returns_cbt_format
  test_recipes_share_create_persists_hash_not_plaintext
  test_recipes_share_list_returns_only_active
  test_recipes_share_revoke_invalidates_immediately
  test_recipes_share_rotate_creates_new_invalidates_old
  test_config_blocks_parseable
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Generator
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Cookbook, CookbookShareToken, User


# ─────────────────────────── DB Fixtures ─────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ─────────────────────────── Helper factories ────────────────────────────────


def _make_user(db: Session) -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Share Tester",
        email=f"{uid}@test.example",
        subscription_tier="operator",
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_cookbook(db: Session, *, owner_id) -> Cookbook:
    cb = Cookbook(
        id=uuid4(),
        name="MCP Share Test CB",
        description="test",
        is_base=False,
        bundle_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _master_ctx() -> AuthContext:
    return AuthContext(scope="master")


def _user_ctx(user_id, cookbook_id=None) -> AuthContext:
    return AuthContext(
        scope="user",
        user_id=user_id,
        bundle_scope=UUID(str(cookbook_id)) if cookbook_id else None,
    )


# ─────────────────────────── Tests ───────────────────────────────────────────


class TestRecipesShareCreate:
    def test_recipes_share_create_returns_cbt_format(self, db_session):
        """Token matches ^cbt_[a-f0-9]{8}_[a-f0-9]{32}$."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            ctx=ctx,
        )
        assert "token" in result
        pattern = re.compile(r"^cbt_[a-f0-9]{8}_[a-f0-9]{32}$")
        assert pattern.match(result["token"]), (
            f"Token {result['token']!r} does not match expected format"
        )

    def test_recipes_share_create_persists_hash_not_plaintext(self, db_session):
        """DB row stores SHA-256 hash, not the plaintext token."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            name="hash-check",
            ctx=ctx,
        )
        plaintext = result["token"]
        expected_hash = hashlib.sha256(plaintext.encode()).hexdigest()

        # Fetch the row directly from DB
        row = (
            db_session.query(CookbookShareToken)
            .filter(CookbookShareToken.id == UUID(result["id"]))
            .first()
        )
        assert row is not None
        assert row.token_hash == expected_hash, "DB stores hash, not plaintext"
        assert row.token_hash != plaintext, "plaintext must NOT be stored"

    def test_recipes_share_create_returns_config_blocks(self, db_session):
        """create result includes config_blocks with hermes_yaml and claude_desktop_json."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            ctx=ctx,
        )
        assert "config_blocks" in result
        cb_result = result["config_blocks"]
        assert "hermes_yaml" in cb_result
        assert "claude_desktop_json" in cb_result

    def test_recipes_share_create_forbidden_for_non_owner(self, db_session):
        """Non-owner gets error response, not 403 exception."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        stranger = _make_user(db_session)
        ctx = _user_ctx(stranger.id)

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            ctx=ctx,
        )
        assert "error" in result


class TestRecipesShareList:
    def test_recipes_share_list_returns_only_active(self, db_session):
        """list returns tokens, but filtering is handled at caller level.

        The contract says list returns all tokens with is_active field so
        callers can distinguish. Verify at least one active token returned.
        """
        from app.mcp.tools.share import recipes_share_create, recipes_share_list

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        # Create two active tokens
        recipes_share_create(db_session, cookbook_id=str(cb.id), name="t1", ctx=ctx)
        recipes_share_create(db_session, cookbook_id=str(cb.id), name="t2", ctx=ctx)

        # Manually revoke one by inserting an inactive token
        cb_prefix = str(cb.id).replace("-", "")[:8]
        rand_hex = secrets.token_hex(16)
        inactive_token = f"cbt_{cb_prefix}_{rand_hex}"
        inactive_hash = hashlib.sha256(inactive_token.encode()).hexdigest()
        inactive_row = CookbookShareToken(
            id=uuid4(),
            bundle_id=cb.id,
            token_hash=inactive_hash,
            token_prefix=cb_prefix,
            scope="edit",
            name="inactive-t",
            is_active=False,
        )
        db_session.add(inactive_row)
        db_session.flush()

        result = recipes_share_list(db_session, cookbook_id=str(cb.id), ctx=ctx)
        assert "tokens" in result
        tokens = result["tokens"]

        active_tokens = [t for t in tokens if t["is_active"]]
        inactive_tokens = [t for t in tokens if not t["is_active"]]

        assert len(active_tokens) >= 2, "Should have at least 2 active tokens"
        assert len(inactive_tokens) >= 1, "Should have at least 1 inactive token"

        # Each token entry must have these fields
        for t in tokens:
            assert "id" in t
            assert "prefix" in t
            assert "scope" in t
            assert "is_active" in t
            assert "created_at" in t

    def test_recipes_share_list_forbidden_for_non_owner(self, db_session):
        """Non-owner gets error response."""
        from app.mcp.tools.share import recipes_share_list

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        stranger = _make_user(db_session)
        ctx = _user_ctx(stranger.id)

        result = recipes_share_list(db_session, cookbook_id=str(cb.id), ctx=ctx)
        assert "error" in result


class TestRecipesShareRevoke:
    def test_recipes_share_revoke_invalidates_immediately(self, db_session):
        """After revoke, list shows token as inactive."""
        from app.mcp.tools.share import (
            recipes_share_create,
            recipes_share_list,
            recipes_share_revoke,
        )

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        created = recipes_share_create(
            db_session, cookbook_id=str(cb.id), name="revoke-me", ctx=ctx
        )
        token_id = created["id"]

        revoke_result = recipes_share_revoke(
            db_session, cookbook_id=str(cb.id), token_id=token_id, ctx=ctx
        )
        assert revoke_result.get("revoked") is True
        assert revoke_result.get("token_id") == token_id

        # Verify in DB
        row = db_session.query(CookbookShareToken).filter(
            CookbookShareToken.id == UUID(token_id)
        ).first()
        assert row is not None
        assert row.is_active is False

        # Verify via list
        list_result = recipes_share_list(
            db_session, cookbook_id=str(cb.id), ctx=ctx
        )
        token_in_list = next(
            (t for t in list_result["tokens"] if t["id"] == token_id), None
        )
        assert token_in_list is not None
        assert token_in_list["is_active"] is False

    def test_recipes_share_revoke_returns_error_for_wrong_cookbook(self, db_session):
        """Revoking a token from another cookbook returns error."""
        from app.mcp.tools.share import recipes_share_create, recipes_share_revoke

        user = _make_user(db_session)
        cb_a = _make_cookbook(db_session, owner_id=user.id)
        cb_b = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        created = recipes_share_create(
            db_session, cookbook_id=str(cb_a.id), ctx=ctx
        )
        token_id = created["id"]

        # Try to revoke using cb_b
        result = recipes_share_revoke(
            db_session, cookbook_id=str(cb_b.id), token_id=token_id, ctx=ctx
        )
        assert "error" in result


class TestRecipesShareRotate:
    def test_recipes_share_rotate_creates_new_invalidates_old(self, db_session):
        """Rotate: old token is_active=False, new token is returned."""
        from app.mcp.tools.share import recipes_share_create, recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        created = recipes_share_create(
            db_session, cookbook_id=str(cb.id), name="rotate-me", scope="edit", ctx=ctx
        )
        old_token_id = created["id"]
        old_token = created["token"]

        rotate_result = recipes_share_rotate(
            db_session,
            cookbook_id=str(cb.id),
            token_id=old_token_id,
            ctx=ctx,
        )
        assert "new_token" in rotate_result
        assert "new_prefix" in rotate_result
        assert "old_token_id" in rotate_result
        assert "new_token_id" in rotate_result
        assert "config_blocks" in rotate_result

        assert rotate_result["old_token_id"] == old_token_id
        assert rotate_result["new_token"] != old_token
        assert rotate_result["new_token_id"] != old_token_id

        # Old token should be inactive in DB
        old_row = db_session.query(CookbookShareToken).filter(
            CookbookShareToken.id == UUID(old_token_id)
        ).first()
        assert old_row is not None
        assert old_row.is_active is False

        # New token should be active in DB
        new_row = db_session.query(CookbookShareToken).filter(
            CookbookShareToken.id == UUID(rotate_result["new_token_id"])
        ).first()
        assert new_row is not None
        assert new_row.is_active is True

    def test_recipes_share_rotate_returns_config_blocks(self, db_session):
        """Rotate result includes parseable config_blocks."""
        from app.mcp.tools.share import recipes_share_create, recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        created = recipes_share_create(db_session, cookbook_id=str(cb.id), ctx=ctx)
        rotate_result = recipes_share_rotate(
            db_session,
            cookbook_id=str(cb.id),
            token_id=created["id"],
            ctx=ctx,
        )
        cb_result = rotate_result["config_blocks"]
        assert "hermes_yaml" in cb_result
        assert "claude_desktop_json" in cb_result


class TestConfigBlocksParseable:
    def test_config_blocks_parseable(self, db_session):
        """config_blocks from create: yaml.safe_load + json.loads succeed."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        ctx = _master_ctx()

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            name="parse-test",
            ctx=ctx,
        )
        cb_result = result["config_blocks"]

        # hermes_yaml must be valid YAML
        parsed_yaml = yaml.safe_load(cb_result["hermes_yaml"])
        assert parsed_yaml is not None, "hermes_yaml must parse as YAML"

        # claude_desktop_json must be valid JSON
        parsed_json = json.loads(cb_result["claude_desktop_json"])
        assert isinstance(parsed_json, dict), "claude_desktop_json must be a JSON object"

    def test_config_blocks_formatter_standalone(self):
        """_config_block_formatter can be called directly for any token."""
        from app._config_block_formatter import build_config_blocks

        blocks = build_config_blocks(
            token="cbt_abcd1234_0123456789abcdef0123456789abcd",
            cookbook_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        assert "hermes_yaml" in blocks
        assert "claude_desktop_json" in blocks

        # Content checks
        parsed_yaml = yaml.safe_load(blocks["hermes_yaml"])
        assert parsed_yaml is not None

        parsed_json = json.loads(blocks["claude_desktop_json"])
        assert isinstance(parsed_json, dict)

        # Token should appear in both outputs
        assert "cbt_abcd1234_0123456789abcdef0123456789abcd" in blocks["hermes_yaml"]
        assert "cbt_abcd1234_0123456789abcdef0123456789abcd" in blocks["claude_desktop_json"]


class TestMcpShareDispatch:
    """Verify the 4 tools are registered in the MCP server dispatcher."""

    def test_share_tools_registered_in_dispatcher(self, db_session):
        """All 4 share tools are reachable via call_tool_sync."""
        from app.mcp.server import call_tool_sync

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        master_caller = {"scope": "master", "user_id": None, "api_key_id": None}

        # create
        result = call_tool_sync(
            "recipes_share_create",
            {"cookbook_id": str(cb.id), "name": "dispatch-test"},
            caller=master_caller,
            db=db_session,
        )
        assert "token" in result, f"Expected token in result, got: {result}"
        token_id = result["id"]

        # list
        list_result = call_tool_sync(
            "recipes_share_list",
            {"cookbook_id": str(cb.id)},
            caller=master_caller,
            db=db_session,
        )
        assert "tokens" in list_result

        # revoke
        revoke_result = call_tool_sync(
            "recipes_share_revoke",
            {"cookbook_id": str(cb.id), "token_id": token_id},
            caller=master_caller,
            db=db_session,
        )
        assert revoke_result.get("revoked") is True

        # Create another token for rotate test
        result2 = call_tool_sync(
            "recipes_share_create",
            {"cookbook_id": str(cb.id), "name": "rotate-dispatch"},
            caller=master_caller,
            db=db_session,
        )
        token_id2 = result2["id"]

        rotate_result = call_tool_sync(
            "recipes_share_rotate",
            {"cookbook_id": str(cb.id), "token_id": token_id2},
            caller=master_caller,
            db=db_session,
        )
        assert "new_token" in rotate_result


# ─── Additional coverage tests for error paths ───────────────────────────────


class TestShareToolsErrorPaths:
    """Cover error/edge branches in app/mcp/tools/share.py."""

    def test_create_with_ctx_none_defaults_to_master(self, db_session):
        """ctx=None defaults to master scope (covers line 59)."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        # ctx=None → should default to master and succeed
        result = recipes_share_create(db_session, cookbook_id=str(cb.id))
        assert "token" in result

    def test_create_with_bad_uuid_returns_error(self, db_session):
        """Bad cookbook_id UUID → cookbook_not_found error (covers lines 37-38, 63)."""
        from app.mcp.tools.share import recipes_share_create

        result = recipes_share_create(
            db_session, cookbook_id="not-a-valid-uuid", ctx=_master_ctx()
        )
        assert "error" in result
        assert result["error"] == "cookbook_not_found"

    def test_create_with_nonexistent_cookbook_returns_error(self, db_session):
        """Non-existent cookbook UUID → cookbook_not_found error."""
        from app.mcp.tools.share import recipes_share_create

        result = recipes_share_create(
            db_session,
            cookbook_id=str(uuid4()),  # valid UUID but doesn't exist
            ctx=_master_ctx(),
        )
        assert "error" in result
        assert result["error"] == "cookbook_not_found"

    def test_create_with_invalid_scope_returns_error(self, db_session):
        """Invalid scope → error dict, not exception (covers lines 77-81)."""
        from app.mcp.tools.share import recipes_share_create

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_create(
            db_session,
            cookbook_id=str(cb.id),
            scope="super_admin",  # invalid scope
            ctx=_master_ctx(),
        )
        assert "error" in result
        assert "invalid_scope" in result["error"]

    def test_list_with_ctx_none_defaults_to_master(self, db_session):
        """ctx=None defaults to master scope for list (covers line 105)."""
        from app.mcp.tools.share import recipes_share_list

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_list(db_session, cookbook_id=str(cb.id))
        assert "tokens" in result

    def test_list_with_bad_uuid_returns_error(self, db_session):
        """Bad cookbook_id → error (covers lines 37-38, 109)."""
        from app.mcp.tools.share import recipes_share_list

        result = recipes_share_list(
            db_session, cookbook_id="bad-uuid-here", ctx=_master_ctx()
        )
        assert "error" in result
        assert result["error"] == "cookbook_not_found"

    def test_revoke_with_ctx_none_defaults_to_master(self, db_session):
        """ctx=None defaults to master scope for revoke (covers line 133)."""
        from app.mcp.tools.share import recipes_share_create, recipes_share_revoke

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        created = recipes_share_create(db_session, cookbook_id=str(cb.id))

        # ctx=None → master
        result = recipes_share_revoke(
            db_session, cookbook_id=str(cb.id), token_id=created["id"]
        )
        assert result.get("revoked") is True

    def test_revoke_with_bad_uuid_returns_error(self, db_session):
        """Bad cookbook_id → error (covers lines 37-38, 137)."""
        from app.mcp.tools.share import recipes_share_revoke

        result = recipes_share_revoke(
            db_session,
            cookbook_id="not-valid",
            token_id=str(uuid4()),
            ctx=_master_ctx(),
        )
        assert "error" in result
        assert result["error"] == "cookbook_not_found"

    def test_revoke_forbidden_for_non_owner(self, db_session):
        """Non-owner gets forbidden error (covers line 140)."""
        from app.mcp.tools.share import recipes_share_revoke

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        stranger = _make_user(db_session)
        ctx = _user_ctx(stranger.id)

        result = recipes_share_revoke(
            db_session,
            cookbook_id=str(cb.id),
            token_id=str(uuid4()),
            ctx=ctx,
        )
        assert "error" in result
        assert result["error"] == "cookbook_forbidden"

    def test_revoke_nonexistent_token_returns_error(self, db_session):
        """Non-existent token_id → error dict via exception (covers lines 144-148)."""
        from app.mcp.tools.share import recipes_share_revoke

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_revoke(
            db_session,
            cookbook_id=str(cb.id),
            token_id=str(uuid4()),  # valid UUID but no matching row
            ctx=_master_ctx(),
        )
        assert "error" in result

    def test_rotate_with_ctx_none_defaults_to_master(self, db_session):
        """ctx=None defaults to master scope for rotate (covers line 169)."""
        from app.mcp.tools.share import recipes_share_create, recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        created = recipes_share_create(db_session, cookbook_id=str(cb.id))

        result = recipes_share_rotate(
            db_session, cookbook_id=str(cb.id), token_id=created["id"]
        )
        assert "new_token" in result

    def test_rotate_with_bad_uuid_returns_error(self, db_session):
        """Bad cookbook_id → error (covers lines 37-38, 173)."""
        from app.mcp.tools.share import recipes_share_rotate

        result = recipes_share_rotate(
            db_session,
            cookbook_id="totally-invalid",
            token_id=str(uuid4()),
            ctx=_master_ctx(),
        )
        assert "error" in result
        assert result["error"] == "cookbook_not_found"

    def test_rotate_forbidden_for_non_owner(self, db_session):
        """Non-owner gets forbidden error (covers line 176)."""
        from app.mcp.tools.share import recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        stranger = _make_user(db_session)
        ctx = _user_ctx(stranger.id)

        result = recipes_share_rotate(
            db_session,
            cookbook_id=str(cb.id),
            token_id=str(uuid4()),
            ctx=ctx,
        )
        assert "error" in result
        assert result["error"] == "cookbook_forbidden"

    def test_rotate_nonexistent_token_returns_error(self, db_session):
        """Non-existent token_id → error dict (covers lines 185-189)."""
        from app.mcp.tools.share import recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_rotate(
            db_session,
            cookbook_id=str(cb.id),
            token_id=str(uuid4()),  # valid UUID but no matching row
            ctx=_master_ctx(),
        )
        assert "error" in result

    def test_rotate_invalid_token_id_uuid_returns_error(self, db_session):
        """Invalid token_id UUID → error dict from _rotate_service (covers lines 297-298)."""
        from app.mcp.tools.share import recipes_share_rotate

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_rotate(
            db_session,
            cookbook_id=str(cb.id),
            token_id="not-a-uuid",  # triggers ValueError in _rotate_service
            ctx=_master_ctx(),
        )
        assert "error" in result

    def test_revoke_invalid_token_id_uuid_returns_error(self, db_session):
        """Invalid token_id UUID → error dict from _revoke_service (covers lines 362-363)."""
        from app.mcp.tools.share import recipes_share_revoke

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)

        result = recipes_share_revoke(
            db_session,
            cookbook_id=str(cb.id),
            token_id="not-a-uuid-either",  # triggers ValueError in _revoke_service
            ctx=_master_ctx(),
        )
        assert "error" in result
