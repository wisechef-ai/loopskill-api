#!/usr/bin/env python3
"""scripts/crawler_daily.py — daily crawler-network refresh + SCOPED funnel reclassify.

t_c4e8d5a1. ``config/crawler_networks.txt`` admits Baidu's spider only as exact
FCrDNS-verified /32s, so a Baidu IP we have not met before lands in the funnel
ledger as ``installed:stranger`` and stays there: the 03:15 ``funnel_backfill``
cron never runs ``--reclassify-installs``. This job closes that loop, once a day:

1. candidates = distinct client IPs of IP-only (no api key) installs from the
   last ``--days`` days that today's rules still call ``stranger``;
2. ``scripts/refresh_crawler_networks.py`` (unchanged admission rules: published
   vendor ranges; Baidu only by forward-confirmed reverse DNS, exact /32; never
   an ISP block) regenerates the file and FCrDNS-checks those candidates;
3. scoped reclassify: an ``installed:stranger`` ledger row flips to ``unknown``
   ONLY when all of these hold:
     * its install is IP-only, inside the window, and not a probe/bench/fleet/
       hosting case (those have their own rules);
     * its funnel entity has no email / api_key / user / stripe identifier;
     * its IP was admitted in this run OR is already in the crawler file.
   Nothing else is touched. Hosting/fleet re-classification stays the job of
   ``funnel_backfill.py --reclassify-installs``.

Reversible: before any UPDATE the pre-change ``(id, classification, evidence)``
of every row to flip is written to a dated JSON file under ``--backup-dir``;
``--restore FILE`` puts them back (only rows still at the value this job set).

Dry-run is the default: pass ``--live`` to write the file and the ledger.
Run it BEFORE ``funnel_backfill`` (03:05 vs 03:15): rows that backfill inserts
afterwards for an admitted IP are classified ``unknown`` at insert time.

Scheduling (wisechef-hq crontab, ahead of the 03:15 funnel_backfill; flock keeps a slow run from overlapping itself):
  5 3 * * * cd /home/wisechef/loopskill-api && set -a && . ./.env && set +a && flock -n /tmp/loopskill-crawler-daily.lock venv/bin/python scripts/crawler_daily.py --live --host wisechef-hq >> /home/wisechef/logs/crawler_daily.log 2>&1

NOTE: --live rewrites config/crawler_networks.txt in the checkout it runs from. That file is
git-tracked, so the working tree goes dirty; commit the new FCrDNS lines (data PR, as in #429)
before a deploy that also changes that file, or the pull will refuse.

Usage:
  python3 scripts/crawler_daily.py                 # dry-run, last 7 days
  python3 scripts/crawler_daily.py --live          # refresh file + flip rows
  python3 scripts/crawler_daily.py --restore ~/backups/crawler-reclassify/<file>.json [--live]
  python3 scripts/crawler_daily.py --self-test     # in-memory, exits before any real mutation

Output: a human report on stdout (IPs checked, admitted with FCrDNS evidence,
rows flipped, backup path); diagnostics on stderr.

Exit codes:
  0  ok (dry-run or live, including "nothing to do")
  1  fatal (bad DB url, backup could not be written, self-test failed)
  2  the vendor-range refresh failed; the file was left untouched and only rows
     already covered by the existing file were considered
"""

from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TASK = "t_c4e8d5a1"
DEFAULT_BACKUP_DIR = Path.home() / "backups" / "crawler-reclassify"
DEFAULT_DAYS = 7
DEFAULT_MAX_IPS = 300  # bounds DNS time per run (2 lookups x 5s worst case each); newest IPs first
# An entity anchored to any of these is a person/account: never downgraded.
ANCHOR_KINDS = ("email", "api_key", "user_id", "stripe_customer", "handle")


@dataclass
class Report:
    live: bool
    days: int
    checked: list[str] = field(default_factory=list)
    truncated: int = 0
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    refresh_rc: int = 0
    file_written: bool = False
    rows_in_scope: int = 0
    flips: list[dict[str, Any]] = field(default_factory=list)
    backup: str | None = None

    @property
    def admitted(self) -> list[dict[str, Any]]:
        return [v for v in self.verdicts if v.get("admitted")]


# ── selection ────────────────────────────────────────────────────────────


