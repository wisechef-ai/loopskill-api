"""t_f0598839: adam-xps egress, loopback and fleet agent keys are not strangers.

Pins the SHIPPED config/fleet_exclusions.yaml (not a fixture copy): the
funnel's installed:stranger count was 201 against ~13 real, 188 of them from
195.128.172.73 and 5 from ::1.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from app.models import APIKey, FunnelEvent, InstallEvent, Skill, User
from app.services.funnel_backfill import _classify_install, reclassify_hosting_installs
from app.services.funnel_ledger import (
    classify,
    clear_fleet_exclusions_cache,
    normalize_ip,
    record_event,
    resolve_entity,
)

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "fleet_exclusions.yaml"
PINNED_KEYS = [
    "522c90f2-c3b4-4ab3-9602-536f326ecaab",
    "45d60407-dde5-4d66-a64f-6627d8b30338",
    "4f6924c9-4f04-447b-8170-fdc006ed7697",
    "2e8742c1-4c12-41ba-a14b-33b5916585a2",
    "bf954c6c-b94b-456b-8de4-4ac70e0588a8",
    "e6713526-6600-4b0b-84f5-8fe6281ee304",
]
# The real stranger installs in the prod ledger; none may flip to fleet.
HISTORICAL_STRANGERS = ["176.111.123.46", "79.77.179.182", "155.4.12.121", "85.11.167.136"]


@pytest.fixture(autouse=True)
def _shipped_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "FUNNEL_FLEET_EXCLUSIONS_PATH", "", raising=False)
    clear_fleet_exclusions_cache()
    yield
    clear_fleet_exclusions_cache()


def test_adam_xps_egress_is_fleet():
    assert classify(ip="195.128.172.73") == ("fleet", "ip:195.128.172.73 in fleet_exclusions.ips")


@pytest.mark.parametrize("spelling", ["::1", "0:0:0:0:0:0:0:1", "0000::0001", " ::1 ", "127.0.0.1"])
def test_loopback_is_fleet_in_any_spelling(spelling):
    assert classify(ip=spelling)[0] == "fleet"


def test_ipv4_mapped_ipv6_matches_its_ipv4_entry():
    assert normalize_ip("::ffff:195.128.172.73") == "195.128.172.73"
    assert classify(ip="::ffff:195.128.172.73")[0] == "fleet"


def test_non_ip_value_normalises_to_itself():
    assert normalize_ip(" not-an-ip ") == "not-an-ip"


@pytest.mark.parametrize("ip", HISTORICAL_STRANGERS)
def test_real_strangers_stay_strangers(ip):
    assert classify(ip=ip)[0] == "stranger"


@pytest.mark.parametrize(
    "neighbour", ["195.128.172.1", "195.128.172.72", "195.128.172.74", "195.128.172.254"]
)
def test_no_slash24_neighbours_are_swallowed(neighbour):
    assert classify(ip=neighbour)[0] == "stranger"


def test_shipped_config_has_no_cidr_and_no_name_rule():
    data = yaml.safe_load(SHIPPED.read_text())
    assert set(data) == {"emails", "ips", "api_key_ids"}
    assert all("/" not in str(ip) for ip in data["ips"])
    assert {"195.128.172.73", "::1", "127.0.0.1"} <= {str(i) for i in data["ips"]}
    for key in data["api_key_ids"]:
        uuid.UUID(str(key))  # exact ids only; a pattern would not parse
    assert set(PINNED_KEYS) <= {str(k) for k in data["api_key_ids"]}


@pytest.mark.parametrize("key", PINNED_KEYS)
def test_pinned_key_is_fleet_any_case(key):
    assert classify(api_key_id=key)[0] == "fleet"
    assert classify(api_key_id=key.upper())[0] == "fleet"


def _install(db, *, ip, key_id=None):
    skill = Skill(id=uuid.uuid4(), slug=f"s-{uuid.uuid4().hex[:8]}", title="t", is_public=True)
    db.add(skill)
    db.flush()
    ev = InstallEvent(
        id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=ip, api_key_id=key_id
    )
    db.add(ev)
    db.flush()
    return ev


def _key(db, key_id):
    user = User(id=uuid.uuid4(), email=f"u-{uuid.uuid4().hex[:6]}@example.test", display_name="u")
    db.add(user)
    db.flush()
    key = APIKey(id=uuid.UUID(key_id), user_id=user.id, key_prefix="rec_agent_x", key_hash=uuid.uuid4().hex)
    db.add(key)
    db.flush()
    return key


def test_classify_install_honours_pinned_key_from_a_stranger_ip(db_session):
    key = _key(db_session, PINNED_KEYS[0])
    ev = _install(db_session, ip="203.0.113.9", key_id=key.id)
    assert _classify_install(ev, "203.0.113.9")[0] == "fleet"


def test_classify_install_unpinned_key_keeps_ip_rules(db_session):
    key = _key(db_session, str(uuid.uuid4()))
    ev = _install(db_session, ip="203.0.113.9", key_id=key.id)
    assert _classify_install(ev, "203.0.113.9")[0] == "stranger"


def test_reclassify_flips_egress_and_loopback_only(db_session):
    key = _key(db_session, PINNED_KEYS[1])
    cases = {
        _install(db_session, ip="195.128.172.73"): "fleet",
        _install(db_session, ip="::1"): "fleet",
        _install(db_session, ip="203.0.113.9", key_id=key.id): "fleet",
        _install(db_session, ip=HISTORICAL_STRANGERS[0]): "stranger",
    }
    for ev in cases:
        record_event(
            db_session,
            stage="installed",
            entity_id=resolve_entity(db_session, "ip", ev.client_ip),
            source_system="loopskill-api",
            source_event_id=str(ev.id),
            source_loop="funnel-backfill",
            host="t",
            classification="stranger",
            classification_evidence="no fleet-exclusion match",
        )
    db_session.flush()

    assert reclassify_hosting_installs(db_session, dry_run=False).written == 3
    for ev, expected in cases.items():
        row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
        assert row.classification == expected
