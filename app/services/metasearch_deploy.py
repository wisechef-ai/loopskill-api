"""Deploy-time resolve-and-pin for external skills (metasearch_0710 P3).

THE north-star mechanism: a metasearch result (skills.sh / github tap) deploys to
a fleet in one motion. P1 proved the card resolves; P2 rendered it deployable;
P3 makes the deploy actually pin a server-side artifact so 30k agents reconcile
against OUR pinned content — never re-resolving upstream (§7.5 invariant, the
scale ship-blocker).

The FK coupling the planning council flagged (C3 — ``bundle_skills.skill_id`` is a
NOT-NULL FK to ``skills.id``) is ALREADY solved: ``materialize_external_skill``
mints a real private ``Skill`` row (``skill_variant=external``,
``original_source_url``), so an external skill has a ``skills.id`` and can enter
desired-state. The remaining gap the plan names: external skills are pin-rejected
(``bundle_routes.py`` L1010) because they have no ``SkillVersion``/semver contract.

P3's insight: fleet-deploy does NOT need a semver — it needs a **content pin**.
At deploy time we resolve the SKILL.md ONCE (``resolve_external_install``), compute
``sha256(content)``, and store the pin in the materialized skill's descriptor:
``{pinned_sha, pinned_at, pinned_raw_url, pinned_scan_status}``. The pin is the
deploy-time artifact identity. Agents reconcile against it; a re-deploy (or a
rate-limited SERVER-side refresh — one call per skill, never per agent) advances
it. This is "pin ≠ rehost the catalog": we pin only the handful actually deployed,
never the 66k rows.

decision #6 (ClawHub): ClawHub is preview-only (Adam condition 2b) — its
``route_install`` is DEEP_LINK so ``resolve_external_install`` returns None →
``pin_external_for_deploy`` fails closed and ClawHub is NOT fleet-deployable. Only
skills.sh + github taps (FETCH_ORIGIN, redistributable) pin. This is enforced by
the resolver, not re-implemented here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Skill

logger = logging.getLogger(__name__)


@dataclass
class PinResult:
    """Outcome of a deploy-time resolve-and-pin. ``pinned`` False = fail-closed
    (the external skill is not fleet-deployable — e.g. ClawHub deep-link, or the
    origin fetch failed). Never raises; a deploy on an unpinnable skill is a
    handled 4xx, never a 500."""

    pinned: bool
    skill_id: str | None = None
    skill_uuid: object = None  # the raw UUID for DB queries (skill_id is the str form for JSON)
    slug: str | None = None
    pinned_sha: str | None = None
    pinned_raw_url: str | None = None
    scan_status: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "pinned": self.pinned,
            "skill_id": self.skill_id,
            "slug": self.slug,
            "pinned_sha": self.pinned_sha,
            "pinned_raw_url": self.pinned_raw_url,
            "scan_status": self.scan_status,
            "reason": self.reason,
        }


def content_sha(content: str) -> str:
    """The pin identity: sha256 of the exact SKILL.md bytes an agent will run.
    Prefixed ``sha256:`` so the pin field self-describes its algorithm and can
    never be confused with a semver."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def pin_external_for_deploy(db: "Session", source: str, slug: str) -> PinResult:
    """Resolve an external skill's SKILL.md ONCE and pin its content SHA onto the
    materialized ``Skill`` row's descriptor — the deploy-time-resolve step (§7.5).

    Fail-closed: a source that can't resolve to redistributable content (ClawHub
    deep-link, non-redistributable license, origin outage, MCP-register) returns
    ``pinned=False`` so the deploy is rejected with a reason — never a dead pin.

    Idempotent: re-pinning the same (source, slug) recomputes the SHA from the
    current origin content and advances the pin (a controlled re-deploy / refresh).
    """
    from app.services.bundle_external import materialize_external_skill, resolve_external_install

    # 1) Resolve the live SKILL.md ONCE (this is the single upstream call — the
    #    deploy-time resolve; agents will never repeat it).
    try:
        resolved = resolve_external_install(source, slug)
    except Exception:  # noqa: BLE001
        logger.warning("pin resolve failed for %s:%s", source, slug, exc_info=True)
        return PinResult(False, reason="resolve_error")

    if not resolved or not isinstance(resolved.get("content"), str) or not resolved["content"].strip():
        # Deep-link (ClawHub), non-redistributable, MCP-register, or origin outage
        # → not fleet-deployable. Fail closed.
        return PinResult(False, slug=slug, reason="not_pinnable_no_content")

    content = resolved["content"]
    sha = content_sha(content)
    raw_url = resolved.get("raw_url")
    scan_status = resolved.get("scan_status")

    # 2) Materialize (or fetch) the private Skill row that carries the FK-satisfying
    #    skills.id — the row desired-state links to.
    skill = materialize_external_skill(db, source, slug)
    if skill is None:
        return PinResult(False, slug=slug, reason="materialize_failed")

    # 3) Write the pin into the descriptor. The materialized row's
    #    external_resources descriptor is the single home for the deploy-time pin.
    descriptor: dict[str, Any] = dict(getattr(skill, "external_resources", None) or {})
    descriptor["pinned_sha"] = sha
    descriptor["pinned_at"] = datetime.now(timezone.utc).isoformat()
    descriptor["pinned_raw_url"] = raw_url
    descriptor["pinned_scan_status"] = scan_status
    skill.external_resources = descriptor
    # Flag SQLAlchemy that the JSON mutated (in-place dict edits aren't tracked).
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(skill, "external_resources")
    except Exception:  # noqa: BLE001
        pass
    db.add(skill)
    db.flush()

    return PinResult(
        pinned=True,
        skill_id=str(skill.id),
        skill_uuid=skill.id,
        slug=skill.slug,
        pinned_sha=sha,
        pinned_raw_url=raw_url,
        scan_status=scan_status,
        reason="pinned",
    )


def get_pin(skill: "Skill") -> str | None:
    """Read the deploy-time content pin from a materialized external skill, or
    None if it was never deployed/pinned (always-latest, never fleet-deployed)."""
    descriptor = getattr(skill, "external_resources", None) or {}
    pin = descriptor.get("pinned_sha") if isinstance(descriptor, dict) else None
    return pin if isinstance(pin, str) and pin.startswith("sha256:") else None
