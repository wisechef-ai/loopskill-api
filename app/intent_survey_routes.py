"""Intent survey routes (stabilization_2605 phase A).

POST /api/intent-survey            — anonymous, accepts {q1..q5}
GET  /api/intent-survey/results    — admin-gated (x-api-key), aggregate counts
"""
from __future__ import annotations

import logging
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import IntentSurveyResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["intent-survey"])


Q1Choice = Literal["yes", "maybe", "no"]
Q4Choice = Literal["agency", "solo", "dev", "curious"]


class IntentSurveyIn(BaseModel):
    q1: Q1Choice = Field(..., description="Would you pay €100/mo for All-in?")
    q2: Optional[str] = Field(default=None, max_length=2000)
    q3: Optional[str] = Field(default=None, max_length=2000)
    q4: Q4Choice = Field(..., description="Which best describes you?")
    q5: Optional[str] = Field(default=None, max_length=512)


@router.post("/intent-survey", status_code=201)
def submit_intent_survey(payload: IntentSurveyIn, db: Session = Depends(get_db)):
    """Persist one anonymous survey response. Returns {ok, id}."""
    row = IntentSurveyResponse(
        id=uuid4(),
        q1=payload.q1,
        q2=payload.q2,
        q3=payload.q3,
        q4=payload.q4,
        q5=payload.q5,
    )
    db.add(row)
    db.commit()
    return {"ok": True, "id": str(row.id)}


def _require_admin(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    if not x_api_key or x_api_key != settings.API_KEY:
        raise HTTPException(status_code=403, detail="admin_only")


@router.get("/intent-survey/results")
def intent_survey_results(
    db: Session = Depends(get_db),
    _admin: None = Depends(_require_admin),
):
    """Aggregate counts grouped by q1 and q4. Email/free-text never returned."""
    total = db.query(IntentSurveyResponse).count()

    q1_rows = (
        db.query(IntentSurveyResponse.q1, func.count(IntentSurveyResponse.id))
          .group_by(IntentSurveyResponse.q1)
          .all()
    )
    q4_rows = (
        db.query(IntentSurveyResponse.q4, func.count(IntentSurveyResponse.id))
          .group_by(IntentSurveyResponse.q4)
          .all()
    )

    return {
        "total": total,
        "q1": {k: v for k, v in q1_rows},
        "q4": {k: v for k, v in q4_rows},
    }
