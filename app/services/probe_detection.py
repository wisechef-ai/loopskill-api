"""coldstart_0609/A — one seam for "is this write a known probe?"

Both ``missing_skill_queries`` and ``install_events`` rows carry an
``is_probe`` flag so downstream demand/install analytics can filter out
the fleet's own dogfooding traffic without hand-maintaining two copies of
the exclusion rule (the same anti-drift lesson as ``app/services/
funnel_ledger.py``'s fleet-exclusions config: ONE place decides who is
fleet/probe traffic, every writer calls it).

A write is a "known probe" when EITHER:
  - the caller's ``x-api-key`` belongs to one of a fixed set of known
    fleet/system user emails (tori@wisechef.ai, system@loopskill.io,
    editorial@wisechef.ai), OR
  - the caller's resolved client IP is one of a fixed set of known probe/
    loopback IPs.

This module does NOT read the fleet_exclusions.yaml config used by
funnel_ledger.py — that file is a *superset* covering broader fleet
identities (more emails/IPs) for a different, older classification
surface. is_probe is a narrower, deliberately-scoped signal for the two
tables listed above; the email/IP lists here are exactly what
coldstart_0609/A specifies. If the lists ever need to grow, do it here —
never re-implement the check inline in a writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

# Known fleet/system accounts whose x-api-key marks a write as a probe.
PROBE_USER_EMAILS: frozenset[str] = frozenset(
    {
        "tori@wisechef.ai",
        "system@loopskill.io",
        "editorial@wisechef.ai",
    }
)

# Known probe / loopback source IPs.
PROBE_CLIENT_IPS: frozenset[str] = frozenset(
    {
        "195.128.172.227",
        "77.42.92.141",
        "::1",
        "127.0.0.1",
    }
)


def _api_key_user_is_probe(db: "Session", api_key_id: UUID | str | None) -> bool:
    """True if *api_key_id* resolves to a user whose email is a known probe."""
    if not api_key_id:
        return False
    from app.models import APIKey, User

    row = db.query(User.email).join(APIKey, APIKey.user_id == User.id).filter(APIKey.id == api_key_id).first()
    if row is None or not row[0]:
        return False
    return row[0].strip().lower() in PROBE_USER_EMAILS


def is_probe_request(
    db: "Session",
    *,
    api_key_id: UUID | str | None = None,
    client_ip: str | None = None,
) -> bool:
    """Return True when this write should be tagged ``is_probe=True``.

    Fails CLOSED to False on any lookup problem (a probe row wrongly left
    untagged is a cosmetic analytics miss; the reverse — tagging real
    customer traffic as a probe — is the failure mode this guards against
    by only ever turning True on an exact, positive match).

    Args:
        db: Active session, used only to resolve api_key_id -> user email.
        api_key_id: The APIKey.id of the authenticated caller, if any.
        client_ip: The resolved real client IP (see
            app.utils.client_ip._real_client_ip), if any.
    """
    if client_ip and client_ip.strip() in PROBE_CLIENT_IPS:
        return True
    try:
        return _api_key_user_is_probe(db, api_key_id)
    # Rationale: probe tagging is best-effort observability metadata; a
    # lookup failure (detached session, disconnected DB) must never block
    # or corrupt the write it is decorating.
    except Exception:  # noqa: BLE001
        return False
