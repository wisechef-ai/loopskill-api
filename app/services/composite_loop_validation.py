"""activate_0701 Phase A2 — composite loop validation + deploy helpers.

Design contract: docs/design/activate0701-phaseA2-composite-loop.md §Validation.

Responsibilities:
  * validate_composite_loop_manifest — verify_slug resolvable, subagents_config
    schema-validated, schedule parseable, budget > 0.
  * derive_residency — most-restrictive of member connector residency_tags.
  * bump_declaring_bundles_for_cl — advance generation of declaring bundles
    on version publish (mirrors reconcile.bump_declaring_bundles).
  * mint_deploy_provenance — mint a provenance_id at deploy time for a member
    (reuse the provenance seam — Phase T field exists on LoopRun).
"""

from __future__ import annotations

import re
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Bundle, BundleCompositeLoop, CompositeLoop, Verifier

# Valid model tiers for subagent config.
_VALID_MODEL_TIERS = frozenset({"opus", "sonnet", "haiku", "local", "any"})

# Schedule shorthand: "<N>m" or "<N>h" (e.g. "30m", "2h").
_SHORTHAND_RE = re.compile(r"^\d+[mh]$")

# Basic 5-field cron expression (minute hour day month dow).
_CRON_RE = re.compile(
    r"^[\*/]?\d+([\-,]\d+)*\s+"  # minute
    r"[\*/]?\d+([\-,]\d+)*\s+"  # hour
    r"[\*/]?\d+([\-,]\d+)*\s+"  # day of month
    r"[\*/]?\d+([\-,]\d+)*\s+"  # month
    r"[\*/]?\d+([\-,]\d+)*$"  # day of week
)

# Residency ordering: non-eu is more restrictive than eu.
_RESIDENCY_RANK = {"eu": 1, "non-eu": 2}