def _valid_ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def _in_scope_rows(db, *, since: datetime):
    """(FunnelEvent, InstallEvent, ip) for IP-only installs in the window that today's
    non-crawler rules still call stranger. Ledger rows are optional (outer join in Python)."""
    from sqlalchemy import select

    from app.models import FunnelEvent, InstallEvent
    from app.services.funnel_backfill import SOURCE_SYSTEM_APP
    from app.services.funnel_ledger import classify
    from app.services.probe_detection import is_bench_slug

    installs = (
        db.execute(
            select(InstallEvent).where(InstallEvent.api_key_id.is_(None), InstallEvent.created_at >= since)
        )
        .scalars()
        .all()
    )
    ledger = {
        ev.source_event_id: ev
        for ev in db.execute(
            select(FunnelEvent).where(
                FunnelEvent.stage == "installed", FunnelEvent.source_system == SOURCE_SYSTEM_APP
            )
        ).scalars()
    }
    out = []
    for inst in installs:
        ip = _valid_ip(inst.client_ip)
        if ip is None or inst.is_probe or is_bench_slug(inst.skill_slug):
            continue
        # allow_crawler_downgrade=False: judge by every OTHER rule, so the answer does
        # not depend on whether the crawler file has already been updated.
        if classify(ip=ip, allow_crawler_downgrade=False)[0] != "stranger":
            continue
        row = ledger.get(str(inst.id))
        if row is not None and row.classification != "stranger":
            continue
        out.append((row, inst, ip))
    return out


def _ips_newest_first(in_scope) -> list[str]:
    """Distinct IPs, most recently seen first, so a cap never starves today's new IPs."""
    latest: dict[str, datetime] = {}
    for _row, inst, ip in in_scope:
        seen = inst.created_at or datetime.min
        if ip not in latest or seen > latest[ip]:
            latest[ip] = seen
    return sorted(latest, key=lambda ip: (latest[ip], ip), reverse=True)


def _is_anchored(db, entity_id) -> bool:
    from sqlalchemy import select

    from app.models import FunnelIdentifier

    return (
        db.execute(
            select(FunnelIdentifier.id)
            .where(FunnelIdentifier.entity_id == entity_id, FunnelIdentifier.kind.in_(ANCHOR_KINDS))
            .limit(1)
        ).first()
        is not None
    )


# ── refresh (reuses scripts/refresh_crawler_networks.py unchanged) ───────


def _refresh(ips: list[str], out: Path, *, live: bool) -> tuple[int, list[dict[str, Any]], bool]:
    """Run the refresh script on *ips*. Returns (rc, verdict dicts, file_written)."""
    from scripts import refresh_crawler_networks as refresh

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(ips) + "\n")
        ips_file = Path(fh.name)
    argv = ["--ips-file", str(ips_file), "--out", str(out), "--json"]
    if not live:
        argv.append("--dry-run")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = refresh.main(argv)
    finally:
        ips_file.unlink(missing_ok=True)
    if rc != 0:
        return rc, [], False
    payload = json.loads(buf.getvalue())
    return 0, payload["verdicts"], bool(payload["written"])


# ── scoped reclassify ────────────────────────────────────────────────────


def _crawler_label(ip: str, admitted: dict[str, tuple[str, str]], out: Path) -> tuple[str, str] | None:
    from app.services.crawler_networks import crawler_network

    # The file is the source of truth (live runs just wrote it); `admitted` only
    # supplies the label in a dry-run, where the file was deliberately not written.
    return crawler_network(ip, path=out) or admitted.get(ip)


def _new_evidence(ip: str, label: tuple[str, str], old: str | None) -> str:
    vendor, source = label
    return f"ip:{ip} is verified crawler ({vendor}, {source}), no email/key [reclassified {TASK}; was: {old}]"


def plan_flips(
    db, *, since: datetime, admitted: dict[str, tuple[str, str]], out: Path
) -> tuple[int, list[dict]]:
    in_scope = [t for t in _in_scope_rows(db, since=since) if t[0] is not None]
    flips = []
    for row, _inst, ip in in_scope:
        label = _crawler_label(ip, admitted, out)
        if label is None or _is_anchored(db, row.entity_id):
            continue
        flips.append(
            {
                "id": str(row.id),
                "source_event_id": row.source_event_id,
                "ip": ip,
                "classification": row.classification,
                "classification_evidence": row.classification_evidence,
                "new_classification": "unknown",
                "new_classification_evidence": _new_evidence(ip, label, row.classification_evidence),
            }
        )
    return len(in_scope), flips


