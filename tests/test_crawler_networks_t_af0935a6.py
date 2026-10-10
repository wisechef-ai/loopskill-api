"""t_af0935a6: verified crawler IPs (published ranges + Baidu FCrDNS /32s) are not strangers.

30 of 38 ``installed:stranger`` rows on 2026-10-10 were anonymous crawler
installs. An IP-only subject at a verified crawler address classifies
``unknown``; a subject with an email or key never does. The shipped
config/crawler_networks.txt is pinned against the real prod IPs below.
"""

from __future__ import annotations

import socket
import time
import uuid
from pathlib import Path

import pytest

from app.models import FunnelEvent, InstallEvent, Skill
from app.services import crawler_networks, hosting_networks
from app.services.funnel_backfill import _classify_install, backfill_installed, reclassify_hosting_installs
from app.services.funnel_ledger import classify, clear_fleet_exclusions_cache, record_event, resolve_entity
from scripts import refresh_crawler_networks as refresh


# Real prod IPs (installed:stranger, 2026-10-10).
BAIDU_VERIFIED = "116.179.37.103"  # PTR baiduspider-116-179-37-103.crawl.baidu.com, forward-confirmed
BAIDU_NEIGHBOUR = "116.179.37.1"  # same /24, never verified: China Unicom ISP space
BAIDU_NO_PTR = "116.179.33.141"  # 116.179.33.x had no PTR on 2026-10-10
META_UNPUBLISHED = ["57.141.20.6", "57.141.20.26", "57.141.20.69"]  # Meta publishes no crawler ranges
CHINA_MOBILE = ["39.175.60.28", "111.31.120.244", "120.201.109.139"]  # no PTR
HISTORICAL_STRANGERS = ["176.111.123.46", "79.77.179.182", "155.4.12.121", "85.11.167.136"]
# Inside a published autonomous-crawler list in the shipped file.
GPTBOT_IP = "132.196.86.1"  # 132.196.86.0/24, https://openai.com/gptbot.json
BINGBOT_IP = "157.55.39.10"  # 157.55.39.0/24, https://www.bing.com/toolbox/bingbot.json
NON_CRAWLER_IP = "203.0.113.9"  # TEST-NET-3


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_fleet_exclusions_cache()
    hosting_networks.clear_cache()
    crawler_networks.clear_cache()
    yield
    clear_fleet_exclusions_cache()
    hosting_networks.clear_cache()
    crawler_networks.clear_cache()


# ── classify() ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("ip", "vendor"), [(GPTBOT_IP, "OpenAI-GPTBot"), (BINGBOT_IP, "Bing")])
def test_published_range_ip_without_key_is_unknown(ip, vendor):
    # Bing's range also sits in Microsoft hosting space; whichever rule names
    # it first, the row must not be a stranger.
    classification, evidence = classify(ip=ip)
    assert classification == "unknown"
    assert crawler_networks.crawler_network(ip)[0] == vendor


def test_published_crawler_evidence_names_vendor_and_source(tmp_path: Path):
    data = tmp_path / "c.txt"
    data.write_text("198.51.100.0/24 OpenAI-GPTBot:https://openai.com/gptbot.json\n", encoding="utf-8")
    assert crawler_networks.crawler_network("198.51.100.7", path=data) == (
        "OpenAI-GPTBot",
        "https://openai.com/gptbot.json",
    )


def test_baidu_verified_slash32_is_unknown_with_fcrdns_evidence():
    classification, evidence = classify(ip=BAIDU_VERIFIED)
    assert classification == "unknown"
    assert evidence.startswith(
        f"ip:{BAIDU_VERIFIED} is verified crawler (Baidu, fcrdns:baiduspider-116-179-37-103"
    )
    assert evidence.endswith("no email/key")


def test_baidu_slash24_neighbour_is_still_stranger():
    assert crawler_networks.crawler_network(BAIDU_NEIGHBOUR) is None
    assert classify(ip=BAIDU_NEIGHBOUR)[0] == "stranger"


@pytest.mark.parametrize("ip", [BAIDU_VERIFIED, GPTBOT_IP])
def test_crawler_ip_with_email_or_key_is_never_downgraded(ip):
    assert classify(ip=ip, email="someone@example.org")[0] == "stranger"
    if hosting_networks.hosting_network(ip) is None:
        assert classify(ip=ip, api_key_id=str(uuid.uuid4()))[0] == "stranger"


