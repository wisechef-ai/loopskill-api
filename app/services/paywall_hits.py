"""paywall_0925 — record every time a tier gate refuses a caller.

Before this module the only paywall metric (``users.free_sync_used_at``) had no
writer, and every live tier gate — private-bundle cap, deploy tier, fleet
member cap, forks, API-key cap, the MCP compose quota — answered 402/403 and
left no trace. So "has a stranger ever reached a paywall?" could not be
answered, and pricing could not be tuned from data.

``record_paywall_hit`` is the ONE writer. Every gate site calls it on the
refusal branch only (never on an allowed call) and then raises exactly the
response it raised before — the 402/403 bodies are an external contract
(``pro_tier_limit``, ``pro_tier_required:*``, ``tier_key_cap_exceeded`` ...).

Guarantees:

* **Never raises, never changes the response.** Any failure is logged and
  swallowed; a telemetry bug must not turn a clean 402 into a 500.
* **Survives the raise.** Written through ``side_session`` (see that module),
  because the caller's request session is discarded when the handler raises.
* **Bounded.** One row per (gate, subject, UTC day); repeats increment
  ``hit_count``. A retry-looping agent costs one row a day, not one per call.
* **Classified once.** fleet / stranger / unknown via the funnel ledger's
  ``classify`` (config/fleet_exclusions.yaml), persisted with its evidence.

Gate names are the ``GATE_*`` constants below; the admin pulse groups by them.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services._side_session import side_session

logger = logging.getLogger(__name__)

# ── Gate names (stable: the pulse groups by these strings) ──────────────
GATE_BUNDLE_PRIVATE_CAP = "bundle_private_cap"  # POST /api/bundles 403 pro_tier_limit
GATE_FORK_CLAIM_PRIVATE_CAP = "fork_claim_private_cap"  # claim a fork 403 pro_tier_limit
GATE_MCP_COMPOSE_PRIVATE_CAP = "mcp_compose_private_cap"  # MCP compose 403 cookbook_limit
GATE_BUNDLE_SKILL_CAP = "bundle_skill_cap"  # add skill 403 pro_skill_cap
GATE_METASEARCH_DEPLOY_SKILL_CAP = "metasearch_deploy_skill_cap"  # 403 skill_cap_reached
GATE_DEPLOY_TIER = "deploy_tier"  # /api/deploy/* 402 pro_tier_required
GATE_FLEET_MEMBER_CAP = "fleet_member_cap"  # enroll member 402 tier_key_cap_exceeded
GATE_FORKS_TIER = "forks_tier"  # /api/forks/* 402 needs_tier
GATE_MCP_FORK_TIER = "mcp_fork_tier"  # MCP tailor verbs needs_tier
GATE_API_KEY_CAP = "api_key_cap"  # POST /api/api-keys 403 key_cap_exceeded
GATE_SKILL_INSTALL_TIER = "skill_install_tier"  # install a Pro skill 403
GATE_SKILL_FILES_TIER = "skill_files_tier"  # read a Pro skill's files 403
GATE_BUNDLE_SKILL_INSTALL_TIER = "bundle_skill_install_tier"  # bundle single install 403
GATE_MCP_BUNDLE_INSTALL_TIER = "mcp_bundle_install_tier"  # MCP bundle install tier_insufficient
GATE_ARTIFACT_LIKE_TIER = "artifact_like_tier"  # like an over-tier artifact 403
GATE_RECIPIFY_TIER = "recipify_tier"  # recipify needs_tier (answers 401, historical)


def _subject_key(user_id: UUID | None, api_key_id: UUID | None, ip: str | None) -> str:
    """Most specific stable identity available: user > api key > ip > anon."""
    if user_id is not None:
        return f"user:{user_id}"
    if api_key_id is not None:
        return f"key:{api_key_id}"
    if ip:
        return f"ip:{ip}"[:128]
    return "anon"


def _as_uuid(value: object) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value  # type: ignore[return-value]
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def record_paywall_hit(
    db: Session,
    *,
    gate: str,
    http_status: int,
    tier: str | None,
    user_id: UUID | str | None = None,
    api_key_id: UUID | str | None = None,
    ip: str | None = None,
    email: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record one refusal at ``gate``. Never raises (see module docstring)."""
    # Rationale: this is fire-and-forget telemetry on an error path; ANY
    # failure (DB down, constraint drift, bad input) must degrade to a log
    # line, never alter the 402/403 the caller is about to return.
    try:
        _record(
            db,
            gate=gate,
            http_status=http_status,
            tier=tier,
            user_id=_as_uuid(user_id),
            api_key_id=_as_uuid(api_key_id),
            ip=ip,
            email=email,
            now=now or datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001
        logger.warning("paywall_hits: failed to record gate=%s", gate, exc_info=True)


def _record(
    db: Session,
    *,
    gate: str,
    http_status: int,
    tier: str | None,
    user_id: UUID | None,
    api_key_id: UUID | None,
    ip: str | None,
    email: str | None,
    now: datetime,
) -> None:
    from app.models import PaywallHit, User
    from app.services.funnel_ledger import classify

    day: date = now.date()
    subject = _subject_key(user_id, api_key_id, ip)

    with side_session(db) as s:
        if email is None and user_id is not None:
            email = s.execute(select(User.email).where(User.id == user_id)).scalar_one_or_none()
        classification, evidence = classify(
            email=email, ip=ip, api_key_id=str(api_key_id) if api_key_id else None
        )

        def _bump() -> bool:
            row = s.execute(
                select(PaywallHit).where(
                    PaywallHit.gate == gate,
                    PaywallHit.subject_key == subject,
                    PaywallHit.day == day,
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.hit_count = (row.hit_count or 0) + 1
            row.last_hit_at = now
            row.http_status = http_status
            row.tier = tier
            return True

        if _bump():
            return
        s.add(
            PaywallHit(
                day=day,
                gate=gate,
                subject_key=subject,
                user_id=user_id,
                api_key_id=api_key_id,
                tier=(tier or None) and str(tier)[:32],
                http_status=int(http_status),
                classification=classification,
                classification_evidence=evidence,
                hit_count=1,
                first_hit_at=now,
                last_hit_at=now,
            )
        )
        try:
            s.flush()
        except IntegrityError:
            # Lost a same-day race for this (gate, subject): fold into the winner.
            s.rollback()
            _bump()


def record_paywall_hit_for_ctx(
    db: Session,
    ctx: object | None,
    *,
    gate: str,
    http_status: int,
    tier: str | None = None,
    request: object | None = None,
) -> None:
    """Convenience form for callers holding an AuthContext-like ``ctx``.

    Reads ``user_id`` / ``api_key_id`` / ``tier`` off ``ctx`` (any object with
    those attributes: AuthContext, CookbookCtx, TierContext) and the client IP
    off ``request`` when one is supplied.
    """
    ip = client_ip_of(request) if request is not None else None
    record_paywall_hit(
        db,
        gate=gate,
        http_status=http_status,
        tier=tier if tier is not None else getattr(ctx, "tier", None),
        user_id=getattr(ctx, "user_id", None),
        api_key_id=getattr(ctx, "api_key_id", None)
        or (getattr(getattr(request, "state", None), "api_key_id", None) if request is not None else None),
        ip=ip,
    )


def client_ip_of(request: object) -> str | None:
    """Trusted-proxy-aware client IP, or None. Never raises."""
    # Rationale: IP is optional enrichment for classification; a request
    # without a client (TestClient variants, MCP stdio) must not break the write.
    try:
        from app.config import settings
        from app.utils.client_ip import _real_client_ip

        ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)  # type: ignore[arg-type]
        return None if ip in (None, "", "unknown", "testclient") else ip
    except Exception:  # noqa: BLE001
        return None
