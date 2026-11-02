"""t_c4e8d5a1: daily crawler refresh + SCOPED reclassify of installed:stranger rows.

New Baidu spider IPs arrive daily and land as ``stranger`` (exact /32s only).
``scripts/crawler_daily.py`` refreshes ``crawler_networks.txt`` and flips ONLY
IP-only stranger installs from the last 7 days whose IP was admitted in the run
or is already in the file. Network is stubbed; nothing here touches DNS.
"""

from __future__ import annotations

import hashlib
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models import APIKey, FunnelEvent, FunnelIdentifier, InstallEvent, Skill, User
from app.services import crawler_networks, hosting_networks
from app.services.funnel_backfill import backfill_installed
from app.services.funnel_ledger import clear_fleet_exclusions_cache
from scripts import crawler_daily as daily
from scripts import refresh_crawler_networks as refresh

NOW = datetime(2026, 11, 3, 3, 5, 0, tzinfo=UTC)
NEW_BAIDU = "192.0.2.10"  # TEST-NET-1 stands in for a never-seen Baidu spider IP
NEW_BAIDU_2 = "192.0.2.11"
KNOWN_BAIDU = "192.0.2.50"  # already in the file
PLAIN = "203.0.113.9"  # a non-crawler stranger (TEST-NET-3)
PTR = {
    NEW_BAIDU: "baiduspider-192-0-2-10.crawl.baidu.com",
    NEW_BAIDU_2: "baiduspider-192-0-2-11.crawl.baidu.com",
}
FILE_BODY = (
    "# header\n# --- published ---\n198.51.100.0/24 Test:https://example.test/r.json\n# --- fcrdns ---\n"
)
KNOWN_LINE = f"{KNOWN_BAIDU}/32 Baidu:fcrdns:baiduspider-192-0-2-50.crawl.baidu.com:2026-10-01\n"


@pytest.fixture(autouse=True)
def _caches():
    for fn in (clear_fleet_exclusions_cache, hosting_networks.clear_cache, crawler_networks.clear_cache):
        fn()
    yield
    for fn in (clear_fleet_exclusions_cache, hosting_networks.clear_cache, crawler_networks.clear_cache):
        fn()


@pytest.fixture()
def net(monkeypatch, tmp_path):
    """Stub the network: published ranges fixed, FCrDNS from PTR; calls recorded."""
    calls: list[str] = []

    def verify(ip, **_k):
        calls.append(ip)
        if ip in PTR:
            return refresh.Verdict(ip, True, "Baidu", PTR[ip], "FCrDNS confirmed")
        return refresh.Verdict(ip, False, reason="no PTR (stub)")

    monkeypatch.setattr(
        refresh, "published_lines", lambda *a, **k: ["198.51.100.0/24 Test:https://example.test/r.json"]
    )
    monkeypatch.setattr(refresh, "fcrdns_verify", verify)
    out = tmp_path / "crawler_networks.txt"
    out.write_text(FILE_BODY, encoding="utf-8")
    # In prod the job's --out IS the file classify() reads; keep the tests faithful to that.
    monkeypatch.setattr(crawler_networks, "DATA_PATH", out)
    return out, calls


def _key(db):
    user = User(id=uuid.uuid4(), display_name="k")
    db.add(user)
    db.flush()
    key = APIKey(id=uuid.uuid4(), user_id=user.id, key_prefix="lsk_test", key_hash=uuid.uuid4().hex)
    db.add(key)
    db.flush()
    return key.id


def _install(db, ip, *, key=None, age_days=0, slug=None):
    skill = Skill(id=uuid.uuid4(), slug=slug or f"s-{uuid.uuid4().hex[:8]}", title="t", is_public=True)
    db.add(skill)
    db.flush()
    ev = InstallEvent(
        id=uuid.uuid4(),
        skill_id=skill.id,
        skill_slug=skill.slug,
        client_ip=ip,
        api_key_id=key,
        created_at=(NOW - timedelta(days=age_days)).replace(tzinfo=None),
    )
    db.add(ev)
    db.flush()
    return ev


