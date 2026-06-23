"""Token-prefix auth resolvers extracted from APIKeyMiddleware.dispatch().

Keeps the middleware god-node (app/middleware/api_key.py) under the 600-line
pyfile-size gate by housing the self-contained, prefix-specific key resolvers
here. Each resolver opens its own short-lived SessionLocal, looks the key up,
and returns an AuthContext (or None when the key is invalid/revoked). The
middleware translates a None into the appropriate 401 JSONResponse so HTTP
shaping stays in one place.

W0.2 (integrator_2905): fleet-key resolution moved here first; cbt_ share-token
resolution can follow the same pattern in a later split if the dispatch method
keeps growing.
"""

from __future__ import annotations

import hashlib

from app.auth_ctx import AuthContext


def resolve_fleet_auth_ctx(key: str) -> AuthContext | None:
    """Resolve a ``rec_fleet_*`` key to a fleet-scoped AuthContext.

    Returns None when the key does not match an active Fleet row (the caller
    maps that to HTTP 401). Opens and closes its own DB session so the
    middleware does not have to thread one through.
    """
    from app.database import SessionLocal
    from app.models import Fleet as _Fleet

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    db = SessionLocal()
    try:
        row = db.query(_Fleet).filter(_Fleet.fleet_api_key_hash == key_hash).first()
        if row is None:
            return None
        return AuthContext(
            scope="fleet",
            fleet_id=row.id,
            user_id=row.owner_user_id,
        )
    finally:
        db.close()
