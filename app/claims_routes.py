"""Claims contract routes — the single check every marketing producer calls.

GET  /api/marketing/claims        the contract (approved facts, allowed prices,
                                  retired rules with Postgres-ready patterns)
POST /api/marketing/claims/check  {"text": "..."} -> {"ok", "violations", ...}

Both live under the public ``/api/marketing/`` prefix (no key): the contract
holds only facts already on the public pricing page, and the check is a pure
function over the caller's own text. See app/services/claims_contract.py for
why this exists and how the Postiz trigger consumes the same rules.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.claims_contract import MAX_CHECK_CHARS, build_contract, check_text

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


class ClaimCheckIn(BaseModel):
    text: str = Field(..., description="Copy to check, as it will be published.")


@router.get("/claims")
def get_claims_contract() -> dict:
    return build_contract()


@router.post("/claims/check")
def post_claims_check(body: ClaimCheckIn) -> dict:
    if len(body.text) > MAX_CHECK_CHARS:
        raise HTTPException(status_code=413, detail=f"text longer than {MAX_CHECK_CHARS} characters")
    contract = build_contract()
    violations = check_text(body.text, contract)
    return {
        "ok": not violations,
        "violations": violations,
        "contract_hash": contract["contract_hash"],
        "approved_facts": contract["approved_facts"] if violations else [],
    }