@pytest.mark.parametrize("ip", HISTORICAL_STRANGERS + CHINA_MOBILE + META_UNPUBLISHED + [BAIDU_NO_PTR])
def test_shipped_file_leaves_real_and_unverified_strangers_alone(ip):
    assert crawler_networks.crawler_network(ip) is None
    assert classify(ip=ip)[0] == "stranger"


def test_shipped_file_has_no_block_wider_than_slash32_outside_published_lists():
    text = crawler_networks.DATA_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cidr, label = line.split(" ", 1)
        if ":fcrdns:" in label:
            assert cidr.endswith(("/32", "/128")), line
        else:
            assert label.split(":", 1)[1].startswith("https://"), line


def test_ipv4_mapped_ipv6_matches():
    assert crawler_networks.crawler_network(f"::ffff:{BAIDU_VERIFIED}")[0] == "Baidu"


def test_missing_file_degrades_to_not_a_crawler(tmp_path: Path, monkeypatch):
    assert crawler_networks.crawler_network(BAIDU_VERIFIED, path=tmp_path / "nope.txt") is None
    monkeypatch.setattr(crawler_networks, "DATA_PATH", tmp_path / "nope.txt")
    crawler_networks.clear_cache()
    assert classify(ip=BAIDU_VERIFIED) == ("stranger", "no fleet-exclusion match")


def test_corrupt_lines_are_skipped(tmp_path: Path):
    data = tmp_path / "c.txt"
    data.write_bytes(
        b"garbage line\n999.1.1.1/32 X:y\n\n192.0.2.5/32 Baidu:fcrdns:h.crawl.baidu.com:2026-10-10\n"
    )
    assert crawler_networks.crawler_network("192.0.2.5", path=data)[0] == "Baidu"
    assert crawler_networks.crawler_network("192.0.2.6", path=data) is None
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert crawler_networks.crawler_network("192.0.2.5", path=bad) is None
    assert crawler_networks.crawler_network("not-an-ip", path=data) is None
    assert crawler_networks.crawler_network(None, path=data) is None


# ── installs: keyed rows are anchored, reclassify flips only crawler rows ──


def _install(db, ip, *, key=None):
    skill = Skill(id=uuid.uuid4(), slug=f"s-{uuid.uuid4().hex[:8]}", title="t", is_public=True)
    db.add(skill)
    db.flush()
    ev = InstallEvent(id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug, client_ip=ip, api_key_id=key)
    db.add(ev)
    db.flush()
    return ev


def test_keyed_install_from_crawler_ip_stays_stranger(db_session):
    ev = _install(db_session, BAIDU_VERIFIED)
    ev.api_key_id = uuid.uuid4()  # unpinned key, no FK row needed for the pure classifier
    assert _classify_install(ev, BAIDU_VERIFIED)[0] == "stranger"
    ev.api_key_id = None
    assert _classify_install(ev, BAIDU_VERIFIED)[0] == "unknown"


def test_backfill_ingest_classifies_crawler_install_unknown(db_session):
    ev = _install(db_session, BAIDU_VERIFIED)
    backfill_installed(db_session, host="t", dry_run=False)
    row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
    assert row.classification == "unknown"
    assert "verified crawler (Baidu" in row.classification_evidence


def test_reclassify_moves_only_verified_crawler_rows(db_session):
    expected = {
        BAIDU_VERIFIED: "unknown",
        BAIDU_NEIGHBOUR: "stranger",
        BAIDU_NO_PTR: "stranger",
        META_UNPUBLISHED[0]: "stranger",
        CHINA_MOBILE[0]: "stranger",
        **{ip: "stranger" for ip in HISTORICAL_STRANGERS},
    }
    events = {ip: _install(db_session, ip) for ip in expected}
    for ev in events.values():
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
    assert (dry.written, dry.skipped) == (1, len(expected) - 1)
    live = reclassify_hosting_installs(db_session, dry_run=False)
    assert live.written == 1
    for ip, ev in events.items():
        row = db_session.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()
        assert row.classification == expected[ip], ip
    assert reclassify_hosting_installs(db_session, dry_run=False).written == 0


# ── refresh script: FCrDNS admission ────────────────────────────────────


def _ptr(mapping):
    def lookup(ip):
        if ip not in mapping:
            raise socket.herror(1, "Unknown host")
        return mapping[ip]

    return lookup