def write_backup(backup_dir: Path, flips: list[dict], *, days: int, now: datetime) -> Path:
    """Durably write the pre-change state. Exclusive create: never overwrites a backup."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"crawler_reclassify_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    doc = {"task": TASK, "created_at": now.isoformat(), "window_days": days, "rows": flips}
    with open(path, "x", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(doc, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def apply_flips(db, flips: list[dict]) -> None:
    from sqlalchemy import update

    from app.models import FunnelEvent

    for f in flips:
        # Guarded UPDATE: only a row that is still the stranger we planned against.
        db.execute(
            update(FunnelEvent)
            .where(FunnelEvent.id == UUID(f["id"]), FunnelEvent.classification == f["classification"])
            .values(
                classification=f["new_classification"],
                classification_evidence=f["new_classification_evidence"],
            )
        )
    db.commit()


def restore(db, backup_file: Path, *, live: bool) -> list[dict]:
    """Put back the rows named in *backup_file* that are still at the value this job set."""
    from app.models import FunnelEvent

    doc = json.loads(backup_file.read_text(encoding="utf-8"))
    done = []
    for f in doc["rows"]:
        row = db.get(FunnelEvent, UUID(f["id"]))
        if (
            row is None
            or row.classification != f["new_classification"]
            or row.classification_evidence != f["new_classification_evidence"]
        ):
            continue
        done.append(f)
        if live:
            row.classification = f["classification"]
            row.classification_evidence = f["classification_evidence"]
    if live:
        db.commit()
    return done


# ── orchestration ────────────────────────────────────────────────────────


def run(
    db,
    *,
    out: Path,
    backup_dir: Path,
    live: bool,
    days: int = DEFAULT_DAYS,
    max_ips: int = DEFAULT_MAX_IPS,
    now: datetime | None = None,
) -> Report:
    from app.services import crawler_networks

    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).replace(tzinfo=None)  # install_events.created_at is naive UTC
    report = Report(live=live, days=days)

    ips = _ips_newest_first(_in_scope_rows(db, since=since))
    report.truncated = max(0, len(ips) - max_ips)
    report.checked = ips[:max_ips]

    report.refresh_rc, report.verdicts, report.file_written = _refresh(report.checked, out, live=live)
    crawler_networks.clear_cache()
    day = now.date().isoformat()
    admitted = {v["ip"]: (v["vendor"], f"fcrdns:{v['ptr']}:{day}") for v in report.admitted}

    report.rows_in_scope, flips = plan_flips(db, since=since, admitted=admitted, out=out)
    if live and flips:
        path = write_backup(backup_dir, flips, days=days, now=now)  # BEFORE the update
        report.backup = str(path)
        apply_flips(db, flips)
    report.flips = flips
    return report


def format_report(r: Report) -> str:
    lines = [f"crawler_daily — {'LIVE' if r.live else 'DRY-RUN (no writes)'} — window={r.days}d"]
    lines.append(
        f"IPs checked: {len(r.checked)}"
        + (f" (+{r.truncated} over the cap, not checked)" if r.truncated else "")
    )
    for v in r.verdicts:
        tag = "ADMIT " if v["admitted"] else "reject"
        lines.append(f"  {tag} {v['ip']:<40} {v.get('ptr') or '-':<50} {v['reason']}")
    lines.append(
        f"IPs admitted: {len(r.admitted)}; crawler file: "
        + ("written" if r.file_written else ("would change" if r.admitted and not r.live else "unchanged"))
        + (f"; refresh FAILED rc={r.refresh_rc}" if r.refresh_rc else "")
    )
    flipped_ips = {f["ip"] for f in r.flips}
    unflipped = [v["ip"] for v in r.admitted if v["ip"] not in flipped_ips]
    if unflipped:
        lines.append(
            f"admitted IPs with no row flipped: {len(unflipped)} (not in the ledger yet: the next "
            f"funnel_backfill classifies them unknown; or anchored to an email/key)"
        )
    verb = "flipped" if r.live else "would flip"
    lines.append(f"stranger install rows in scope: {r.rows_in_scope}; {verb}: {len(r.flips)}")
    for f in r.flips:
        lines.append(f"  FLIP {f['id']} {f['ip']} {f['classification']}->{f['new_classification']}")
    if r.backup:
        lines.append(f"backup: {r.backup}")
    return "\n".join(lines)


# ── self-test ────────────────────────────────────────────────────────────


def self_test() -> int:
    """In-memory SQLite + stubbed network. Never touches the real DB, file or DNS."""
    # Force (not setdefault) BEFORE the first app import: even an inherited prod
    # WR_DATABASE_URL must not be reachable from here.
    os.environ["WR_DATABASE_URL"] = "sqlite://"
    os.environ["WR_COOKIES_SECURE"] = "false"
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models import Base
    from app.models import FunnelEvent, InstallEvent, Skill
    from app.services import crawler_networks
    from app.services.funnel_backfill import backfill_installed
    from scripts import refresh_crawler_networks as refresh

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    tmp = Path(tempfile.mkdtemp(prefix="crawler_daily_selftest_"))
    out = tmp / "crawler_networks.txt"
    out.write_text("# empty\n", encoding="utf-8")
    baidu, other = "192.0.2.77", "203.0.113.9"  # TEST-NET: in no real list

    def install(ip, key=None):
        s = Skill(id=uuid.uuid4(), slug=f"s-{uuid.uuid4().hex[:8]}", title="t")
        db.add(s)
        db.flush()
        db.add(
            InstallEvent(
                id=uuid.uuid4(),
                skill_id=s.id,
                skill_slug=s.slug,
                client_ip=ip,
                api_key_id=key,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db.flush()

    install(baidu)
    install(other)
    install(baidu, key=uuid.uuid4())
    crawler_networks.clear_cache()
    backfill_installed(db, host="selftest", dry_run=False)
    db.commit()

    real = (refresh.published_lines, refresh.fcrdns_verify)
    refresh.published_lines = lambda *a, **k: ["198.51.100.0/24 Test:selftest"]
    refresh.fcrdns_verify = lambda ip, **k: (
        refresh.Verdict(ip, True, "Baidu", "baiduspider-x.crawl.baidu.com", "FCrDNS confirmed")
        if ip == baidu
        else refresh.Verdict(ip, False, reason="no PTR (selftest)")
    )
    problems = []
    try:
        before = out.read_text()
        dry = run(db, out=out, backup_dir=tmp / "bk", live=False)
        if len(dry.flips) != 1 or out.read_text() != before or (tmp / "bk").exists():
            problems.append("dry-run wrote something or planned the wrong rows")
        live = run(db, out=out, backup_dir=tmp / "bk", live=True)
        got = sorted(
            (
                i.client_ip,
                i.api_key_id is not None,
                db.query(FunnelEvent).filter_by(source_event_id=str(i.id)).one().classification,
            )
            for i in db.query(InstallEvent)
        )
        want = sorted([(baidu, False, "unknown"), (other, False, "stranger"), (baidu, True, "stranger")])
        if len(live.flips) != 1 or got != want or not live.backup:
            problems.append(f"live flip wrong: {got}")
        again = run(db, out=out, backup_dir=tmp / "bk", live=True)
        if again.flips or again.backup:
            problems.append("second run was not a no-op")
    finally:
        refresh.published_lines, refresh.fcrdns_verify = real
        crawler_networks.clear_cache()
        db.close()
    for p in problems:
        print(f"SELF-TEST FAIL: {p}", file=sys.stderr)
    print("self-test: " + ("FAIL" if problems else "ok"))
    return 1 if problems else 0


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--live", action="store_true", help="write the crawler file and the ledger (default: dry-run)"
    )
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="install window in days (default 7)")
    ap.add_argument("--max-ips", type=int, default=DEFAULT_MAX_IPS, help="cap on IPs FCrDNS-checked per run")
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument(
        "--out", type=Path, default=ROOT / "config" / "crawler_networks.txt", help=argparse.SUPPRESS
    )
    ap.add_argument("--host", default="", help="accepted for parity with funnel_backfill.py; informational")
    ap.add_argument(
        "--restore", type=Path, metavar="BACKUP.json", help="undo a previous run from its backup file"
    )
    ap.add_argument("--self-test", action="store_true", help="run an in-memory check and exit")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not 1 <= args.days <= 90 or args.max_ips < 1:
        ap.error("--days must be 1..90 and --max-ips >= 1")

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.restore:
            done = restore(db, args.restore, live=args.live)
            print(
                f"restore {'LIVE' if args.live else 'DRY-RUN'}: {len(done)} row(s) {'restored' if args.live else 'restorable'}"
            )
            for f in done:
                print(f"  {f['id']} {f['ip']} {f['new_classification']}->{f['classification']}")
            return 0
        report = run(
            db, out=args.out, backup_dir=args.backup_dir, live=args.live, days=args.days, max_ips=args.max_ips
        )
    finally:
        db.close()
    print(format_report(report))
    if report.refresh_rc:
        print(f"refresh failed (rc={report.refresh_rc}); file untouched", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
