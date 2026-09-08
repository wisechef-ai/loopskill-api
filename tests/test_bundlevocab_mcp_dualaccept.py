"""bundlevocab cutover — MCP dual-accept + dual-emit for the bundle identifier.

The product renamed 'cookbook' → 'bundle' (DB already migrated: models are
Bundle*). This suite pins the AGENT-FACING wire contract on the MCP surface:

* INPUT dual-accept: an agent may call any affected tool with EITHER
  ``bundle_id`` (canonical) OR ``cookbook_id`` (legacy pre-rename wire name).
  Dispatch reads ``bundle_id`` first, falls back to ``cookbook_id``, and
  raises the same KeyError the legacy path raised when both are absent.
* OUTPUT dual-emit: every response that carries ``cookbook_id`` (or a nested
  ``cookbook`` object) also carries ``bundle_id`` / ``bundle`` with the
  identical value/content — legacy key kept, canonical key added.
* SCHEMA dual-advertise: every tool schema exposing ``cookbook_id`` also
  exposes ``bundle_id``, and neither single spelling is hard-required
  (validation moved to the dispatch-seam helper).

Modeled on tests/test_qa0208_dualaccept.py (new-canonical-primary,
legacy-accepted-as-fallback, nothing-that-works-today-breaks).
"""

from __future__ import annotations

import hashlib
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Bundle, Fleet, FleetSubscription, User


# ── Fixtures (module-scoped engine + per-test rollback, qa0208 style) ───────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _rec):
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


