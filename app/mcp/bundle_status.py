"""bundle_status — query outdated skills for a user's bundles.

Used by the MCP dispatcher to inject a ``bundle_status`` block into every
tool response when the authenticated user has skills with newer versions
available.

Caching: Redis with 60 s TTL, keyed on ``bundle_status:<user_id>:<org_id>``.
If Redis is unavailable (e.g., in the test environment), caching is skipped
gracefully.

mesh_0408 W1 (P0), codex review of PR #202 finding 2 — "automatic MCP response
decoration leaks cross-org data". This block is injected into EVERY MCP tool
response, and it used to select on a bare ``Bundle.bundle_owner == user_id``.
Every fleet-member key of an account carries the SAME ``user_id`` (it is minted
with ``user_id = fleet.owner_user_id``), so an agent deployed at client B got
client A's bundle ids, bundle NAMES and skill slugs appended to the result of
any tool it called — ``loopskill_search``, ``loopskill_feedback``, anything.
No authorization site was involved and no tool argument had to be guessed; the
leak arrived unrequested. The tenant now travels with the query AND with the
cache key.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.authz import owner_match_within_tenant_clause
from app.models import Bundle, BundleSkill, Skill

logger = logging.getLogger("wiserecipes.bundle_status")

_TTL = 60  # seconds


def _redis_client():  # pragma: no cover — optional dependency
    """Return a Redis client if available, else None."""
    try:
        import redis  # type: ignore[import-untyped]

        url = getattr(redis, "_recipes_url", None)
        return redis.from_url(url) if url else None
    # Rationale: Redis is optional; any import/connection error → return None (DB will serve)
    except Exception:  # noqa: BLE001
        return None


def get_bundle_status(
    db: Session, user_id: UUID | str | None, *, org_id: UUID | str | None = None
) -> dict[str, Any] | None:
    """Return the ``bundle_status`` dict or *None* if nothing to report.

    ``org_id`` is the tenant the CALLING KEY resolved to (``auth_ctx.org_id``).
    It is keyword-only and defaults to ``None`` = personal scope, which is the
    fail-CLOSED answer: a caller whose tenant could not be resolved is shown
    only bundles that carry no ``org_id`` at all, never an org's. Passing the
    caller's ``user_id`` without their tenant can therefore no longer widen the
    result set by accident.

    Returns *None* when:
      - user_id is None
      - the caller has no cookbooks IN THIS TENANT
      - all pinned versions are already at latest
    """
    if user_id is None:
        return None

    # Normalise to UUID
    if isinstance(user_id, str):
        try:
            user_id = UUID(user_id)
        except (ValueError, AttributeError):
            return None
    if isinstance(org_id, str):
        try:
            org_id = UUID(org_id)
        except (ValueError, AttributeError):
            return None

    # Try Redis cache. The tenant is part of the key: one account's two member
    # keys share a user_id, so a user-only key served client A's status block to
    # client B's agent for the whole TTL even once the query was scoped.
    cache_key = _cache_key(user_id, org_id)
    try:
        rds = _redis_client()
        if rds:
            cached = rds.get(cache_key)
            if cached:
                data = json.loads(cached)
                return data if data else None
    # Rationale: Redis cache read is best-effort; any error → fall through to DB query
    except Exception:  # noqa: BLE001
        logger.debug("Redis cache read failed, proceeding with query")

    # ── DB query ─────────────────────────────────────────────────────────
    # portal_0610 B2 — fetch all declared bundle skills for this owner, then
    # compute the SEMANTIC latest per skill in Python (SQL func.max(semver) is
    # lexicographic and mis-ranks "1.10.0" < "1.9.0").
    from app.services.semver import latest_semver_for_skills

    candidate_rows = (
        db.query(
            Bundle.id.label("cb_id"),
            Bundle.name.label("cb_name"),
            Skill.id.label("skill_id"),
            Skill.slug,
            BundleSkill.pinned_version,
        )
        .join(BundleSkill, BundleSkill.bundle_id == Bundle.id)  # compat-alias
        .join(Skill, Skill.id == BundleSkill.skill_id)
        # mesh_0408 W1 (P0): tenant-scoped through the SAME clause every other
        # bundle-list site uses, so this decoration cannot drift away from the
        # routes it decorates.
        .filter(owner_match_within_tenant_clause(SimpleNamespace(user_id=user_id, org_id=org_id), Bundle))
        .all()
    )

    latest_by_skill = latest_semver_for_skills(db, {r.skill_id for r in candidate_rows})

    # Outdated when the skill has a latest version AND (pin is NULL or pin != latest).
    rows = []
    for r in candidate_rows:
        latest = latest_by_skill.get(r.skill_id)
        if latest is None:
            continue
        if r.pinned_version is None or r.pinned_version != latest:
            rows.append(
                type(
                    "Row",
                    (),
                    {
                        "cb_id": r.cb_id,
                        "cb_name": r.cb_name,
                        "slug": r.slug,
                        "pinned_version": r.pinned_version,
                        "latest": latest,
                    },
                )()
            )

    if not rows:
        # Cache empty sentinel so we don't re-query every call
        _cache_set(cache_key, {})
        return None

    # Group by bundle
    cookbooks_map: dict[UUID, dict[str, Any]] = {}
    for r in rows:
        cb_id = r.cb_id
        if cb_id not in cookbooks_map:
            cookbooks_map[cb_id] = {
                "id": str(cb_id),
                "name": r.cb_name,
                "updates_available": 0,
                "outdated_skills": [],
            }
        cookbooks_map[cb_id]["updates_available"] += 1
        cookbooks_map[cb_id]["outdated_skills"].append(
            {
                "slug": r.slug,
                "current": r.pinned_version,
                "latest": r.latest,
            }
        )

    result: dict[str, Any] = {
        "your_cookbooks": list(cookbooks_map.values()),
        "hint": "Call loopskill_sync(<cookbook_id>) to apply updates.",
    }

    _cache_set(cache_key, result)
    return result


def _cache_key(user_id: Any, org_id: Any) -> str:
    """Redis key for one (caller, tenant) pair — never one per caller alone."""
    return f"bundle_status:{user_id}:{org_id}"


def invalidate_bundle_status(user_id: UUID | str | None, org_id: UUID | str | None = None) -> None:
    """Invalidate cached status for a (user, tenant) pair after a sync applies."""
    if user_id is None:
        return
    try:
        rds = _redis_client()
        if rds:
            rds.delete(_cache_key(user_id, org_id))
    # Rationale: Redis cache invalidation is non-critical; any error → log and continue
    except Exception:  # noqa: BLE001
        logger.debug("Redis cache invalidation failed (non-critical)")


def _cache_set(cache_key: str, data: dict) -> None:
    try:
        rds = _redis_client()
        if rds:
            rds.setex(cache_key, _TTL, json.dumps(data, default=str))
    # Rationale: Redis cache write is best-effort; any error → log and continue
    except Exception:  # noqa: BLE001
        logger.debug("Redis cache write failed (non-critical)")