def _fwd(mapping):
    def lookup(host):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return set(mapping[host])

    return lookup


IP = "116.179.37.103"
HOST = "baiduspider-116-179-37-103.crawl.baidu.com"


def test_fcrdns_admits_ptr_under_baidu_that_resolves_back():
    v = refresh.fcrdns_verify(IP, ptr_lookup=_ptr({IP: HOST + "."}), forward_lookup=_fwd({HOST: [IP]}))
    assert v.admitted and v.vendor == "Baidu" and v.ptr == HOST
    assert refresh.fcrdns_line(v, "2026-10-10") == f"{IP}/32 Baidu:fcrdns:{HOST}:2026-10-10"


@pytest.mark.parametrize(
    ("ptr", "fwd", "reason"),
    [
        ({}, {}, "no PTR"),
        ({IP: "176-111-123-46.net.cybernetwmw.pl"}, {}, "PTR not under a known crawler domain"),
        ({IP: "crawl.notbaidu.com"}, {"crawl.notbaidu.com": [IP]}, "PTR not under a known crawler domain"),
        (
            {IP: "baidu.com.evil.example"},
            {"baidu.com.evil.example": [IP]},
            "PTR not under a known crawler domain",
        ),
        ({IP: HOST}, {HOST: ["116.179.37.104"]}, "forward lookup does not return the IP"),
        ({IP: HOST}, {}, "forward lookup failed"),
    ],
)
def test_fcrdns_rejects_ptr_or_forward_mismatch(ptr, fwd, reason):
    v = refresh.fcrdns_verify(IP, ptr_lookup=_ptr(ptr), forward_lookup=_fwd(fwd))
    assert not v.admitted
    assert v.reason.startswith(reason)


def test_fcrdns_timeout_is_not_admitted():
    def slow_ptr(_ip):
        time.sleep(2)
        return HOST

    v = refresh.fcrdns_verify(IP, ptr_lookup=slow_ptr, forward_lookup=_fwd({HOST: [IP]}), timeout=0.05)
    assert not v.admitted and v.reason == "PTR lookup timed out"

    def slow_fwd(_host):
        time.sleep(2)
        return {IP}

    v = refresh.fcrdns_verify(IP, ptr_lookup=_ptr({IP: HOST}), forward_lookup=slow_fwd, timeout=0.05)
    assert not v.admitted and v.reason == "forward lookup timed out"


def test_refresh_keeps_fcrdns_lines_and_fails_closed_on_fetch_error(tmp_path: Path, monkeypatch):
    out = tmp_path / "crawler_networks.txt"
    out.write_text(f"# old\n{IP}/32 Baidu:fcrdns:{HOST}:2026-10-10\n", encoding="utf-8")

    def boom(_url):
        raise OSError("network down")

    monkeypatch.setattr(refresh, "published_lines", lambda: boom(""))
    assert refresh.main(["--out", str(out)]) == 2
    assert f"{IP}/32" in out.read_text(encoding="utf-8")

    monkeypatch.setattr(
        refresh, "published_lines", lambda: ["198.51.100.0/24 OpenAI-GPTBot:https://openai.com/gptbot.json"]
    )
    monkeypatch.setattr(
        refresh,
        "fcrdns_verify",
        lambda ip: refresh.Verdict(ip, False, reason="no PTR (herror)"),
    )
    assert refresh.main(["--out", str(out), "--ips", "203.0.113.9", IP, "198.51.100.4"]) == 0
    body = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert body == [
        "198.51.100.0/24 OpenAI-GPTBot:https://openai.com/gptbot.json",
        f"{IP}/32 Baidu:fcrdns:{HOST}:2026-10-10",
    ]


def test_published_lines_parses_vendor_json_and_skips_nothing():
    payload = {
        "prefixes": [
            {"ipv4Prefix": "198.51.100.0/25"},
            {"ipv4Prefix": "198.51.100.128/25"},
            {"ipv6Prefix": "2001:db8::/48"},
        ]
    }
    lines = refresh.published_lines(fetch=lambda _url: payload)
    first_vendor, first_url = refresh.PUBLISHED_SOURCES[0]
    assert f"198.51.100.0/24 {first_vendor}:{first_url}" in lines
    assert f"2001:db8::/48 {first_vendor}:{first_url}" in lines
    with pytest.raises(ValueError):
        refresh.published_lines(fetch=lambda _url: {"prefixes": []})
