"""recipes_carousel_today — proxy for today's curated carousel."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.carousel.routes import _build_response, _entries_for_date


def recipes_carousel_today(db: Session) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    entries = _entries_for_date(today, db)
    return _build_response(today, entries).model_dump(mode="json")