def _row(db, ev):
    return db.query(FunnelEvent).filter(FunnelEvent.source_event_id == str(ev.id)).one()


def _seed(db, *events):
    """Write the ledger the way the 03:15 backfill does, under the CURRENT (empty-of-test-IPs) file."""
    crawler_networks.clear_cache()
    backfill_installed(db, host="t", dry_run=False)
    db.commit()
    for ev in events:
        assert _row(db, ev).classification == "stranger"


def _run(db, out, tmp_path, *, live=True, **kw):
    return daily.run(db, out=out, backup_dir=tmp_path / "bk", live=live, now=NOW, **kw)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ── acceptance: scoped flips ────────────────────────────────────────────


def test_new_crawler_ip_is_admitted_with_evidence_and_flipped(db_session, net, tmp_path):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    plain = _install(db_session, PLAIN)
    _seed(db_session, ev, plain)

    rep = _run(db_session, out, tmp_path)

    assert [v["ip"] for v in rep.admitted] == [NEW_BAIDU]
    assert rep.truncated == 0 and rep.file_written is True
    assert "crawler file: written" in daily.format_report(rep)
    assert rep.admitted[0]["ptr"] == PTR[NEW_BAIDU]  # FCrDNS evidence is in the report
    assert [f["ip"] for f in rep.flips] == [NEW_BAIDU]
    row = _row(db_session, ev)
    assert row.classification == "unknown"
    assert "verified crawler (Baidu, fcrdns:" in row.classification_evidence
    assert "was: no fleet-exclusion match" in row.classification_evidence
    today = datetime.now(UTC).date().isoformat()  # refresh_crawler_networks stamps the real date
    line = f"{NEW_BAIDU}/32 Baidu:fcrdns:{PTR[NEW_BAIDU]}:{today}"
    assert line in out.read_text()
    assert f"fcrdns:{PTR[NEW_BAIDU]}:{today}" in row.classification_evidence  # DB evidence == file label
    # the non-crawler stranger is untouched
    assert _row(db_session, plain).classification == "stranger"


def test_non_crawler_ip_only_stranger_is_never_flipped(db_session, net, tmp_path):
    out, calls = net
    plain = _install(db_session, PLAIN)
    _seed(db_session, plain)
    before = _row(db_session, plain).classification_evidence

    rep = _run(db_session, out, tmp_path)

    assert calls == [PLAIN]  # it was checked...
    assert rep.admitted == [] and rep.flips == []  # ...and rejected
    row = _row(db_session, plain)
    assert (row.classification, row.classification_evidence) == ("stranger", before)
    assert not (tmp_path / "bk").exists()


def test_keyed_install_from_crawler_ip_is_never_flipped(db_session, net, tmp_path):
    out, calls = net
    out.write_text(FILE_BODY + KNOWN_LINE, encoding="utf-8")  # IP is IN the file
    keyed = _install(db_session, KNOWN_BAIDU, key=_key(db_session))
    crawler_networks.clear_cache()
    # A keyed install is classified stranger by _classify_install (crawler rule off for keys).
    backfill_installed(db_session, host="t", dry_run=False)
    db_session.commit()
    assert _row(db_session, keyed).classification == "stranger"

    rep = _run(db_session, out, tmp_path)

    assert rep.checked == [] and calls == []  # keyed rows are not even candidates
    assert rep.flips == []
    assert _row(db_session, keyed).classification == "stranger"


def test_keyed_and_ip_only_share_ip_only_the_ip_only_row_flips(db_session, net, tmp_path):
    out, _ = net
    keyed = _install(db_session, NEW_BAIDU, key=_key(db_session))
    bare = _install(db_session, NEW_BAIDU)
    _seed(db_session, keyed, bare)

    rep = _run(db_session, out, tmp_path)

    assert [f["source_event_id"] for f in rep.flips] == [str(bare.id)]
    assert _row(db_session, keyed).classification == "stranger"
    assert _row(db_session, bare).classification == "unknown"