class CompositeLoopValidationError(Exception):
    """Raised when a composite loop manifest fails validation."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


def validate_schedule(schedule: str) -> None:
    """Validate a schedule string (cron 5-field or '<N>m|h' shorthand)."""
    if not schedule or not isinstance(schedule, str):
        raise CompositeLoopValidationError("schedule", "schedule is required")
    s = schedule.strip()
    if _SHORTHAND_RE.match(s) or _CRON_RE.match(s):
        return
    raise CompositeLoopValidationError("schedule", f"unparseable schedule: {schedule!r}")


def validate_subagents_config(config: Any) -> None:
    """Validate the structured subagents_config.

    Required shape: {maker: {model_tier, toolsets}, checker?: {…}}.
    At minimum ``maker`` must be present with a valid model_tier.
    """
    if not isinstance(config, dict):
        raise CompositeLoopValidationError("subagents_config", "must be a JSON object")
    if "maker" not in config:
        raise CompositeLoopValidationError("subagents_config", "must contain a 'maker' key")
    for role_key, role_cfg in config.items():
        if role_key not in ("maker", "checker"):
            raise CompositeLoopValidationError(
                "subagents_config", f"unknown role key {role_key!r} (expected 'maker' or 'checker')"
            )
        if not isinstance(role_cfg, dict):
            raise CompositeLoopValidationError("subagents_config", f"{role_key} must be a JSON object")
        tier = role_cfg.get("model_tier")
        if tier not in _VALID_MODEL_TIERS:
            raise CompositeLoopValidationError(
                "subagents_config",
                f"{role_key}.model_tier must be one of {sorted(_VALID_MODEL_TIERS)}, got {tier!r}",
            )
        toolsets = role_cfg.get("toolsets", [])
        if not isinstance(toolsets, list) or not all(isinstance(t, str) for t in toolsets):
            raise CompositeLoopValidationError(
                "subagents_config", f"{role_key}.toolsets must be a list of strings"
            )


def validate_budget(budget: Any) -> None:
    """budget_usd must be > 0 when present."""
    if budget is None:
        return
    try:
        val = float(budget)
    except (TypeError, ValueError):
        raise CompositeLoopValidationError("budget_usd", f"not a number: {budget!r}")
    if val <= 0:
        raise CompositeLoopValidationError("budget_usd", "must be > 0")


def derive_residency(connectors: list[dict[str, Any]]) -> str | None:
    """Derive the most-restrictive residency from member connectors.

    Returns None if no connector carries a residency_tag. Otherwise the
    most-restrictive (non-eu > eu) propagates.
    """
    tags = [c.get("residency_tag") for c in connectors if c.get("residency_tag")]
    if not tags:
        return None
    most_restrictive: str | None = None
    best_rank = 0
    for tag in tags:
        rank = _RESIDENCY_RANK.get(tag, 99)
        if rank > best_rank:
            best_rank = rank
            most_restrictive = tag
    return most_restrictive


def validate_composite_loop_manifest(
    db: Session,
    *,
    verifier_slug: str,
    schedule: str,
    subagents_config: Any,
    budget_usd: Any,
    skills: list[dict[str, Any]] | None = None,
    connectors: list[dict[str, Any]] | None = None,
) -> str | None:
    """Full validation pass for a composite loop manifest.

    Returns the derived residency if all checks pass; raises
    CompositeLoopValidationError on any failure.
    """
    # 1. verifier_slug must resolve to an existing public (or same-org) Verifier.
    if not verifier_slug:
        raise CompositeLoopValidationError("verifier_slug", "verifier_slug is required")
    verifier = db.query(Verifier).filter(Verifier.slug == verifier_slug).first()
    if verifier is None:
        raise CompositeLoopValidationError("verifier_slug", f"verifier {verifier_slug!r} not found")
    if verifier.is_archived:
        raise CompositeLoopValidationError("verifier_slug", f"verifier {verifier_slug!r} is archived")

    # 2. schedule parseable.
    validate_schedule(schedule)

    # 3. subagents_config schema.
    validate_subagents_config(subagents_config)

    # 4. budget > 0 when present.
    validate_budget(budget_usd)

    # 5. skills entries must have a slug key.
    for entry in skills or []:
        if not isinstance(entry, dict) or "slug" not in entry:
            raise CompositeLoopValidationError("skills", f"invalid skill entry: {entry!r}")

    # 6. connectors must have a slug key (residency_tag optional).
    for entry in connectors or []:
        if not isinstance(entry, dict) or "slug" not in entry:
            raise CompositeLoopValidationError("connectors", f"invalid connector entry: {entry!r}")

    # 7. derive residency.
    return derive_residency(connectors or [])


def bump_declaring_bundles_for_cl(db: Session, composite_loop_id: UUID) -> int:
    """Advance the generation (updated_at) of every bundle declaring a composite loop.

    Mirrors reconcile.bump_declaring_bundles. Called on version publish so the
    304 fast-path breaks and polling agents see the new version. Returns count.
    Caller commits.
    """
    bundle_ids = [
        row[0]
        for row in db.query(BundleCompositeLoop.bundle_id)
        .filter(BundleCompositeLoop.composite_loop_id == composite_loop_id)
        .all()
    ]
    if not bundle_ids:
        return 0
    return (
        db.query(Bundle)
        .filter(Bundle.id.in_(bundle_ids))
        .update({Bundle.updated_at: func.now()}, synchronize_session=False)
    )


def mint_deploy_provenance(
    db: Session,
    *,
    composite_loop_slug: str,
    member_id: UUID,
    fleet_id: UUID,
    instance_key: str,
) -> str:
    """Mint a provenance_id for a composite loop deploy.

    This reuses the provenance seam (secrets.token_urlsafe) to produce a random
    opaque token carried into LoopRun reports (Phase T field exists). Unlike
    install-provenance, this does NOT require an InstallEvent — it's a deploy
    provenance, so we mint directly. The caller commits.
    """
    return secrets.token_urlsafe(32)
