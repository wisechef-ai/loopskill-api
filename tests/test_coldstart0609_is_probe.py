"""coldstart_0609/A — is_probe tagging on missing_skill_queries + install_events.

One function (app.services.probe_detection.is_probe_request) decides
"is this write a known probe?" for both writers. These tests pin:
  1. the standalone predicate against every documented email/IP,
  2. record_missing_skill_query stamping is_probe from api_key_id/client_ip,
  3. _record_install_event (the shared cookbook/MCP install helper) doing
     the same for install_events,
  4. the migration adding the column NOT NULL DEFAULT false on both tables.
"""

from __future__ import annotations

import uuid

from app.models import APIKey, InstallEvent, MissingSkillQuery, Skill, User
from app.services.probe_detection import (
    PROBE_CLIENT_IPS,
    PROBE_USER_EMAILS,
    is_probe_request,
)


def _mk_user(db, email):
    u = User(id=uuid.uuid4(), display_name="probe-test-user", email=email)
    db.add(u)
    db.flush()
    return u


def _mk_api_key(db, user):
    k = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix="rec_test",
        key_hash="hash-" + uuid.uuid4().hex,
    )
    db.add(k)
    db.flush()
    return k


def _mk_skill(db):
    s = Skill(id=uuid.uuid4(), slug=f"skill-{uuid.uuid4().hex[:8]}", title="Test Skill", is_public=True)
    db.add(s)
    db.flush()
    return s


# ── 1. the predicate itself ────────────────────────────────────────────────


class TestIsProbeRequestPredicate:
    def test_no_identifiers_is_not_a_probe(self, db_session):
        assert is_probe_request(db_session, api_key_id=None, client_ip=None) is False

    def test_known_probe_email_via_api_key_is_a_probe(self, db_session):
        for email in PROBE_USER_EMAILS:
            user = _mk_user(db_session, email)
            key = _mk_api_key(db_session, user)
            assert is_probe_request(db_session, api_key_id=key.id, client_ip=None) is True, email

    def test_ordinary_user_email_via_api_key_is_not_a_probe(self, db_session):
        user = _mk_user(db_session, "genuine-customer@example.com")
        key = _mk_api_key(db_session, user)
        assert is_probe_request(db_session, api_key_id=key.id, client_ip=None) is False

    def test_unknown_api_key_id_is_not_a_probe(self, db_session):
        assert is_probe_request(db_session, api_key_id=uuid.uuid4(), client_ip=None) is False

    def test_known_probe_ips_are_probes(self, db_session):
        for ip in PROBE_CLIENT_IPS:
            assert is_probe_request(db_session, api_key_id=None, client_ip=ip) is True, ip

    def test_ordinary_ip_is_not_a_probe(self, db_session):
        assert is_probe_request(db_session, api_key_id=None, client_ip="8.8.8.8") is False

    def test_probe_ip_wins_even_with_a_genuine_key(self, db_session):
        """IP match short-circuits before the api_key lookup — either signal
        alone is sufficient (OR semantics per coldstart_0609/A spec)."""
        user = _mk_user(db_session, "genuine-customer@example.com")
        key = _mk_api_key(db_session, user)
        assert is_probe_request(db_session, api_key_id=key.id, client_ip="195.128.172.227") is True


# ── 2. MissingSkillQuery writer stamps is_probe ────────────────────────────


class TestMissingSkillQueryIsProbe:
    def test_probe_ip_search_is_stamped(self, db_session):
        from app.services.demand_capture import record_missing_skill_query

        record_missing_skill_query(db_session, "probe search", client_ip="127.0.0.1")
        row = db_session.query(MissingSkillQuery).filter(MissingSkillQuery.query == "probe search").one()
        assert row.is_probe is True

    def test_probe_api_key_search_is_stamped(self, db_session):
        from app.services.demand_capture import record_missing_skill_query

        user = _mk_user(db_session, "tori@wisechef.ai")
        key = _mk_api_key(db_session, user)

        record_missing_skill_query(db_session, "fleet search", api_key_id=key.id)
        row = db_session.query(MissingSkillQuery).filter(MissingSkillQuery.query == "fleet search").one()
        assert row.is_probe is True

    def test_ordinary_search_defaults_to_not_probe(self, db_session):
        from app.services.demand_capture import record_missing_skill_query

        record_missing_skill_query(db_session, "genuine search")
        row = db_session.query(MissingSkillQuery).filter(MissingSkillQuery.query == "genuine search").one()
        assert row.is_probe is False


# ── 3. InstallEvent writer stamps is_probe ──────────────────────────────────


class TestInstallEventIsProbe:
    def test_shared_install_helper_stamps_probe_ip(self, db_session):
        from app._skill_helpers import _record_install_event

        skill = _mk_skill(db_session)

        class _FakeClient:
            host = "195.128.172.227"

        class _FakeState:
            api_key_id = None

        class _FakeRequest:
            client = _FakeClient()
            state = _FakeState()
            headers: dict = {}

        _record_install_event(db_session, skill=skill, version_semver="1.0.0", request=_FakeRequest())
        db_session.flush()

        row = db_session.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).one()
        assert row.is_probe is True

    def test_shared_install_helper_ordinary_caller_not_probe(self, db_session):
        from app._skill_helpers import _record_install_event

        skill = _mk_skill(db_session)

        class _FakeClient:
            host = "8.8.8.8"

        class _FakeState:
            api_key_id = None

        class _FakeRequest:
            client = _FakeClient()
            state = _FakeState()
            headers: dict = {}

        _record_install_event(db_session, skill=skill, version_semver="1.0.0", request=_FakeRequest())
        db_session.flush()

        row = db_session.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).one()
        assert row.is_probe is False

    def test_no_request_defaults_to_not_probe(self, db_session):
        from app._skill_helpers import _record_install_event

        skill = _mk_skill(db_session)
        _record_install_event(db_session, skill=skill, version_semver="1.0.0", request=None)
        db_session.flush()

        row = db_session.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).one()
        assert row.is_probe is False

    def test_provenance_writer_stamps_probe_email(self, db_session):
        from app.services.provenance import record_install_with_provenance

        skill = _mk_skill(db_session)
        user = _mk_user(db_session, "system@loopskill.io")
        key = _mk_api_key(db_session, user)

        class _FakeClient:
            host = "8.8.8.8"

        class _FakeState:
            api_key_id = key.id

        class _FakeRequest:
            client = _FakeClient()
            state = _FakeState()
            headers: dict = {}

        event, _pid = record_install_with_provenance(
            db_session, skill=skill, version_semver="1.0.0", request=_FakeRequest()
        )
        db_session.flush()
        assert event.is_probe is True


# ── 4. schema: column exists, non-null, defaults False ──────────────────────


class TestIsProbeColumnSchema:
    def test_install_event_column_declared_non_null_default_false(self):
        col = InstallEvent.__table__.c.is_probe
        assert col.nullable is False
        assert bool(col.default.arg) is False

    def test_missing_skill_query_column_declared_non_null_default_false(self):
        col = MissingSkillQuery.__table__.c.is_probe
        assert col.nullable is False
        assert bool(col.default.arg) is False

    def test_orm_insert_without_is_probe_defaults_false(self, db_session):
        skill = _mk_skill(db_session)
        ev = InstallEvent(skill_id=skill.id, version_semver="1.0.0")
        db_session.add(ev)
        db_session.flush()
        db_session.refresh(ev)
        assert ev.is_probe is False
