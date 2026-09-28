"""pricing0928 (t_7f5808d2, option E): stranger-measurement fixes.

E1  hosting-network IPs classify ``unknown``, not ``stranger``
E2  coldstart-bench / probe installs are ``fleet`` (and stamped is_probe)
E3  MCP bundle installs and the REST external install keep the caller's key
E5  a subscription PaymentIntent is never also counted as one-time revenue,
    on either Stripe API shape; existing duplicate rows can be pruned
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from app.auth_ctx import AuthContext
from app.models import APIKey, Bundle, BundleSkill, FunnelEvent, InstallEvent, Skill, SkillVersion, User
from app.services import hosting_networks
from app.services.funnel_backfill import (
    _stripe_paid_source_ids,
    backfill_installed,
    backfill_paid,
    prune_invoice_backed_onetime,
    reclassify_hosting_installs,
)
from app.services.funnel_ledger import classify, clear_fleet_exclusions_cache, record_event, resolve_entity
from app.services.probe_detection import is_bench_slug, is_probe_request

# Real addresses inside the snapshot, one per provider named in the finding.
AWS_IP = "3.5.140.2"  # 3.5.140.0/22, AS16509
MICROSOFT_IP = "20.42.65.92"  # 20.42.64.0/19-ish, AS8075
HETZNER_IP = "5.9.10.10"  # 5.9.0.0/16, AS24940
NON_HOSTING_IP = "203.0.113.9"  # TEST-NET-3


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_fleet_exclusions_cache()
    hosting_networks.clear_cache()
    yield
    clear_fleet_exclusions_cache()
    hosting_networks.clear_cache()


# ── E1 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ip", "provider"), [(AWS_IP, "AWS"), (MICROSOFT_IP, "Microsoft"), (HETZNER_IP, "Hetzner")]
)
def test_hosting_ip_alone_is_unknown_with_named_evidence(ip, provider):
    assert hosting_networks.hosting_network(ip) == provider
    classification, evidence = classify(ip=ip)
    assert classification == "unknown"
    assert provider in evidence


def test_non_hosting_ip_is_still_stranger():
    assert hosting_networks.hosting_network(NON_HOSTING_IP) is None
    assert classify(ip=NON_HOSTING_IP)[0] == "stranger"


def test_hosting_ip_with_an_email_is_still_stranger():
    """A person who signed up from a cloud box is still a person."""
    assert classify(ip=AWS_IP, email="someone-new@example.org")[0] == "stranger"


def test_fleet_ip_still_wins_over_hosting():
    assert classify(ip="77.42.92.141")[0] == "fleet"  # Hetzner, but listed fleet


def test_ipv6_and_garbage_do_not_raise():
    assert hosting_networks.hosting_network("2a01:4f8::1") == "Hetzner"
    assert hosting_networks.hosting_network("not-an-ip") is None
    assert hosting_networks.hosting_network("") is None


def test_missing_data_file_degrades_to_not_hosting(tmp_path: Path):
    assert hosting_networks.hosting_network(AWS_IP, path=tmp_path / "nope.txt") is None


def test_backfill_classifies_hosting_install_unknown(db_session):
    skill = Skill(id=uuid.uuid4(), slug=f"s-{uuid.uuid4().hex[:8]}", title="t", is_public=True)
    db_session.add(skill)
    db_session.flush()
    ev = InstallEvent(id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=AWS_IP)
    db_session.add(ev)
    db_session.flush()

    backfill_installed(db_session, host="t", dry_run=False)
    row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
    assert row.classification == "unknown"


# ── E2 ──────────────────────────────────────────────────────────────────


def test_bench_slug_detection():
    assert is_bench_slug("coldstart-bench-20260908-b8c56156")
    assert not is_bench_slug("super-memory")
    assert not is_bench_slug(None)


def test_bench_install_is_probe_even_from_a_stranger_ip(db_session):
    assert is_probe_request(db_session, client_ip=AWS_IP, skill_slug="coldstart-bench-x1") is True
    assert is_probe_request(db_session, client_ip=AWS_IP, skill_slug="super-memory") is False


def test_backfill_pins_bench_install_as_fleet(db_session):
    skill = Skill(id=uuid.uuid4(), slug="coldstart-bench-20260908-b8c56156", title="t", is_public=True)
    db_session.add(skill)
    db_session.flush()
    # Recorded before the is_probe rule existed: is_probe False, stranger IP.
    ev = InstallEvent(
        id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=NON_HOSTING_IP, is_probe=False
    )
    db_session.add(ev)
    db_session.flush()

    backfill_installed(db_session, host="t", dry_run=False)
    row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
    assert row.classification == "fleet"


def test_reclassify_rewrites_only_rows_whose_answer_changed(db_session):
    skill = Skill(id=uuid.uuid4(), slug=f"s-{uuid.uuid4().hex[:8]}", title="t", is_public=True)
    bench = Skill(id=uuid.uuid4(), slug="coldstart-bench-r1", title="t", is_public=True)
    db_session.add_all([skill, bench])
    db_session.flush()
    cases = {
        "aws": (
            InstallEvent(id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=AWS_IP),
            "unknown",
        ),
        "bench": (
            InstallEvent(id=uuid.uuid4(), skill_id=bench.id, skill_slug=bench.slug, client_ip=NON_HOSTING_IP),
            "fleet",
        ),
        "human": (
            InstallEvent(id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=NON_HOSTING_IP),
            "stranger",
        ),
    }
    for ev, _ in cases.values():
        db_session.add(ev)
    db_session.flush()
    # Seed the ledger the way the OLD rule wrote it: all three as stranger.
    for ev, _ in cases.values():
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

    dry = reclassify_hosting_installs(db_session, dry_run=True)
    assert (dry.written, dry.skipped) == (2, 1)
    assert db_session.query(FunnelEvent).filter(FunnelEvent.classification == "stranger").count() == 3

    live = reclassify_hosting_installs(db_session, dry_run=False)
    assert live.written == 2
    for ev, expected in cases.values():
        row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
        assert row.classification == expected
    assert reclassify_hosting_installs(db_session, dry_run=False).written == 0  # idempotent


# ── E3 ──────────────────────────────────────────────────────────────────


def _user_key_bundle(db):
    user = User(id=uuid.uuid4(), display_name="u", email=f"{uuid.uuid4().hex[:8]}@example.org")
    db.add(user)
    db.flush()
    raw = "rec_" + uuid.uuid4().hex
    key = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix=raw[:8],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        is_active=True,
    )
    skill = Skill(id=uuid.uuid4(), slug=f"b-{uuid.uuid4().hex[:8]}", title="t", tier="free", is_public=True)
    db.add_all([key, skill])
    db.flush()
    db.add(
        SkillVersion(
            id=uuid.uuid4(), skill_id=skill.id, semver="1.0.0", checksum_sha256="x", tarball_size_bytes=1
        )
    )
    bundle = Bundle(id=uuid.uuid4(), name="b", bundle_owner=user.id)
    db.add(bundle)
    db.flush()
    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="manual"))
    db.flush()
    ctx = AuthContext(scope="user", user_id=user.id, api_key_id=key.id, tier="free")
    return ctx, bundle, skill, key


@pytest.mark.parametrize("single", [True, False])
def test_mcp_bundle_install_records_the_callers_key(db_session, single):
    from app.mcp.tools.bundle_install import loopskill_bundle_install

    ctx, bundle, skill, key = _user_key_bundle(db_session)
    kwargs = {"cookbook_id": str(bundle.id)}
    if single:
        kwargs["slug"] = skill.slug
    loopskill_bundle_install(db=db_session, ctx=ctx, **kwargs)

    ev = db_session.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).one()
    assert ev.api_key_id == key.id, "MCP bundle install dropped the caller's api_key_id"


# ── E5 ──────────────────────────────────────────────────────────────────

# Shapes as returned by Stripe API 2026-08-26.dahlia (verified against the live
# account 2026-09-28): no top-level pi.invoice; the link is in
# pi.payment_details.order_reference and invoice.payments.
DAHLIA_INVOICE = {
    "id": "in_dahlia_1",
    "amount_paid": 100,
    "currency": "usd",
    "customer": "cus_Z",
    "created": 1790000000,
    "payments": {"data": [{"payment": {"type": "payment_intent", "payment_intent": "pi_dahlia_1"}}]},
}
DAHLIA_PI = {
    "id": "pi_dahlia_1",
    "amount": 100,
    "status": "succeeded",
    "customer": "cus_Z",
    "payment_details": {"customer_reference": None, "order_reference": "in_dahlia_1"},
}


@pytest.mark.parametrize(
    "pi",
    [
        DAHLIA_PI,
        {**DAHLIA_PI, "payment_details": None},  # linked only via invoice.payments
        {
            **DAHLIA_PI,
            "id": "pi_other",
            "payment_details": {"order_reference": "in_elsewhere"},
        },  # only order_ref
    ],
)
def test_subscription_charge_is_not_also_onetime(pi):
    merged = _stripe_paid_source_ids(invoices=[DAHLIA_INVOICE], payment_intents=[pi])
    assert [(sid, system) for sid, _obj, system in merged] == [("in_dahlia_1", "stripe")]


def test_genuine_one_time_charge_still_counts():
    one_time = {"id": "pi_founding", "amount": 4900, "status": "succeeded", "payment_details": None}
    merged = _stripe_paid_source_ids(invoices=[DAHLIA_INVOICE], payment_intents=[DAHLIA_PI, one_time])
    assert {(sid, system) for sid, _o, system in merged} == {
        ("in_dahlia_1", "stripe"),
        ("pi_founding", "stripe-onetime"),
    }


def test_prune_removes_existing_duplicate_onetime_rows(db_session):
    backfill_paid(db_session, host="t", invoices=[DAHLIA_INVOICE], payment_intents=[], dry_run=False)
    # The pre-fix double count, as it sits in prod today.
    record_event(
        db_session,
        stage="paid",
        entity_id=resolve_entity(db_session, "stripe_customer", "cus_Z"),
        source_system="stripe-onetime",
        source_event_id="pi_dahlia_1",
        source_loop="funnel-backfill",
        host="t",
        classification="unknown",
        amount_cents=100,
    )
    db_session.flush()
    paid = db_session.query(FunnelEvent).filter(FunnelEvent.stage == "paid")
    assert paid.count() == 2

    assert (
        prune_invoice_backed_onetime(
            db_session, invoices=[DAHLIA_INVOICE], payment_intents=[DAHLIA_PI], dry_run=True
        ).written
        == 1
    )
    assert paid.count() == 2  # dry run wrote nothing

    prune_invoice_backed_onetime(
        db_session, invoices=[DAHLIA_INVOICE], payment_intents=[DAHLIA_PI], dry_run=False
    )
    assert [(r.source_system, r.source_event_id) for r in paid.all()] == [("stripe", "in_dahlia_1")]

    # And a re-run of the paid backfill does not bring it back.
    backfill_paid(db_session, host="t", invoices=[DAHLIA_INVOICE], payment_intents=[DAHLIA_PI], dry_run=False)
    assert paid.count() == 1
