"""Phase D — RECIPES_TELEMETRY=off must short-circuit the heartbeat client.

Verified at the unit level via a mock urllib3 PoolManager: when the env var
is "off", `send_heartbeat` MUST NOT call `.request(...)` at all.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from app.heartbeat_client import send_heartbeat


def _mock_pool():
    pm = MagicMock()
    pm.request.return_value = MagicMock(status=201, data=b'{"ok":true}')
    return pm


def test_off_blocks_outbound(monkeypatch):
    monkeypatch.setenv("RECIPES_TELEMETRY", "off")
    pool = _mock_pool()
    out = send_heartbeat(
        endpoint="https://recipes.wisechef.ai/api/v1/heartbeat",
        pool=pool,
    )
    assert out["skipped"] is True
    pool.request.assert_not_called()


def test_unset_sends(monkeypatch):
    monkeypatch.delenv("RECIPES_TELEMETRY", raising=False)
    pool = _mock_pool()
    out = send_heartbeat(
        endpoint="https://recipes.wisechef.ai/api/v1/heartbeat",
        pool=pool,
    )
    assert out["sent"] is True
    pool.request.assert_called_once()
    args, kwargs = pool.request.call_args
    assert args[0] == "POST"
    body = kwargs.get("body") or args[2]
    # Body must contain only salt and last_seen_day
    import json as _json
    parsed = _json.loads(body)
    assert set(parsed.keys()) == {"salt", "last_seen_day"}


def test_explicit_on_sends(monkeypatch):
    monkeypatch.setenv("RECIPES_TELEMETRY", "on")
    pool = _mock_pool()
    out = send_heartbeat(
        endpoint="https://recipes.wisechef.ai/api/v1/heartbeat",
        pool=pool,
    )
    assert out["sent"] is True
    pool.request.assert_called_once()


def test_arbitrary_truthy_value_sends(monkeypatch):
    """Only literal "off" disables — typo-safe (default is OPT-OUT, not opt-in)."""
    monkeypatch.setenv("RECIPES_TELEMETRY", "yes")
    pool = _mock_pool()
    out = send_heartbeat(
        endpoint="https://recipes.wisechef.ai/api/v1/heartbeat",
        pool=pool,
    )
    assert out["sent"] is True


def test_disabled_via_zero(monkeypatch):
    """`RECIPES_TELEMETRY=0` and `=false` are also recognised as off."""
    pool = _mock_pool()
    for v in ("0", "false", "FALSE", "off", "OFF"):
        monkeypatch.setenv("RECIPES_TELEMETRY", v)
        out = send_heartbeat(
            endpoint="https://recipes.wisechef.ai/api/v1/heartbeat",
            pool=pool,
        )
        assert out["skipped"] is True, f"{v!r} should disable"
    pool.request.assert_not_called()
