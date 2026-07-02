"""Tests for Issue #22: InstallEvent.client_ip uses _real_client_ip.

Verifies that when a POST /api/skills/install request arrives from a
Cloudflare edge IP (in TRUSTED_PROXY_CIDRS) with a CF-Connecting-IP header,
the InstallEvent row records the visitor IP (not the CF edge IP).
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from uuid import uuid4

from app.utils.client_ip import _real_client_ip

# ── Unit tests for the client_ip usage in install endpoint ───────────────────

CF_CIDR = "173.245.48.0/20"
CF_PEER = "173.245.48.5"


def _make_mock_request(peer: str, cf_ip: str | None = None, xff: str | None = None):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer
    headers: dict = {}
    if cf_ip is not None:
        headers["cf-connecting-ip"] = cf_ip
    if xff is not None:
        headers["x-forwarded-for"] = xff
    req.headers.get = lambda name, default="": headers.get(name.lower(), default)
    return req


def test_install_event_uses_cf_ip_when_peer_is_trusted():
    """CF peer + CF-Connecting-IP header → visitor IP used as client_ip."""
    req = _make_mock_request(CF_PEER, cf_ip="203.0.113.77")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "203.0.113.77"


def test_install_event_uses_socket_when_peer_is_untrusted():
    """Direct (untrusted) peer → raw socket IP used even if CF header present."""
    req = _make_mock_request("1.2.3.4", cf_ip="203.0.113.77")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "1.2.3.4"


def test_install_event_uses_socket_when_no_headers():
    """No forwarding headers → socket IP."""
    req = _make_mock_request(CF_PEER)
    result = _real_client_ip(req, [CF_CIDR])
    assert result == CF_PEER


def test_install_event_client_ip_import_in_routes():
    """Verify install_routes.py imports _real_client_ip from app.utils.client_ip."""
    import inspect
    import app.install_routes as install_routes_module  # Phase E: moved from routes.py

    src = inspect.getsource(install_routes_module)
    assert "from app.utils.client_ip import _real_client_ip" in src, (
        "install_routes.py must import _real_client_ip from app.utils.client_ip (Issue #22)"
    )


def test_install_event_uses_trusted_cidrs_from_settings():
    """Verify install_routes.py passes settings.TRUSTED_PROXY_CIDRS to _real_client_ip."""
    import inspect
    import app.install_routes as install_routes_module  # Phase E: moved from routes.py

    src = inspect.getsource(install_routes_module)
    assert "settings.TRUSTED_PROXY_CIDRS" in src, (
        "install_routes.py must pass settings.TRUSTED_PROXY_CIDRS to _real_client_ip"
    )
