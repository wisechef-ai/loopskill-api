"""Install-integrity: the ONE organic-vs-internal predicate shared everywhere.

CHEF-2026-08-23-A (t_4a38fed9). The original B3/§4.2 filter
(``coalesce(APIKey.is_test, false) = false``) missed two classes of
self-traffic that were being counted as organic installs:

1. **CI self-installs** — the deploy pipeline's self-hosted runner executes
   on the production host itself, so its installs arrive with
   ``client_ip`` = the server's own public IPv4 and ``api_key_id IS NULL``
   (anonymous → coalesce treated them as organic). 118 of 432 installs in
   the 2026-08-16→2026-08-23 window.
2. **Agent-probe installs** — self-serve agent registration
   (``POST /api/agents/register``) mints a shadow ``User(is_agent=True)``
   + ``APIKey(name='agent:<name>')`` with no human behind it. Those keys
   default to ``is_test=false`` (correct — they are not test keys), so
   keyed installs from them were counted as organic. All 3 external
   "new users" in the window were automated probes, not humans.

The shared predicate below is the single definition of "organic install"
used by every read surface (/api/stats, search cards, cookbook cards,
leaderboards, creator dashboards) AND the denormalised
``Skill.install_count`` counter bump, so the counter and the event-derived
counts can never disagree about what counts as organic:

    organic(event) ≡ NOT key.is_test
                     AND NOT key-owner-user.is_agent
                     AND client_ip ∉ {SERVER_PUBLIC_IP} ∪ KNOWN_INTERNAL_IPS

Fail-closed posture (pitfall #24 discipline): the internal-IP set is
boot-gated — a non-sqlite deployment that has not configured
``WR_SERVER_PUBLIC_IP`` refuses to boot rather than silently counting its
own CI traffic as organic. ``client_ip`` NULL (unparseable / legacy rows)
is treated as organic — matching the anonymous-organic convention — but
the IP set itself can never be silently empty in production.
"""

from __future__ import annotations

from sqlalchemy import func, or_, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import APIKey, InstallEvent, User


def internal_network_ips() -> set[str]:
    """Return the internal/synthetic IP set (server's own IP + known dogfood IPs).

    Reads ``app.config.settings`` lazily so importing this module never
    constructs Settings at import time.
    """
    from app.config import settings

    ips: set[str] = set()
    if settings.SERVER_PUBLIC_IP:
        ips.add(settings.SERVER_PUBLIC_IP)
    for ip in settings.KNOWN_INTERNAL_IPS:
        ip = ip.strip()
        if ip:
            ips.add(ip)
    return ips


def _internal_ip_filter() -> ColumnElement:
    """Read-time SQL predicate: the event's client_ip is NOT internal.

    NULL/empty ``client_ip`` passes (treated as organic, per the anonymous
    convention). Non-IP sentinel strings (``cookbook:<uuid>`` fleet-apply
    rows) also pass — they carry no network origin to filter on.
    """
    internal_ips = sorted(internal_network_ips())
    if not internal_ips:
        # Empty set (dev/sqlite) — nothing to exclude.
        return true()
    return or_(
        InstallEvent.client_ip.is_(None),
        ~InstallEvent.client_ip.in_(internal_ips),
    )


def organic_install_predicate() -> list[ColumnElement]:
    """The shared organic predicate as a list of SQLAlchemy filters.

    Join contract: the caller MUST outer-join APIKey on
    ``APIKey.id == InstallEvent.api_key_id`` and User on
    ``User.id == APIKey.user_id``. Both joins are on the nullable
    ``api_key_id`` path, so anonymous installs keep every joined column
    NULL and remain organic.
    """
    return [
        func.coalesce(APIKey.is_test, False).is_(False),
        func.coalesce(User.is_agent, False).is_(False),
        _internal_ip_filter(),
    ]


def install_is_organic(
    db: Session,
    *,
    api_key_id: str | None,
    client_ip: str | None,
) -> bool:
    """Point check for the write path: is THIS install organic?

    Used by the InstallEvent writers when deciding whether to bump the
    denormalised ``Skill.install_count`` counter — the same definition the
    read surfaces aggregate over, so counter and event counts can't drift
    apart by filtering differently.
    """
    if client_ip and client_ip in internal_network_ips():
        return False
    if api_key_id is None:
        return True
    row = (
        db.query(APIKey.is_test, User.is_agent)
        .outerjoin(User, User.id == APIKey.user_id)
        .filter(APIKey.id == api_key_id)
        .one_or_none()
    )
    if row is None:
        # Key row vanished (shouldn't happen — FK), treat as organic to
        # match the coalesce convention on the read path.
        return True
    is_test, is_agent = row
    return not (is_test or is_agent)


def recompute_organic_skill_installs(db: Session) -> dict[str, int]:
    """Recompute organic install counts per skill from InstallEvent rows.

    One grouped query; used by the one-time counter re-sync migration and
    available to the drift probe as its truth source so probe and counter
    share the exact same organic definition.
    """
    rows = (
        db.query(InstallEvent.skill_slug, func.count(InstallEvent.id).label("installs"))
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .outerjoin(User, User.id == APIKey.user_id)
        .filter(*organic_install_predicate())
        .group_by(InstallEvent.skill_slug)
        .all()
    )
    return {slug: int(c or 0) for slug, c in rows if slug}
