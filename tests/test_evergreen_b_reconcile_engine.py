"""evergreen_0206 Phase B — full reconcile engine {add, update, remove, drift}.

Pins the reconcile-contract §1 diff shape against the new
app/services/reconcile.py engine. Six diff shapes (add/update/remove/drift/
mixed/no-op) + prune gating + backward-compat that recipes_sync is untouched.
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Cookbook, CookbookSkill, Skill, SkillVersion, User
from app.services.reconcile import (
    LocalSkillState,
    compute_reconcile_plan,
    recipes_reconcile,
)


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
def db(engine_fixture) -> Generator[Session, None, None]:
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


# ─────────────────────────── Helpers ────────────────────────────────────


def _user(db: Session) -> User:
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Owner",
        email=f"{uid}@test.example",
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _cookbook(db: Session, owner: User) -> Cookbook:
    cb = Cookbook(id=uuid4(), name="CB", is_base=False, cookbook_owner=owner.id)
    db.add(cb)
    db.flush()
    return cb


def _skill(db: Session, slug: str, versions: list[tuple[str, str]]) -> Skill:
    """versions: list of (semver, sha256)."""
    s = Skill(id=uuid4(), slug=slug, title=slug, description="x", is_public=True)
    db.add(s)
    db.flush()
    for semver, sha in versions:
        db.add(
            SkillVersion(
                id=uuid4(),
                skill_id=s.id,
                semver=semver,
                tarball_path=f"/tmp/{slug}-{semver}.tar.gz",
                tarball_size_bytes=10,
                checksum_sha256=sha,
            )
        )
    db.flush()
    return s


def _declare(db: Session, cb: Cookbook, skill: Skill, *, source="custom-added", pin=None):
    db.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source=source, pinned_version=pin))
    db.flush()


# ─────────────────────────── The six shapes ─────────────────────────────


class TestComputeReconcilePlan:
    def test_add_only(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "add-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, pin="1.0.0")
        db.commit()

        plan = compute_reconcile_plan(db, cb.id, local=[])  # nothing local
        assert len(plan.add) == 1
        assert plan.add[0]["slug"] == "add-skill"
        assert plan.add[0]["version"] == "1.0.0"
        assert plan.add[0]["checksum_sha256"] == "a" * 64
        assert not plan.update and not plan.remove and not plan.drift

    def test_update_only(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "upd-skill", [("1.0.0", "a" * 64), ("2.0.0", "b" * 64)])
        _declare(db, cb, s, pin="2.0.0")
        db.commit()

        local = [LocalSkillState(slug="upd-skill", pinned_version="1.0.0", sha256="a" * 64)]
        plan = compute_reconcile_plan(db, cb.id, local=local)
        assert len(plan.update) == 1
        assert plan.update[0] == {
            "slug": "upd-skill",
            "from": "1.0.0",
            "to": "2.0.0",
            "checksum_sha256": "b" * 64,
        }
        assert not plan.add and not plan.remove and not plan.drift

    def test_remove_only_requires_prune(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        # Cookbook declares nothing; local has a skill not in the cookbook.
        db.commit()
        local = [LocalSkillState(slug="orphan", pinned_version="1.0.0")]

        # Default: no prune → remove is empty (premortem #4).
        plan = compute_reconcile_plan(db, cb.id, local=local, prune=False)
        assert plan.no_op, "default reconcile must never remove"

        # prune=True → orphan emitted.
        plan2 = compute_reconcile_plan(db, cb.id, local=local, prune=True)
        assert plan2.remove == [{"slug": "orphan"}]
        assert not plan2.add and not plan2.update and not plan2.drift

    def test_remove_via_disabled_source(self, db):
        """A skill with source='disabled' is undeclared → prune removes it."""
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "dis-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, source="disabled", pin="1.0.0")
        db.commit()

        local = [LocalSkillState(slug="dis-skill", pinned_version="1.0.0")]
        plan = compute_reconcile_plan(db, cb.id, local=local, prune=True)
        assert plan.remove == [{"slug": "dis-skill"}]

    def test_drift_only(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "drift-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, pin="1.0.0")
        db.commit()

        # Right version, WRONG sha → drift.
        local = [LocalSkillState(slug="drift-skill", pinned_version="1.0.0", sha256="f" * 64)]
        plan = compute_reconcile_plan(db, cb.id, local=local)
        assert len(plan.drift) == 1
        assert plan.drift[0]["slug"] == "drift-skill"
        assert plan.drift[0]["expected_sha256"] == "a" * 64
        assert plan.drift[0]["current_sha256"] == "f" * 64
        assert not plan.add and not plan.update and not plan.remove

    def test_mixed(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s_add = _skill(db, "m-add", [("1.0.0", "1" * 64)])
        s_upd = _skill(db, "m-upd", [("1.0.0", "2" * 64), ("2.0.0", "3" * 64)])
        s_drift = _skill(db, "m-drift", [("1.0.0", "4" * 64)])
        _declare(db, cb, s_add, pin="1.0.0")
        _declare(db, cb, s_upd, pin="2.0.0")
        _declare(db, cb, s_drift, pin="1.0.0")
        db.commit()

        local = [
            # m-add absent → ADD
            LocalSkillState(slug="m-upd", pinned_version="1.0.0", sha256="2" * 64),  # UPDATE
            LocalSkillState(slug="m-drift", pinned_version="1.0.0", sha256="9" * 64),  # DRIFT
            LocalSkillState(slug="m-orphan", pinned_version="1.0.0"),  # REMOVE (prune)
        ]
        plan = compute_reconcile_plan(db, cb.id, local=local, prune=True)
        assert {a["slug"] for a in plan.add} == {"m-add"}
        assert {u["slug"] for u in plan.update} == {"m-upd"}
        assert {d["slug"] for d in plan.drift} == {"m-drift"}
        assert {r["slug"] for r in plan.remove} == {"m-orphan"}

    def test_no_op(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "noop-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, pin="1.0.0")
        db.commit()

        local = [LocalSkillState(slug="noop-skill", pinned_version="1.0.0", sha256="a" * 64)]
        plan = compute_reconcile_plan(db, cb.id, local=local)
        assert plan.no_op


# ─────────────────────── Tool-level plan / apply ────────────────────────


class TestRecipesReconcileTool:
    def test_dry_run_returns_diff_no_writes(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "dr-skill", [("1.0.0", "a" * 64), ("2.0.0", "b" * 64)])
        _declare(db, cb, s, pin="2.0.0")
        db.commit()

        ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
        local = [{"slug": "dr-skill", "pinned_version": "1.0.0", "sha256": "a" * 64}]
        res = recipes_reconcile(db, cookbook_id=str(cb.id), local=local, dry_run=True, ctx=ctx)
        assert res["no_op"] is False
        assert res["diff"]["update"][0]["slug"] == "dr-skill"
        assert "applied" not in res
        # No write: pin still 1.0.0 absent (cookbook declared 2.0.0 but local at 1.0.0;
        # dry run must not have mutated the CookbookSkill pin which was 2.0.0 already)
        cs = db.query(CookbookSkill).filter(CookbookSkill.cookbook_id == cb.id).first()
        assert cs.pinned_version == "2.0.0"

    def test_apply_advances_generation(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "ap-skill", [("1.0.0", "a" * 64), ("2.0.0", "b" * 64)])
        _declare(db, cb, s, pin="2.0.0")
        # Backdate generation so any bump is detectable on whole-second SQLite clocks.
        from datetime import datetime, timezone

        db.query(Cookbook).filter(Cookbook.id == cb.id).update(
            {"updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}, synchronize_session=False
        )
        db.commit()
        before = db.query(Cookbook).filter(Cookbook.id == cb.id).first().updated_at

        ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
        local = [{"slug": "ap-skill", "pinned_version": "1.0.0", "sha256": "a" * 64}]
        res = recipes_reconcile(db, cookbook_id=str(cb.id), local=local, dry_run=False, ctx=ctx)
        assert res["applied"] is True
        db.expire_all()
        after = db.query(Cookbook).filter(Cookbook.id == cb.id).first().updated_at
        assert after > before, "apply with an UPDATE row must advance the generation token"

    def test_apply_noop_does_not_bump(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "apn-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, pin="1.0.0")
        from datetime import datetime, timezone

        db.query(Cookbook).filter(Cookbook.id == cb.id).update(
            {"updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}, synchronize_session=False
        )
        db.commit()
        before = db.query(Cookbook).filter(Cookbook.id == cb.id).first().updated_at

        ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
        local = [{"slug": "apn-skill", "pinned_version": "1.0.0", "sha256": "a" * 64}]
        res = recipes_reconcile(db, cookbook_id=str(cb.id), local=local, dry_run=False, ctx=ctx)
        assert res["no_op"] is True
        db.expire_all()
        after = db.query(Cookbook).filter(Cookbook.id == cb.id).first().updated_at
        assert after == before, "a no-op reconcile must not advance the generation token"


class TestReconcileIsolation:
    """Tenant isolation (reconcile-contract §7): non-owner cannot reconcile."""

    def test_non_owner_forbidden(self, db):
        owner = _user(db)
        intruder = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "iso-skill", [("1.0.0", "a" * 64)])
        _declare(db, cb, s, pin="1.0.0")
        db.commit()

        ctx = AuthContext(scope="user", user_id=intruder.id, tier="pro")
        res = recipes_reconcile(db, cookbook_id=str(cb.id), local=[], ctx=ctx)
        assert (
            res.get("error") == "cookbook_forbidden"
        ), "a non-owner must not be able to reconcile another tenant's cookbook"


class TestBackwardCompat:
    """recipes_sync must keep its update-only behavior (unchanged surface)."""

    def test_recipes_sync_still_update_only(self, db):
        from app.mcp.tools.recipes_sync import recipes_sync

        owner = _user(db)
        cb = _cookbook(db, owner)
        s = _skill(db, "bc-skill", [("1.0.0", "a" * 64), ("2.0.0", "b" * 64)])
        _declare(db, cb, s, source="overridden", pin="1.0.0")
        db.commit()

        ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
        res = recipes_sync(db=db, ctx=ctx, cookbook_id=str(cb.id))
        # Legacy shape: 'changes' with action='update', not the new {add,update,...} diff.
        assert "changes" in res
        assert res["changes"][0]["action"] == "update"
        assert res.get("applied") is True
