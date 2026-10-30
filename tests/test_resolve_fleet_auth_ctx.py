"""DRAFTED TEST 2 — critical path: rec_fleet_ key resolution in token auth.

WHY THIS PATH IS CRITICAL
-------------------------
`app/middleware/_token_auth.py:resolve_fleet_auth_ctx` measured at 0.0%
coverage (14/14 statements missed) on 2026-09-26. This function is the ONLY
resolver that turns a `rec_fleet_*` API key into a fleet-scoped AuthContext
used by every fleet control-plane route (fleetos_1607: placements, harvest,
run registry). If resolution breaks or leaks:
  - fleet routes authenticate as the wrong principal, or
  - a fleet key hashing mismatch silently 401s every fleet agent.
It is reached from APIKeyMiddleware.dispatch() (line ~487), which itself has
that branch missed (487-496). Whole auth path: ZERO tests today
(`grep resolve_fleet_auth_ctx tests/` → 0 hits).

WHAT IS TESTED
--------------
1. Valid rec_fleet_ key (SHA-256 present in fleets table) -> fleet AuthContext
2. Unknown key -> None (middleware maps None -> 401)
3. Same key twice -> deterministic identical hash lookup
"""

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.auth_ctx import AuthContext
from app.middleware._token_auth import resolve_fleet_auth_ctx


def _patch_fleet_query(found_row):
    """Patch SessionLocal + Fleet model query inside the module-under-test.

    Note: the function under test imports SessionLocal and the Fleet model
    INSIDE its body (`from app.database import SessionLocal`), so patching must
    target the source modules, not _token_auth itself. The DB is never touched:
    we patch app.database.SessionLocal and drive app.models.Fleet's query chain.
    """
    row = found_row if found_row is not None else None

    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = row
    session_local = MagicMock(return_value=session)
    return session_local, session


def test_valid_fleet_key_resolves_to_fleet_scoped_authctx():
    key = "rec_fleet_" + "a" * 32
    expected_hash = hashlib.sha256(key.encode()).hexdigest()
    fleet_row = SimpleNamespace(id=uuid.uuid4(), owner_user_id=uuid.uuid4())

    session_local, session = _patch_fleet_query(fleet_row)
    with patch("app.database.SessionLocal", session_local), \
         patch("app.models.Fleet") as fleet_cls:
        ctx = resolve_fleet_auth_ctx(key)

    # Filter used the SHA-256 hash of the key as the lookup column value
    (filter_expr,), _kwargs = fleet_cls.fleet_api_key_hash.__eq__.call_args
    assert filter_expr == expected_hash
    assert isinstance(ctx, AuthContext)
    assert ctx.scope == "fleet"
    assert ctx.fleet_id == fleet_row.id
    assert ctx.user_id == fleet_row.owner_user_id
    session.close.assert_called_once()


def test_unknown_fleet_key_returns_none_not_exception():
    """None is the contract: the middleware translates None -> 401."""
    session_local, _ = _patch_fleet_query(None)
    with patch("app.database.SessionLocal", session_local), \
         patch("app.models.Fleet"):
        ctx = resolve_fleet_auth_ctx("rec_fleet_" + "does-not-exist")

    assert ctx is None


def test_fleet_key_hash_is_deterministic_sha256():
    """The lookup column value IS sha256(key) — and identical across calls.

    Pins what resolve_fleet_auth_ctx actually derives
    (app/middleware/_token_auth.py): key_hash = sha256(key.encode()).hexdigest()
    queried as ``Fleet.fleet_api_key_hash == key_hash``. Two resolutions of
    the same key must hit the DB with the same 64-char hash and resolve the
    same principal; a hash-function or encoding change silently 401s every
    fleet agent, so the hash is read back FROM the query, not recomputed in
    the test beside it.
    """
    key = "rec_fleet_" + "b" * 32
    expected_hash = hashlib.sha256(key.encode()).hexdigest()
    fleet_row = SimpleNamespace(id=uuid.uuid4(), owner_user_id=uuid.uuid4())

    session_local, session = _patch_fleet_query(fleet_row)
    with patch("app.database.SessionLocal", session_local), \
         patch("app.models.Fleet") as fleet_cls:
        ctx1 = resolve_fleet_auth_ctx(key)
        ctx2 = resolve_fleet_auth_ctx(key)
        queried = [
            c.args[0]
            for c in fleet_cls.fleet_api_key_hash.__eq__.call_args_list
        ]

    assert queried == [expected_hash, expected_hash]
    assert len(queried[0]) == 64  # matches Fleet.fleet_api_key_hash String(64)
    assert ctx1 is not None and ctx2 is not None
    assert ctx1.fleet_id == ctx2.fleet_id == fleet_row.id
    assert ctx1.user_id == ctx2.user_id == fleet_row.owner_user_id
    assert session.close.call_count == 2  # one session opened+closed per call
