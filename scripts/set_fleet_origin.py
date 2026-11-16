"""t_de052498 — the callable path for the synthetic-origin markers.

``app/services/synthetic_runs.py`` says it in its own docstring: **"a marker no
code path can set is not a marker — it is a hard-coded slug list wearing a
column."** It ships :func:`set_member_origin` and :func:`set_fleet_origin` as
the after-creation half of that lifecycle, and nothing called either one. They
were referenced from tests and nowhere else.

The consequence was measured against prod on 2026-11-16, and it is the reason
this script had to exist before any new run row was allowed to land:

    ``fleets.is_synthetic``          NULL on both fleets
    ``fleet_members.is_synthetic``   NULL on all four members
    ``api_keys.is_test``             false on all four member keys

Every identity unclassified. ``origin_verdict_for_member`` therefore returned
``None`` for every run, dropping each one to the ``SELF_ORIGINATED_LOOP_SLUGS``
backstop — so a beacon row reads correctly (its slug is on the list) while
**any non-beacon row from our own three hosts reads as EXTERNAL ADOPTION**. The
one direction the mesh_0408 W4 module exists to protect was the one left open,
because the identity half was never wired and the backstop half was filling in
for it.

Wiring a loop producer without fixing this first would have manufactured
adoption numbers out of our own fleet's telemetry — the exact failure mode
WIS-43/45 were burned for.

Two verdicts, and the precedence order matters
----------------------------------------------
``member`` beats ``fleet`` beats ``APIKey.is_test`` beats the slug backstop,
most specific first (see the module docstring). Which means classifying a fleet
does NOT classify members that already carry their own verdict — and a member
verdict set here outlives any later fleet flip.

DRY RUN BY DEFAULT. ``--execute`` is required to write anything.

Usage::

    python -m scripts.set_fleet_origin                                  # report every identity
    python -m scripts.set_fleet_origin --fleet wise-agents-supervision --synthetic true
    python -m scripts.set_fleet_origin --host mac01 --profile default --synthetic true
    python -m scripts.set_fleet_origin --member <uuid> --synthetic false --execute
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from app.database import SessionLocal
from app.models import Fleet, FleetMember, LoopRun
from app.services.synthetic_runs import set_fleet_origin, set_member_origin


def _split(db, member_ids: list[UUID]) -> tuple[int, int]:
    """(total, synthetic) run rows currently held for these members."""
    if not member_ids:
        return 0, 0
    rows = db.query(LoopRun.is_synthetic).filter(LoopRun.member_id.in_(member_ids)).all()
    total = len(rows)
    synthetic = sum(1 for (flag,) in rows if flag)
    return total, synthetic


def _verdict(value: bool | None) -> str:
    return "unset" if value is None else ("synthetic(ours)" if value else "external")


def _report_identity(db, label: str, before: tuple[int, int], after: tuple[int, int]) -> None:
    """Print one identity and what the write actually did to its history.

    The after-split is not decoration. ``_repair_ingested_runs`` re-stamps rows
    written under the old verdict, and if it silently failed the counts would be
    the only thing that told you.
    """
    delta = before[1] - after[1] if before[1] != after[1] else 0
    note = f"  re-stamped {abs(delta)} run row(s)" if delta else ""
    print(f"  {label}: runs {before[0]} (synthetic {before[1]} → {after[1]}){note}")


def _resolve_fleet(db, ref: str) -> Fleet | None:
    try:
        return db.query(Fleet).filter(Fleet.id == UUID(ref)).first()
    except ValueError:
        return db.query(Fleet).filter(Fleet.name == ref).first()


def _resolve_members(db, member: str | None, host: str | None, profile: str | None) -> list[FleetMember]:
    q = db.query(FleetMember)
    if member:
        return q.filter(FleetMember.id == UUID(member)).all()
    if host:
        q = q.filter(FleetMember.host == host)
        if profile:
            q = q.filter(FleetMember.profile == profile)
        return q.all()
    return []


def _list_all(db) -> int:
    """Print every fleet and member with its current verdict.

    This is the half of the script that answers the question that started the
    card — "is this number ours?" — which, before it existed, could only be
    answered by reading prod SQL by hand.
    """
    print("Fleets")
    for f in db.query(Fleet).order_by(Fleet.created_at).all():
        members = db.query(FleetMember).filter(FleetMember.fleet_id == f.id).all()
        print(f"  [{_verdict(f.is_synthetic):>16}]  {f.name}  ({len(members)} members)")
        for m in members:
            total, synth = _split(db, [m.id])
            print(
                f"      [{_verdict(m.is_synthetic):>16}]  {m.host}/{m.profile}"
                f"  runs={total} synthetic={synth}  id={m.id}"
            )
    unattributed = db.query(LoopRun).filter(LoopRun.member_id.notin_(db.query(FleetMember.id))).count()
    if unattributed:
        print(f"  unattributed run rows (member_id resolves to nothing): {unattributed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify fleet/member loop-run origin (ours vs external)")
    ap.add_argument("--fleet", help="fleet name or uuid (classify a whole fleet)")
    ap.add_argument("--member", help="member uuid")
    ap.add_argument("--host", help="member host, with --profile for one member of that host")
    ap.add_argument("--profile", help="member profile, used with --host")
    ap.add_argument(
        "--synthetic", choices=("true", "false"), help="true = ours (never adoption); false = a customer"
    )
    ap.add_argument("--execute", action="store_true", help="write (default is dry run)")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        if not args.fleet and not args.member and not args.host:
            return _list_all(db)

        if args.synthetic is None:
            print("error: --synthetic true|false is required when naming an identity", file=sys.stderr)
            return 2
        synthetic = args.synthetic == "true"

        if args.fleet and (args.member or args.host):
            print("error: --fleet is exclusive of --member/--host", file=sys.stderr)
            return 2

        if not args.execute:
            print("DRY RUN — nothing will be written. Re-run with --execute.")

        if args.fleet:
            fleet = _resolve_fleet(db, args.fleet)
            if fleet is None:
                print(f"error: no fleet {args.fleet!r}", file=sys.stderr)
                return 1
            member_ids = [m.id for m in db.query(FleetMember).filter(FleetMember.fleet_id == fleet.id).all()]
            before = _split(db, member_ids)
            print(f"fleet {fleet.name!r}: {_verdict(fleet.is_synthetic)} → {_verdict(synthetic)}")
            if args.execute:
                set_fleet_origin(db, fleet, synthetic=synthetic)
                db.commit()
            _report_identity(db, f"fleet {fleet.name}", before, _split(db, member_ids))
            return 0

        members = _resolve_members(db, args.member, args.host, args.profile)
        if not members:
            print("error: no member matched (check --member / --host / --profile)", file=sys.stderr)
            return 1
        for m in members:
            before = _split(db, [m.id])
            print(f"member {m.host}/{m.profile}: {_verdict(m.is_synthetic)} → {_verdict(synthetic)}")
            if args.execute:
                set_member_origin(db, m, synthetic=synthetic)
                db.commit()
            _report_identity(db, f"member {m.host}/{m.profile}", before, _split(db, [m.id]))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