def test_entity_anchored_to_an_email_is_never_flipped(db_session, net, tmp_path):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    db_session.add(
        FunnelIdentifier(
            id=uuid.uuid4(), entity_id=_row(db_session, ev).entity_id, kind="email", value="a@b.test"
        )
    )
    db_session.commit()

    rep = _run(db_session, out, tmp_path)

    assert [v["ip"] for v in rep.admitted] == [NEW_BAIDU]  # IP is genuinely a crawler...
    assert rep.flips == []  # ...but the subject has an email, so it stays
    assert _row(db_session, ev).classification == "stranger"


def test_run_with_no_new_ips_is_a_noop(db_session, net, tmp_path):
    out, calls = net
    out.write_text(FILE_BODY + KNOWN_LINE, encoding="utf-8")
    # No in-window stranger candidates: a NULL-IP install (unknown) and a stranger older than 7 days.
    unknown = _install(db_session, None)
    stale = _install(db_session, PLAIN, age_days=30)
    crawler_networks.clear_cache()
    backfill_installed(db_session, host="t", dry_run=False)
    db_session.commit()
    assert (_row(db_session, unknown).classification, _row(db_session, stale).classification) == (
        "unknown",
        "stranger",
    )
    file_sha = _sha(out)
    snapshot = [(r.id, r.classification, r.classification_evidence) for r in db_session.query(FunnelEvent)]

    rep = _run(db_session, out, tmp_path)

    assert (rep.checked, rep.admitted, rep.flips, rep.backup) == ([], [], [], None)
    assert calls == [] and _sha(out) == file_sha and not (tmp_path / "bk").exists()
    assert "crawler file: unchanged" in daily.format_report(rep)
    assert snapshot == [
        (r.id, r.classification, r.classification_evidence) for r in db_session.query(FunnelEvent)
    ]


def test_second_run_is_idempotent(db_session, net, tmp_path):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    first = _run(db_session, out, tmp_path)
    assert len(first.flips) == 1
    file_sha = _sha(out)

    second = _run(db_session, out, tmp_path)

    assert second.flips == [] and second.backup is None and _sha(out) == file_sha
    assert len(list((tmp_path / "bk").glob("*.json"))) == 1


def test_ip_already_in_file_flips_without_a_dns_check(db_session, net, tmp_path):
    out, calls = net
    ev = _install(db_session, KNOWN_BAIDU)
    _seed(db_session, ev)  # stranger: file did not have it at ingest time
    out.write_text(FILE_BODY + KNOWN_LINE, encoding="utf-8")

    rep = _run(db_session, out, tmp_path)

    assert KNOWN_BAIDU not in calls  # "already listed" -> no new FCrDNS lookup needed
    assert [f["ip"] for f in rep.flips] == [KNOWN_BAIDU]
    assert (
        "fcrdns:baiduspider-192-0-2-50.crawl.baidu.com:2026-10-01"
        in _row(db_session, ev).classification_evidence
    )


def test_window_is_seven_days(db_session, net, tmp_path):
    out, calls = net
    fresh = _install(db_session, NEW_BAIDU, age_days=6)
    old = _install(db_session, NEW_BAIDU_2, age_days=8)
    _seed(db_session, fresh, old)

    rep = _run(db_session, out, tmp_path)

    assert calls == [NEW_BAIDU] and [f["source_event_id"] for f in rep.flips] == [str(fresh.id)]
    assert _row(db_session, old).classification == "stranger"


def test_garbage_and_null_client_ip_never_crash_or_flip(db_session, net, tmp_path):
    out, calls = net
    for bad in ("not-an-ip", "", None, "999.1.1.1"):
        _install(db_session, bad)
    db_session.commit()
    backfill_installed(db_session, host="t", dry_run=False)
    db_session.commit()

    rep = _run(db_session, out, tmp_path)

    assert calls == [] and rep.flips == [] and rep.refresh_rc == 0


def test_probe_and_bench_installs_are_not_candidates(db_session, net, tmp_path):
    out, calls = net
    bench = _install(db_session, NEW_BAIDU, slug="coldstart-bench-abc")
    probe = _install(db_session, NEW_BAIDU_2)
    probe.is_probe = True
    db_session.commit()
    backfill_installed(db_session, host="t", dry_run=False)
    db_session.commit()
    assert _row(db_session, bench).classification == "fleet"

    rep = _run(db_session, out, tmp_path)

    assert calls == [] and rep.flips == []
    assert _row(db_session, bench).classification == "fleet"


