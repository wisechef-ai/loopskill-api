"""recipes_carousel_today — proxy for today's curated carousel."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.carousel.routes import _build_response, _entries_for_date


def recipes_carousel_today(db: Session) -> dict[str, Any]:
    """Return today's curated carousel of skills."""
    # Public-scope MCP tool: carousel data is always public; no user-specific data returned.
    today = datetime.now(UTC).date()
    entries = _entries_for_date(today, db)
    return _build_response(today, entries).model_dump(mode="json")