def _make_user(db: Session) -> User:
    user = User(
        id=uuid4(),
        display_name="Dual Vocab",
        email=f"{uuid4()}@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_bundle(db: Session, owner_id) -> Bundle:
    cb = Bundle(
        id=uuid4(),
        name="DualVocab CB",
        description="d",
        is_base=False,
        bundle_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _master_caller(user) -> dict:
    return {"scope": "master", "user_id": None, "api_key_id": None}


def _call(db: Session, tool: str, args: dict, user: User) -> dict:
    from app.mcp.server import call_tool_sync

    return call_tool_sync(tool, args, caller=_master_caller(user), db=db)


def _make_fleet(db: Session, owner_id) -> Fleet:
    fleet = Fleet(
        id=uuid4(),
        owner_user_id=owner_id,
        name=f"fleet-{uuid4().hex[:8]}",
        fleet_api_key_hash=hashlib.sha256(uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


# ── 1/2/3. Input dual-accept at the dispatch seam ───────────────────────────


class TestInputDualAccept:
    def test_bundle_id_only_works(self, db_session):
        """(1) calling the dispatch with only 'bundle_id' works."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        out = _call(db_session, "loopskill_share_list", {"bundle_id": str(cb.id)}, user)
        assert "error" not in out, out
        assert "tokens" in out

    def test_cookbook_id_only_legacy_still_works_same_result(self, db_session):
        """(2) calling with only 'cookbook_id' (legacy) works and returns the same result."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        canonical = _call(db_session, "loopskill_share_list", {"bundle_id": str(cb.id)}, user)
        legacy = _call(db_session, "loopskill_share_list", {"cookbook_id": str(cb.id)}, user)
        assert "error" not in legacy, legacy
        assert legacy == canonical

    def test_both_when_agreeing_works(self, db_session):
        """(3) calling with BOTH, where they agree, works."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        both = _call(
            db_session,
            "loopskill_share_list",
            {"bundle_id": str(cb.id), "cookbook_id": str(cb.id)},
            user,
        )
        assert "error" not in both, both
        assert both == _call(db_session, "loopskill_share_list", {"bundle_id": str(cb.id)}, user)

    def test_neither_errors_the_legacy_way(self, db_session):
        """(4) calling with NEITHER errors the same way it did before (KeyError)."""
        from app.mcp.server import call_tool_sync

        user = _make_user(db_session)
        with pytest.raises(KeyError):
            call_tool_sync(
                "loopskill_share_list",
                {},
                caller=_master_caller(user),
                db=db_session,
            )

    def test_sync_accepts_bundle_id_only(self, db_session):
        """loopskill_sync (previously required cookbook_id) accepts bundle_id alone."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        out = _call(db_session, "loopskill_sync", {"bundle_id": str(cb.id)}, user)
        assert out.get("bundle_id") == str(cb.id)
        assert out.get("cookbook_id") == str(cb.id)
        assert "error" not in out, out

    def test_fleet_subscribe_accepts_bundle_id_only(self, db_session):
        """loopskill_fleet_subscribe accepts bundle_id in place of cookbook_id."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)
        fleet = _make_fleet(db_session, owner_id=user.id)

        out = _call(
            db_session,
            "loopskill_fleet_subscribe",
            {"fleet_id": str(fleet.id), "bundle_id": str(cb.id)},
            user,
        )
        assert out.get("channel") == "stable", out
        assert out.get("bundle_id") == out.get("cookbook_id") == str(cb.id)

    def test_share_create_accepts_cookbook_id_only(self, db_session):
        """Legacy-only agent call: share_create with only cookbook_id still works."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        out = _call(db_session, "loopskill_share_create", {"cookbook_id": str(cb.id), "name": "legacy"}, user)
        assert out.get("error") != "bundle_not_found", out
        assert out.get("token", "").startswith("cbt_"), out


# ── 5. Output dual-emit: bundle_id accompanies cookbook_id ─────────────────


class TestOutputDualEmit:
    def test_sync_response_carries_both_keys(self, db_session):
        """(5) sync response emits both bundle_id and cookbook_id, equal."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        for args in ({"bundle_id": str(cb.id)}, {"cookbook_id": str(cb.id)}):
            out = _call(db_session, "loopskill_sync", args, user)
            assert out["bundle_id"] == out["cookbook_id"] == str(cb.id)

    def test_error_payloads_carry_both_keys(self, db_session):
        """(5) error payloads (not_found) emit both spellings."""
        user = _make_user(db_session)
        ghost = str(uuid4())

        out = _call(db_session, "loopskill_sync", {"bundle_id": ghost}, user)
        assert out.get("error") == "not_found"
        assert out["bundle_id"] == out["cookbook_id"] == ghost

    def test_share_error_payloads_carry_both_keys(self, db_session):
        out = _call(
            db_session,
            "loopskill_share_list",
            {"bundle_id": str(uuid4())},
            _make_user(db_session),
        )
        assert out.get("error") == "bundle_not_found"
        assert out["bundle_id"] == out["cookbook_id"]

    def test_list_bundle_emits_bundle_object_sibling(self, db_session):
        """(5) nested 'cookbook' object gains a 'bundle' sibling with same content."""
        from app.mcp.server import call_tool_sync

        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)

        out = call_tool_sync(
            "loopskill_list_bundle",
            {},
            caller={"scope": "master", "user_id": user.id, "api_key_id": None},
            db=db_session,
        )
        assert out["cookbook"] is not None
        assert out["bundle"] == out["cookbook"]
        assert out["bundle"]["id"] == str(cb.id)

    def test_fleet_subscribe_response_carries_both_keys(self, db_session):
        """(5) fleet_subscribe success payload emits both spellings."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)
        fleet = _make_fleet(db_session, owner_id=user.id)

        out = _call(
            db_session,
            "loopskill_fleet_subscribe",
            {"fleet_id": str(fleet.id), "cookbook_id": str(cb.id)},
            user,
        )
        assert "error" not in out, out
        assert out["bundle_id"] == out["cookbook_id"] == str(cb.id)

    def test_fleet_list_subscriptions_carry_both_keys(self, db_session):
        """(5) fleet_list subscription rows emit both spellings."""
        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)
        fleet = _make_fleet(db_session, owner_id=user.id)
        db_session.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="stable"))
        db_session.flush()

        out = _call(db_session, "loopskill_fleet_list", {}, user)
        fleet_row = next(f for f in out["fleets"] if f["fleet_id"] == str(fleet.id))
        sub = fleet_row["subscriptions"][0]
        assert sub["bundle_id"] == sub["cookbook_id"] == str(cb.id)

    def test_direct_tool_payloads_carry_both_keys(self, db_session):
        """(5) direct tool-fn payloads (share, bundle_install) emit both spellings."""
        from app.mcp.tools.bundle_install import loopskill_bundle_install
        from app.mcp.tools.share import loopskill_share_list

        user = _make_user(db_session)
        cb = _make_bundle(db_session, owner_id=user.id)
        ctx = AuthContext(scope="master")

        listed = loopskill_share_list(db_session, cookbook_id=str(cb.id), ctx=ctx)
        assert "error" not in listed, listed

        installed = loopskill_bundle_install(db=db_session, ctx=ctx, cookbook_id=str(cb.id))
        assert installed["bundle_id"] == installed["cookbook_id"] == str(cb.id)


# ── 6. Schema dual-advertise: bundle_id sibling on every cookbook_id tool ───


def _all_tool_schemas():
    from app.mcp.registry import _tool_definitions

    for tool in _tool_definitions():
        schema = tool.inputSchema or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        yield tool.name, schema, props


class TestSchemaDualAdvertise:
    def test_every_cookbook_id_tool_also_advertises_bundle_id(self):
        """(6) every tool schema exposing 'cookbook_id' also exposes 'bundle_id'."""
        offenders = []
        for name, _schema, props in _all_tool_schemas():
            if "cookbook_id" in props and "bundle_id" not in props:
                offenders.append(name)
        assert not offenders, f"tools missing bundle_id sibling: {offenders}"

    def test_no_schema_hard_requires_cookbook_id(self):
        """(6) no schema declares 'cookbook_id' as required (validation is at the seam)."""
        offenders = []
        for name, schema, _props in _all_tool_schemas():
            if "cookbook_id" in (schema.get("required") or []):
                offenders.append(name)
        assert not offenders, f"tools still hard-requiring cookbook_id: {offenders}"

    def test_bundle_id_same_type_as_cookbook_id(self):
        """(6) the bundle_id sibling has the same JSON type as cookbook_id."""
        for _name, _schema, props in _all_tool_schemas():
            if "cookbook_id" in props:
                assert props["bundle_id"]["type"] == props["cookbook_id"]["type"]