def test_max_ips_caps_dns_checks_and_prefers_the_newest_ips(db_session, net, tmp_path):
    out, calls = net
    older, newer = _install(db_session, NEW_BAIDU_2, age_days=3), _install(db_session, NEW_BAIDU, age_days=1)
    _seed(db_session, older, newer)

    rep = _run(db_session, out, tmp_path, max_ips=1)

    assert calls == [NEW_BAIDU] and rep.truncated == 1  # newest first: a cap never starves today's IPs
    assert _row(db_session, older).classification == "stranger"  # picked up by a later run


def test_admitted_ip_without_a_ledger_row_yet_is_reported_and_classified_by_the_next_backfill(
    db_session, net, tmp_path, monkeypatch
):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)  # installed after the last backfill: no funnel row yet

    rep = _run(db_session, out, tmp_path)

    assert [v["ip"] for v in rep.admitted] == [NEW_BAIDU] and rep.flips == []
    assert "admitted IPs with no row flipped: 1" in daily.format_report(rep)
    # backfill reads the shipped path; point it at the job's file to emulate the 03:15 run that follows
    monkeypatch.setattr(crawler_networks, "DATA_PATH", out)
    crawler_networks.clear_cache()
    backfill_installed(db_session, host="t", dry_run=False)
    assert _row(db_session, ev).classification == "unknown"


# ── dry-run / reversibility ─────────────────────────────────────────────


def test_dry_run_reports_the_flips_but_writes_nothing(db_session, net, tmp_path):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    file_sha = _sha(out)

    rep = _run(db_session, out, tmp_path, live=False)

    assert [f["ip"] for f in rep.flips] == [NEW_BAIDU] and [v["ip"] for v in rep.admitted] == [NEW_BAIDU]
    assert _sha(out) == file_sha and not (tmp_path / "bk").exists()
    assert _row(db_session, ev).classification == "stranger"
    text = daily.format_report(rep)
    assert "crawler file: would change" in text
    assert "DRY-RUN" in text and "would flip: 1" in text and PTR[NEW_BAIDU] in text and NEW_BAIDU in text


def test_backup_holds_pre_change_state_and_is_written_before_the_update(
    db_session, net, tmp_path, monkeypatch
):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    pre = _row(db_session, ev)
    pre_id, pre_evidence = str(pre.id), pre.classification_evidence
    seen = {}
    real = daily.write_backup

    def spy(backup_dir, flips, **kw):
        path = real(backup_dir, flips, **kw)
        seen["db_at_backup_time"] = _row(db_session, ev).classification  # update has NOT happened yet
        return path

    monkeypatch.setattr(daily, "write_backup", spy)

    rep = _run(db_session, out, tmp_path)

    assert seen["db_at_backup_time"] == "stranger"
    doc = json.loads(Path(rep.backup).read_text())
    assert doc["task"] == "t_c4e8d5a1"
    assert [(r["id"], r["classification"], r["classification_evidence"]) for r in doc["rows"]] == [
        (pre_id, "stranger", pre_evidence)
    ]
    assert Path(rep.backup).name == "crawler_reclassify_20261103T030500Z.json"
    assert oct(Path(rep.backup).stat().st_mode & 0o777) == "0o600"


def test_failed_backup_means_no_flip(db_session, net, tmp_path, monkeypatch):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(daily, "write_backup", boom)
    with pytest.raises(OSError):
        _run(db_session, out, tmp_path)
    assert _row(db_session, ev).classification == "stranger"


def test_backup_never_overwrites_an_existing_file(tmp_path):
    flips = [{"id": str(uuid.uuid4())}]
    daily.write_backup(tmp_path, flips, days=7, now=NOW)
    with pytest.raises(FileExistsError):
        daily.write_backup(tmp_path, flips, days=7, now=NOW)


