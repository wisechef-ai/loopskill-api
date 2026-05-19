"""recipes_subrecipe_resolve — Phase C (sub-recipe key minting).

Phase A always reports the caller as ``operator``. Phase C swaps this for
the actual sub-key validation logic.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def recipes_subrecipe_resolve(db: Session, **_: Any) -> dict[str, Any]:  # noqa: ARG001
    # Public-scope MCP tool: Phase C stub; returns fixed operator scope, no data exposure.
    return {"scope": "operator"}
