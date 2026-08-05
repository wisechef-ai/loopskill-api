"""Tests for Issue #22: InstallEvent.client_ip uses _real_client_ip.

Verifies that when a POST /api/skills/install request arrives from a
Cloudflare edge IP (in TRUSTED_PROXY_CIDRS) with a CF-Connecting-IP header,
the InstallEvent row records the visitor IP (not the CF edge IP).

RETARGETED 2026-08-05 (mesh_0408 Q031): the install-recording path that
used to live in app/install_routes.py as a hand-rolled InstallEvent block
moved into the canonical app/services/provenance.py::record_install_with_
provenance(...) helper (already shared by bundle_routes.py). The security
behaviour did NOT change -- record_install_with_provenance performs the
identical `from app.utils.client_ip import _real_client_ip` +
`_real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)` call that
install_routes.py used to do inline. The two source-text guards below were
pointed at the OLD location and started failing CI even though the fix they
guard is fully intact on the new call path -- they were testing WHERE the
text lives, not WHAT the code does. They now inspect
app.services.provenance (the module that owns the call today) instead of
app.install_routes, plus a guard that install_routes.py has not grown a
second, hand-rolled client-IP extraction that bypasses the trusted-CIDR
check.

A source-text assertion can only ever prove a string is present somewhere
in a module -- it cannot prove the trusted-CIDR check actually gates
anything. That is exactly why it took a manual read to notice the guard was
stale instead of broken: a purely textual check has no way to fail when the
logic underneath is still correct, and equally no way to fail if the logic
underneath were subtly wrong (e.g. if the CIDR argument were silently
swapped for an empty list at the call site). To close that gap,
`test_real_client_ip_rejects_spoofed_xff_from_untrusted_peer` and
`test_real_client_ip_honours_xff_from_trusted_peer` below call the REAL
`_real_client_ip` with untrusted-then-trusted peers and a spoofed
X-Forwarded-For header, so a regression in the actual spoof-rejection logic
is caught even if every import statement and settings reference stays
byte-for-byte in place.
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


def test_install_event_client_ip_import_in_provenance_service():
    """Verify app/services/provenance.py imports _real_client_ip from
    app.utils.client_ip (Issue #22, retargeted 2026-08-05: the install-
    recording path moved from install_routes.py's hand-rolled block into
    record_install_with_provenance)."""
    import inspect
    import app.services.provenance as provenance_module

    src = inspect.getsource(provenance_module)
    assert "from app.utils.client_ip import _real_client_ip" in src, (
        "app/services/provenance.py must import _real_client_ip from "
        "app.utils.client_ip (Issue #22)"
    )


def test_install_event_uses_trusted_cidrs_from_settings():
    """Verify app/services/provenance.py passes settings.TRUSTED_PROXY_CIDRS
    to _real_client_ip (Issue #22, retargeted 2026-08-05 -- see module
    docstring)."""
    import inspect
    import app.services.provenance as provenance_module

    src = inspect.getsource(provenance_module)
    assert "settings.TRUSTED_PROXY_CIDRS" in src, (
        "app/services/provenance.py must pass settings.TRUSTED_PROXY_CIDRS "
        "to _real_client_ip"
    )


def test_install_routes_has_no_hand_rolled_client_ip_extraction():
    """Guard against install_routes.py growing a SECOND, hand-rolled
    client-IP extraction that bypasses the trusted-CIDR check now that the
    canonical path lives in record_install_with_provenance. install_routes.py
    is allowed to have no client-IP logic at all (it delegates), but if it
    ever imports _real_client_ip directly again, it must still gate on
    settings.TRUSTED_PROXY_CIDRS rather than trusting headers unconditionally."""
    import inspect
    import app.install_routes as install_routes_module

    src = inspect.getsource(install_routes_module)
    if "_real_client_ip" in src:
        assert "settings.TRUSTED_PROXY_CIDRS" in src, (
            "install_routes.py calls _real_client_ip directly but does not "
            "pass settings.TRUSTED_PROXY_CIDRS -- this would silently trust "
            "spoofable headers (Issue #22)"
        )


# ── Behavioural spoofing tests ────────────────────────────────────────────
#
# A source-text assertion cannot prove the trusted-CIDR check actually
# gates anything -- it can only prove a string is present. These tests
# call the REAL _real_client_ip with a spoofed X-Forwarded-For header and
# assert on the RESOLVED VALUE, which is the property Issue #22 actually
# cares about and which no `assert "..." in src` check could ever verify.

UNTRUSTED_PEER = "10.0.0.9"
SPOOFED_XFF = "1.2.3.4"


def test_real_client_ip_rejects_spoofed_xff_from_untrusted_peer():
    """Untrusted peer + spoofed X-Forwarded-For + empty trusted-CIDR list
    -> the spoofed header MUST NOT win; the raw socket peer is returned."""
    req = _make_mock_request(UNTRUSTED_PEER, xff=SPOOFED_XFF)
    result = _real_client_ip(req, [])
    assert result == UNTRUSTED_PEER, (
        f"spoofed X-Forwarded-For {SPOOFED_XFF!r} was honoured from an "
        f"untrusted peer -- got {result!r}, expected raw peer {UNTRUSTED_PEER!r}"
    )
    assert result != SPOOFED_XFF


def test_real_client_ip_honours_xff_from_trusted_peer():
    """Mirror case: when the peer IS inside the trusted-CIDR list, the
    forwarded header IS honoured."""
    trusted_peer = "173.245.48.5"
    req = _make_mock_request(trusted_peer, xff=SPOOFED_XFF)
    result = _real_client_ip(req, ["173.245.48.0/20"])
    assert result == SPOOFED_XFF, (
        f"trusted peer's X-Forwarded-For was not honoured -- got {result!r}, "
        f"expected {SPOOFED_XFF!r}"
    )
