"""Validator for the loop_spec JSON payload on Skill rows with kind='loop'.

Reuses composite_loop_validation primitives (schedule, subagents_config, budget)
so Phase 1 and the existing CompositeLoop validation stay in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import VALID_SKILL_KINDS, Skill
from app.services.composite_loop_validation import (
    CompositeLoopValidationError,
    validate_budget,
    validate_schedule,
    validate_subagents_config,
)

logger = logging.getLogger(__name__)


def assert_kind_valid(kind: str) -> None:
    """Raise ValueError if kind is not in VALID_SKILL_KINDS."""
    if kind not in VALID_SKILL_KINDS:
        raise ValueError(f"Invalid skill kind {kind!r}. Must be one of {VALID_SKILL_KINDS}.")


def validate_loop_spec(db: Session, spec: dict[str, Any]) -> None:
    """Validate a loop_spec payload for a Skill with kind='loop'.

    Raises CompositeLoopValidationError on any invalid field.
    verifier_slug existence is a soft check — logs a warning but does not
    hard-fail (Phase 2 handles verifier migration).
    """
    if not isinstance(spec, dict):
        raise CompositeLoopValidationError("loop_spec", "must be a JSON object")

    # schedule is required and must be parseable.
    validate_schedule(spec.get("schedule", ""))

    # subagents_config is required and must have a maker key.
    validate_subagents_config(spec.get("subagents_config", {}))

    # verifier_slug is required (non-empty); existence is a soft check.
    verifier_slug = spec.get("verifier_slug")
    if not verifier_slug or not isinstance(verifier_slug, str):
        raise CompositeLoopValidationError("verifier_slug", "verifier_slug is required")
    verifier_row = db.query(Skill).filter(Skill.slug == verifier_slug).first()
    if verifier_row is None:
        logger.warning(
            "loop_spec verifier_slug %r not found in skills table — "
            "soft-fail (Phase 2 will migrate verifiers into skills)",
            verifier_slug,
        )

    # budget_usd is optional but must be > 0 when present.
    validate_budget(spec.get("budget_usd"))

    # skills entries (if present) must each have a slug key.
    for entry in spec.get("skills") or []:
        if not isinstance(entry, dict) or "slug" not in entry:
            raise CompositeLoopValidationError("skills", f"invalid skill entry: {entry!r}")

    # connectors entries (if present) must each have a slug key.
    for entry in spec.get("connectors") or []:
        if not isinstance(entry, dict) or "slug" not in entry:
            raise CompositeLoopValidationError("connectors", f"invalid connector entry: {entry!r}")
