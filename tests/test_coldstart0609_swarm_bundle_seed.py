"""Seed test for the public ``swarm-refactor-campaign`` bundle (coldstart_0609 clause #9).

Contract pinned here (behaviour, not snapshot):
  * two internal catalog members + one federated (botmaker via github-botmaker tap)
  * the federated member is a PRIVATE pointer row (``ext:...``), never rehosted
  * a missing member aborts with rc=1 and writes NOTHING (no half-verified bundle)
  * idempotent: second run attaches 0, reports already_present=3
  * --dry-run writes nothing
Fixture shape mirrors tests/test_marketing_0712_tap_bundle.py.
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Bundle, BundleSkill, Skill
from app.services.federation import ExternalSkill, InstallPath


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, trans):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _stub_botmaker_ext(source: str, slug: str) -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title="botmaker",
        source=source,
        install_path=InstallPath.FETCH_ORIGIN,
        origin_url="https://github.com/techjanitor/botmaker/tree/main/skills/autonomous-ai-agents/botmaker",
        license="MIT",
        redistributable=True,
        description="A Hermes SOUL+skill for minting specialist bots",
    )


def _internal_skill(db, slug: str) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=slug, description=slug, tier="free", is_public=True)
    db.add(s)
    db.flush()
    return s


def _wire(monkeypatch, db_session):
    import scripts.seed_swarm_refactor_bundle as seed
    from app.services import bundle_external as be

    monkeypatch.setattr(be, "_resolve_external", _stub_botmaker_ext)
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
    monkeypatch.setattr(db_session, "close", lambda: None)
    return seed


class TestSeedSwarmRefactorBundle:
    def test_composes_public_verified_bundle_with_federated_pointer(self, db_session, monkeypatch, capsys):
        seed = _wire(monkeypatch, db_session)
        for slug in seed.INTERNAL_SKILLS:
            _internal_skill(db_session, slug)

        assert seed.seed(dry_run=False) == 0
        cb = db_session.query(Bundle).filter(Bundle.slug == seed.BUNDLE_SLUG).one()
        assert cb.visibility == "public" and cb.is_verified is True and cb.is_base is False
        assert "techjanitor/botmaker" in cb.description and "MIT" in cb.description
        member_slugs = {
            db_session.query(Skill).get(m.skill_id).slug
            for m in db_session.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id)
        }
        assert set(seed.INTERNAL_SKILLS) <= member_slugs
        ext = [s for s in member_slugs if s.startswith("ext:github-botmaker:")]
        assert len(ext) == 1
        assert (
            db_session.query(Skill).filter(Skill.slug == ext[0]).one().is_public is False
        )  # pointer stays private

        # idempotent second run
        assert seed.seed(dry_run=False) == 0
        assert "attached=0 already_present=3" in capsys.readouterr().out
        assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).count() == 3

    def test_missing_internal_member_aborts_and_writes_nothing(self, db_session, monkeypatch, capsys):
        seed = _wire(monkeypatch, db_session)
        _internal_skill(db_session, seed.INTERNAL_SKILLS[0])  # the second one is absent

        assert seed.seed(dry_run=False) == 1
        assert "ABORT" in capsys.readouterr().err
        assert db_session.query(Bundle).filter(Bundle.slug == seed.BUNDLE_SLUG).first() is None

    def test_dry_run_writes_nothing(self, db_session, monkeypatch):
        seed = _wire(monkeypatch, db_session)
        for slug in seed.INTERNAL_SKILLS:
            _internal_skill(db_session, slug)
        assert seed.seed(dry_run=True) == 0
        assert db_session.query(Bundle).filter(Bundle.slug == seed.BUNDLE_SLUG).first() is None
