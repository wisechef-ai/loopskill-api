"""Loop manifest validation — the safety-bounded contract gate.

A LoopSkill *loop* is a runnable autonomous agentic loop. What makes the registry
*vetted* (the white-space wedge) is that every published loop MUST carry a
safety-bounded execution contract the registry validates on publish and a runner
can enforce:

  - success_condition     : the goal the loop drives toward (non-empty)
  - verification_script   : an OBJECTIVE check of success_condition (non-empty);
                            a loop you can't verify is unsafe to share
  - stopping_criteria     : explicit success / failure / budget stops
  - max_turns             : a hard ceiling on autonomous turns (> 0, capped)
  - tool_allowlist        : deny-by-default; an explicit list (may be empty, never null)
  - budget_usd            : optional USD budget; if absent, max_turns is the only
                            backstop and MUST be present (it always is)

This module is import-safe (stdlib only) so it can be reused by the publish route,
the MCP publish tool, and any future runner without dragging in FastAPI/DB deps.
"""
from __future__ import annotations

from typing import Any

# Hard ceiling so a published loop can't request an unbounded turn budget.
MAX_TURNS_CEILING = 500
# Required keys inside stopping_criteria.
REQUIRED_STOP_KEYS = ("success", "failure", "budget")


class LoopValidationError(ValueError):
    """Raised when a loop manifest violates the safety-bounded contract."""


def validate_loop_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a loop manifest. Returns the cleaned dict.

    Raises LoopValidationError with an actionable message on any violation.
    Pure function — no I/O, no DB. Safe to call from route, MCP tool, or runner.
    """
    if not isinstance(manifest, dict):
        raise LoopValidationError("manifest must be an object")

    errors: list[str] = []
    out: dict[str, Any] = {}

    # — success_condition —
    sc = (manifest.get("success_condition") or "").strip()
    if not sc:
        errors.append("success_condition is required and must be non-empty")
    out["success_condition"] = sc

    # — verification_script: a loop you can't verify is unsafe to share —
    vs = (manifest.get("verification_script") or "").strip()
    if not vs:
        errors.append(
            "verification_script is required — an objective check of the success "
            "condition. A loop with no verification cannot be safely published."
        )
    out["verification_script"] = vs

    # — system_prompt —
    sp = (manifest.get("system_prompt") or "").strip()
    if not sp:
        errors.append("system_prompt is required and must be non-empty")
    out["system_prompt"] = sp

    # — max_turns: hard ceiling against runaway —
    raw_turns = manifest.get("max_turns", 25)
    try:
        turns = int(raw_turns)
    except (TypeError, ValueError):
        turns = -1
    if turns <= 0:
        errors.append("max_turns is required and must be a positive integer")
    elif turns > MAX_TURNS_CEILING:
        errors.append(
            f"max_turns {turns} exceeds ceiling {MAX_TURNS_CEILING}; "
            "loops must be bounded"
        )
    out["max_turns"] = turns

    # — budget_usd: optional, but if present must be a non-negative number —
    budget = manifest.get("budget_usd")
    if budget is not None:
        try:
            budget_f = float(budget)
        except (TypeError, ValueError):
            errors.append("budget_usd must be a number when provided")
            budget_f = None
        else:
            if budget_f < 0:
                errors.append("budget_usd must be >= 0")
        out["budget_usd"] = budget_f
    else:
        out["budget_usd"] = None

    # — tool_allowlist: deny-by-default; explicit list, may be empty, never null —
    allow = manifest.get("tool_allowlist")
    if allow is None:
        errors.append(
            "tool_allowlist is required (deny-by-default). Use [] for a loop that "
            "needs no tools; never omit it."
        )
        allow = []
    elif not isinstance(allow, list) or not all(isinstance(t, str) for t in allow):
        errors.append("tool_allowlist must be a list of tool-name strings")
        allow = [t for t in allow if isinstance(t, str)] if isinstance(allow, list) else []
    out["tool_allowlist"] = allow

    # — stopping_criteria: explicit success/failure/budget stops —
    stops = manifest.get("stopping_criteria")
    if not isinstance(stops, dict):
        errors.append(
            "stopping_criteria is required and must be an object with keys "
            f"{REQUIRED_STOP_KEYS}"
        )
        stops = {}
    else:
        missing = [k for k in REQUIRED_STOP_KEYS if k not in stops]
        if missing:
            errors.append(
                "stopping_criteria missing required keys: "
                + ", ".join(missing)
                + f" (need all of {REQUIRED_STOP_KEYS})"
            )
    out["stopping_criteria"] = stops

    # Cross-check: there must always be at least one hard backstop. max_turns is
    # mandatory and bounded above, so this holds by construction — but assert it
    # so a future relaxation of max_turns can't silently produce an unbounded loop.
    if out["max_turns"] <= 0 and out["budget_usd"] in (None, 0):
        errors.append(
            "a loop must have at least one hard backstop (max_turns or budget_usd)"
        )

    if errors:
        raise LoopValidationError("; ".join(errors))
    return out