def test_restore_undoes_the_flip_and_skips_rows_changed_since(db_session, net, tmp_path):
    out, _ = net
    a, b = _install(db_session, NEW_BAIDU), _install(db_session, NEW_BAIDU_2)
    _seed(db_session, a, b)
    rep = _run(db_session, out, tmp_path)
    assert len(rep.flips) == 2
    _row(db_session, b).classification = "fleet"  # someone re-classified b after our flip
    db_session.commit()

    dry = daily.restore(db_session, Path(rep.backup), live=False)
    assert [f["source_event_id"] for f in dry] == [str(a.id)]
    assert _row(db_session, a).classification == "unknown"  # dry-run changed nothing

    daily.restore(db_session, Path(rep.backup), live=True)
    ra = _row(db_session, a)
    assert (ra.classification, ra.classification_evidence) == ("stranger", "no fleet-exclusion match")
    assert _row(db_session, b).classification == "fleet"


# ── refresh failure ─────────────────────────────────────────────────────


def test_refresh_failure_leaves_file_untouched_and_still_flips_known_ips(
    db_session, net, monkeypatch, tmp_path
):
    out, _ = net
    known = _install(db_session, KNOWN_BAIDU)
    new = _install(db_session, NEW_BAIDU)
    _seed(db_session, known, new)
    out.write_text(FILE_BODY + KNOWN_LINE, encoding="utf-8")
    file_sha = _sha(out)

    def down(*a, **k):
        raise OSError("vendor list unreachable")

    monkeypatch.setattr(refresh, "published_lines", down)
    rep = _run(db_session, out, tmp_path)

    assert rep.refresh_rc == 2 and rep.file_written is False and _sha(out) == file_sha
    assert [f["ip"] for f in rep.flips] == [KNOWN_BAIDU]  # already-in-file only
    assert _row(db_session, new).classification == "stranger"
    assert "refresh FAILED rc=2" in daily.format_report(rep)


# ── CLI ─────────────────────────────────────────────────────────────────


def test_self_test_passes_and_never_touches_the_real_db_or_file(monkeypatch):
    import app.database as database

    shipped = _sha(Path(daily.ROOT) / "config" / "crawler_networks.txt")

    def forbidden(*a, **k):
        raise AssertionError("self-test opened a real DB session")

    monkeypatch.setattr(database, "SessionLocal", forbidden)
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite:///./test_dev.db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    real = (refresh.published_lines, refresh.fcrdns_verify)

    assert daily.main(["--self-test"]) == 0

    assert (refresh.published_lines, refresh.fcrdns_verify) == real  # stubs restored
    assert _sha(Path(daily.ROOT) / "config" / "crawler_networks.txt") == shipped


@pytest.mark.parametrize("argv", [["--days", "0"], ["--days", "91"], ["--max-ips", "0"]])
def test_out_of_range_args_are_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        daily.main(argv)
    assert exc.value.code == 2


def test_cli_defaults_to_dry_run(db_session, net, tmp_path, monkeypatch, capsys):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    file_sha = _sha(out)

    rc = daily.main(["--out", str(out), "--backup-dir", str(tmp_path / "bk")])

    assert rc == 0 and "DRY-RUN" in capsys.readouterr().out
    assert _sha(out) == file_sha and _row(db_session, ev).classification == "stranger"


def test_backup_dir_default_is_under_home_backups():
    assert daily.DEFAULT_BACKUP_DIR == Path.home() / "backups" / "crawler-reclassify"


def test_socket_is_never_used(db_session, net, tmp_path, monkeypatch):
    """The stubs must fully cover the network: a real lookup would be a test bug."""

    def no_dns(*a, **k):
        raise AssertionError("real DNS lookup")

    monkeypatch.setattr(socket, "gethostbyaddr", no_dns)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    assert len(_run(db_session, out, tmp_path).flips) == 1


