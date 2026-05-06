"""recipes_recall — Phase E (skill memory). Stub returns a deterministic
``not_implemented`` payload so MCP clients can tolerate early integration.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def recipes_recall(db: Session, **_: Any) -> dict[str, Any]:  # noqa: ARG001
    return {"error": "not_implemented", "phase": "E"}
