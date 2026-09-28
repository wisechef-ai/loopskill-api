"""paywall_0925 — the paywall-pressure + repeat-use figures on /api/admin/pulse.

Kept out of ``admin_routes`` (600-line module discipline) and pure-read so the
pulse test can drive it directly.

* ``paywall_hits_7d_by_gate`` — per gate: total refusals (sum of hit_count),
  distinct subjects, and distinct STRANGER subjects over the last 7 days. The
  stranger column is the moneypath G12 signal: did a non-fleet human ever
  reach a paywall? Fleet/unknown are shown in the totals, never hidden.
* ``repeat_sync_users_30d`` — human users (``is_agent = false``) with syncs on
  >= 2 distinct UTC days in the last 30 days.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session


class PaywallGateOut(BaseModel):
    gate: str
    hits: int  # sum of hit_count — every refusal, repeats included
    subjects: int  # distinct subjects (user > api key > ip) refused
    stranger_subjects: int  # ...of which classified 'stranger' at write time


def paywall_hits_by_gate(db: Session, *, since: date) -> list[PaywallGateOut]:
    from app.models import PaywallHit

    stranger_subject = case((PaywallHit.classification == "stranger", PaywallHit.subject_key), else_=None)
    rows = (
        db.query(
            PaywallHit.gate,
            func.coalesce(func.sum(PaywallHit.hit_count), 0),
            func.count(func.distinct(PaywallHit.subject_key)),
            func.count(func.distinct(stranger_subject)),
        )
        .filter(PaywallHit.day >= since)
        .group_by(PaywallHit.gate)
        .order_by(PaywallHit.gate)
        .all()
    )
    return [
        PaywallGateOut(gate=g, hits=int(h), subjects=int(s), stranger_subjects=int(st))
        for g, h, s, st in rows
    ]


def repeat_sync_users(db: Session, *, today: date, window_days: int = 30) -> int:
    from app.models import User, UserSyncDay

    since = today - timedelta(days=window_days - 1)
    per_user = (
        db.query(UserSyncDay.user_id)
        .join(User, User.id == UserSyncDay.user_id)
        .filter(UserSyncDay.day >= since, User.is_agent.is_(False))
        .group_by(UserSyncDay.user_id)
        .having(func.count(UserSyncDay.day) >= 2)
        .subquery()
    )
    return int(db.query(func.count()).select_from(per_user).scalar() or 0)
