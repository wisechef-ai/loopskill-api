"""Bundle fast-path onboarding hint (task t_8ccbdbc5, 2026-08-23).

Evidence (7d install-event analysis, t_c70553c2): the only repeating
external installer in the window (49% of all external installs, IP
74.7.243.224, 2026-08-22 13:36 UTC) hand-walked 29 sequential direct
skill-installs — every slug a member of the public bundle
``loopskill-essentials`` (53 skills). They were replicating a bundle one
request at a time because nothing in the API response told them one
command does it.

This module detects that behaviour class AFTER a direct install commits
and returns a small advisory payload for the response:

    {"slug": "loopskill-essentials",
     "matched": "12 of 53",
     "install_all": "curl -fsSL https://app.loopskill.io/api/bundles/install.sh | bash -s -- loopskill-essentials"}

Trigger contract (task spec):
    >=3 direct (bundle_id IS NULL) installs from the same client_ip within
    24h where ALL the distinct slugs belong to a single public bundle.

    "All in one bundle" (not ">=3 overlap") is the deliberate strictness:
    the hint must have zero false positives — telling a user who is
    cherry-picking across bundles that "one command installs everything
    you're doing" would be wrong. The acceptance test pins this: installs
    spread across bundles -> no hint.

    When several public bundles contain the whole recent set (a superset
    bundle also qualifies), the SMALLEST bundle wins — the most specific
    fast-path for what the user is actually doing. Deterministic
    tiebreak: smaller total member count, then slug alphabetical.

install_all command note: the task draft suggested
``loopskill bundle install <slug>``, but no such CLI verb exists — the
PyPI ``loopskill`` package (0.1.0/0.2.0) ships import/diff/pull/apply
only, and no CLI surface calls ``GET /api/skills/install`` (verified
2026-08-23, so there is also no response passthrough to surface the hint
through). Shipping a command that dead-ends would burn the one shot at
converting this user class. The verified one-command bulk path is the
auth-free bundle installer script (bundles0811 P1 F1/F2, e2e-verified
2026-08-11 installing all 53 essentials skills with zero credentials):

    curl -fsSL https://app.loopskill.io/api/bundles/install.sh | bash -s -- loopskill-essentials

That is what the hint teaches. If a future CLI release grows a
``loopskill bundle install`` verb, swap the command string here — the
detection logic does not change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, InstallEvent

#: Minimum distinct direct-install slugs from one IP before a hint can fire.
BUNDLE_HINT_MIN_SLUGS = 3

#: Lookback window for the "repeated installs" signal.
BUNDLE_HINT_WINDOW = timedelta(hours=24)

#: Upper bound on recent-event rows examined per request. A pathological
#: or hostile IP cannot make this lookup unbounded; 200 covers the
#: observed worst case (29 sequential installs) 6x over.
_BUNDLE_HINT_MAX_EVENTS = 200


def compute_bundle_hint(
    db: Session,
    *,
    client_ip: str | None,
    skill_id_being_installed: Any = None,
    now: datetime | None = None,
) -> dict | None:
    """Return the bundle fast-path hint payload, or None.

    Called by the /api/skills/install route AFTER the InstallEvent for the
    current request has been committed, so the just-recorded install is
    included in the window — the response that crosses the >=3 threshold
    is itself the response that carries the hint.

    Args:
        client_ip: the recorded InstallEvent.client_ip for THIS install.
        skill_id_being_installed: reserved for callers that compute before
            commit; when the event is already committed (the route path)
            the current install is in the window and this stays None.
        now: injectable clock for tests.

    Cost: one indexed lookup on install_events.client_ip (see the
    bhint0823 migration) + one bundle_skills probe for the recent skill
    ids + one count for the winning candidates. No schema write, no auth
    change, purely additive to the response.
    """
    if not client_ip:
        return None

    now = now or datetime.now(UTC)
    window_start = now - BUNDLE_HINT_WINDOW

    recent_rows = (
        db.query(InstallEvent.skill_id, InstallEvent.skill_slug)
        .filter(
            InstallEvent.client_ip == client_ip,
            InstallEvent.bundle_id.is_(None),  # direct installs only
            InstallEvent.created_at >= window_start,
            InstallEvent.status == "ok",
        )
        .order_by(InstallEvent.created_at.desc())
        .limit(_BUNDLE_HINT_MAX_EVENTS)
        .all()
    )

    # Include a not-yet-committed install (defensive; the route commits first).
    recent_skill_ids: set = {row.skill_id for row in recent_rows if row.skill_id}
    if skill_id_being_installed is not None:
        recent_skill_ids.add(skill_id_being_installed)

    if len(recent_skill_ids) < BUNDLE_HINT_MIN_SLUGS:
        return None

    # Which PUBLIC, slug-addressed bundles contain at least one of these skills?
    membership_rows = (
        db.query(BundleSkill.bundle_id, BundleSkill.skill_id, Bundle.slug)
        .join(Bundle, Bundle.id == BundleSkill.bundle_id)
        .filter(
            BundleSkill.skill_id.in_(recent_skill_ids),
            BundleSkill.source != "disabled",
            Bundle.visibility == "public",
            Bundle.slug.isnot(None),
        )
        .all()
    )

    per_bundle: dict = {}
    slug_by_id: dict = {}
    for row in membership_rows:
        per_bundle.setdefault(row.bundle_id, set()).add(row.skill_id)
        slug_by_id[row.bundle_id] = row.slug

    # Strict ALL-membership: the bundle must contain every distinct recent skill.
    candidates = [bid for bid, ids in per_bundle.items() if ids >= recent_skill_ids]
    if not candidates:
        return None

    # Total (non-disabled) member count per candidate -> most specific bundle wins.
    total_rows = (
        db.query(BundleSkill.bundle_id)
        .filter(
            BundleSkill.bundle_id.in_(candidates),
            BundleSkill.source != "disabled",
        )
        .all()
    )
    totals: dict = {}
    for (bid,) in total_rows:
        totals[bid] = totals.get(bid, 0) + 1

    winner = min(candidates, key=lambda bid: (totals.get(bid, 0), slug_by_id.get(bid, "")))
    total = totals.get(winner, len(recent_skill_ids))
    slug = slug_by_id.get(winner)
    if not slug:  # unreachable (slug.isnot(None) above) — fail quiet, not loud
        return None

    from app import config

    api_base = config.public_origin().rstrip("/")
    return {
        "slug": slug,
        "matched": f"{len(recent_skill_ids)} of {total}",
        # bhint-tel0824 (t_55a1a333): the fetch URL carries ?slug=<slug> as an
        # attribution beacon — the handler ignores unknown query params (the
        # script still receives the slug via bash argv), but the serve path
        # uses it to write a converted_pull telemetry row when the pulling
        # client_ip was hinted this slug within 7d. Without the beacon every
        # hinted pull is slug-less and conversion is unmeasurable.
        "install_all": f"curl -fsSL {api_base}/api/bundles/install.sh?slug={slug} | bash -s -- {slug}",
    }