def test_fleet_ip_is_not_a_candidate_even_if_a_crawler_file_lists_it(db_session, net, tmp_path):
    """The job only re-judges strangers; fleet/hosting stay with their own rules."""
    out, calls = net
    fleet_ip = "195.128.172.73"  # adam-xps egress, pinned in config/fleet_exclusions.yaml
    ev = _install(db_session, fleet_ip)
    crawler_networks.clear_cache()
    backfill_installed(db_session, host="t", dry_run=False)
    db_session.commit()
    assert _row(db_session, ev).classification == "fleet"
    out.write_text(FILE_BODY + f"{fleet_ip}/32 Baidu:fcrdns:x.crawl.baidu.com:2026-10-01\n", encoding="utf-8")

    rep = _run(db_session, out, tmp_path)

    assert calls == [] and rep.flips == []
    assert _row(db_session, ev).classification == "fleet"


# ── CLI paths ───────────────────────────────────────────────────────────


def _cli(monkeypatch, db_session):
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)


def test_cli_live_then_restore_roundtrip(db_session, net, tmp_path, monkeypatch, capsys):
    out, _ = net
    ev = _install(db_session, NEW_BAIDU)
    _seed(db_session, ev)
    _cli(monkeypatch, db_session)
    bk = tmp_path / "bk"

    assert daily.main(["--live", "--out", str(out), "--backup-dir", str(bk)]) == 0
    printed = capsys.readouterr().out
    assert (
        "LIVE" in printed
        and "flipped: 1" in printed
        and f"FLIP {_row(db_session, ev).id} {NEW_BAIDU}" in printed
    )
    assert _row(db_session, ev).classification == "unknown"
    (backup,) = bk.glob("*.json")

    assert daily.main(["--restore", str(backup)]) == 0  # dry-run: reports, changes nothing
    assert "DRY-RUN: 1 row(s) restorable" in capsys.readouterr().out
    assert _row(db_session, ev).classification == "unknown"

    assert daily.main(["--restore", str(backup), "--live"]) == 0
    assert "LIVE: 1 row(s) restored" in capsys.readouterr().out
    assert _row(db_session, ev).classification == "stranger"


def test_cli_exits_2_when_the_refresh_failed(db_session, net, tmp_path, monkeypatch, capsys):
    out, _ = net
    _cli(monkeypatch, db_session)

    def down(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(refresh, "published_lines", down)
    assert daily.main(["--out", str(out), "--backup-dir", str(tmp_path / "bk")]) == 2
    captured = capsys.readouterr()
    assert "refresh failed (rc=2)" in captured.err and "refresh FAILED" in captured.out


def test_self_test_fails_loudly_when_the_job_misbehaves(monkeypatch, capsys):
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite:///./test_dev.db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    real_run = daily.run

    def lazy(db, **kw):  # a job that never flips anything, and "flips" on a re-run
        rep = real_run(db, **{**kw, "live": False})
        rep.backup = "x"
        return rep

    monkeypatch.setattr(daily, "run", lazy)
    assert daily.main(["--self-test"]) == 1
    err = capsys.readouterr().err
    assert "SELF-TEST FAIL: live flip wrong" in err and "second run was not a no-op" in err


def test_self_test_flags_a_dry_run_that_plans_the_wrong_rows(monkeypatch, capsys):
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite:///./test_dev.db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    monkeypatch.setattr(daily, "run", lambda db, **kw: daily.Report(live=kw["live"], days=7))
    assert daily.main(["--self-test"]) == 1
    assert "SELF-TEST FAIL: dry-run wrote something or planned the wrong rows" in capsys.readouterr().err


# ── boundaries found by mutation testing ────────────────────────────────


def test_install_exactly_seven_days_old_is_in_the_window(db_session, net, tmp_path):
    out, calls = net
    edge = _install(db_session, NEW_BAIDU, age_days=7)
    _seed(db_session, edge)

    rep = _run(db_session, out, tmp_path)

    assert calls == [NEW_BAIDU] and len(rep.flips) == 1


@pytest.mark.parametrize("kind", ["probe", "bench"])
def test_probe_and_bench_ips_are_never_dns_checked_even_before_backfill(db_session, net, tmp_path, kind):
    """No ledger row yet (backfill has not run): the install itself must keep its IP out of the candidates."""
    out, calls = net
    ev = _install(db_session, NEW_BAIDU, slug="coldstart-bench-xyz" if kind == "bench" else None)
    ev.is_probe = kind == "probe"
    db_session.commit()

    rep = _run(db_session, out, tmp_path)

    assert calls == [] and rep.checked == [] and rep.admitted == []


def test_known_crawler_ip_in_the_live_file_still_flips_a_stranger_row(db_session, net, tmp_path):
    """classify() reads the same file the job writes; a stranger row must still be judged by the OTHER rules."""
    out, _ = net
    ev = _install(db_session, KNOWN_BAIDU)
    _seed(db_session, ev)
    out.write_text(FILE_BODY + KNOWN_LINE, encoding="utf-8")
    crawler_networks.clear_cache()
    assert crawler_networks.crawler_network(KNOWN_BAIDU) is not None  # classify() would now say unknown

    assert len(_run(db_session, out, tmp_path).flips) == 1


def test_backup_creates_missing_parent_directories(tmp_path):
    path = daily.write_backup(tmp_path / "a" / "b", [{"id": "x"}], days=7, now=NOW)
    assert path.exists() and json.loads(path.read_text())["rows"] == [{"id": "x"}]


def test_backup_file_is_valid_pretty_json_with_the_window(tmp_path):
    path = daily.write_backup(tmp_path, [{"id": "x"}], days=5, now=NOW)
    doc = json.loads(path.read_text())
    assert (doc["window_days"], doc["created_at"]) == (5, NOW.isoformat())
    assert "\n  " in path.read_text()  # human-readable: this file is the rollback record


@pytest.mark.parametrize("argv", [["--days", "1"], ["--days", "90"], ["--max-ips", "1"]])
def test_in_range_boundary_args_are_accepted(db_session, tmp_path, monkeypatch, argv):
    _cli(monkeypatch, db_session)
    seen = {}

    def fake_run(db, **kw):
        seen.update(kw)
        return daily.Report(live=False, days=kw["days"])

    monkeypatch.setattr(daily, "run", fake_run)
    assert daily.main([*argv, "--backup-dir", str(tmp_path)]) == 0
    assert seen["days"] == (int(argv[1]) if argv[0] == "--days" else daily.DEFAULT_DAYS)


@pytest.mark.parametrize("mutation", ["file_touched", "backup_dir_created", "wrong_flip_count"])
def test_self_test_catches_each_dry_run_violation(monkeypatch, capsys, mutation):
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite:///./test_dev.db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    real_run = daily.run

    def bad(db, **kw):
        rep = real_run(db, **kw)
        if kw["live"]:
            return rep
        if mutation == "file_touched":
            kw["out"].write_text(kw["out"].read_text() + "# touched\n")
        elif mutation == "backup_dir_created":
            kw["backup_dir"].mkdir(parents=True)
        else:
            rep.flips = []
        return rep

    monkeypatch.setattr(daily, "run", bad)
    assert daily.main(["--self-test"]) == 1
    assert "SELF-TEST FAIL: dry-run wrote something" in capsys.readouterr().err


@pytest.mark.parametrize("second", ["flips", "backup"])
def test_self_test_catches_a_second_run_that_is_not_a_noop(monkeypatch, capsys, second):
    monkeypatch.setenv("WR_DATABASE_URL", "sqlite:///./test_dev.db")
    monkeypatch.setenv("WR_COOKIES_SECURE", "false")
    real_run = daily.run
    calls = {"live": 0}

    def bad(db, **kw):
        rep = real_run(db, **kw)
        if kw["live"]:
            calls["live"] += 1
            if calls["live"] == 2:  # the re-run
                if second == "flips":
                    rep.flips = [{"id": "x"}]
                else:
                    rep.backup = "x"
        return rep

    monkeypatch.setattr(daily, "run", bad)
    assert daily.main(["--self-test"]) == 1
    assert "second run was not a no-op" in capsys.readouterr().err


def test_dry_run_with_nothing_admitted_says_file_unchanged(db_session, net, tmp_path):
    out, _ = net
    plain = _install(db_session, PLAIN)
    _seed(db_session, plain)

    text = daily.format_report(_run(db_session, out, tmp_path, live=False))

    assert "IPs admitted: 0; crawler file: unchanged" in text
