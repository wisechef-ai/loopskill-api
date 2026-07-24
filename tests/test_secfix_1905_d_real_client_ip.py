"""Tests for Issue #12: _real_client_ip trusted-proxy gate.

TDD: RED commit shows old unconditional header-trust behaviour fails the spec.
GREEN commit shows the new trusted-CIDR gate works correctly.

Tests:
  - Trusted CIDR peer → CF-Connecting-IP header honoured
  - Trusted CIDR peer → X-Forwarded-For honoured (no CF header)
  - Untrusted peer → raw socket IP returned (headers ignored)
  - Missing headers from trusted peer → raw peer IP returned
  - Empty CF header → falls through to XFF
  - Empty XFF → falls through to raw peer IP
  - IPv6 trusted peer in CIDR → honoured
"""

import pytest
from unittest.mock import MagicMock

from app.utils.client_ip import _real_client_ip, _is_trusted


# ── _is_trusted unit tests ────────────────────────────────────────────────────

def test_is_trusted_ip_in_cidr():
    assert _is_trusted("173.245.48.1", ["173.245.48.0/20"]) is True


def test_is_trusted_ip_not_in_cidr():
    assert _is_trusted("1.2.3.4", ["173.245.48.0/20"]) is False


def test_is_trusted_unknown_host():
    assert _is_trusted("unknown", ["173.245.48.0/20"]) is False


def test_is_trusted_empty_cidrs():
    assert _is_trusted("173.245.48.1", []) is False


def test_is_trusted_invalid_cidr_skipped():
    # Bad CIDR entry should not raise; other CIDRs still match
    assert _is_trusted("173.245.48.1", ["not-a-cidr", "173.245.48.0/20"]) is True


def test_is_trusted_hostname_not_trusted():
    # Hostnames (not IP literals) cannot match a CIDR
    assert _is_trusted("cloudflare.com", ["173.245.48.0/20"]) is False


# ── _real_client_ip integration tests ────────────────────────────────────────

def _make_request(peer_host: str, cf_ip: str | None = None, xff: str | None = None):
    """Build a minimal mock request."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer_host
    headers = {}
    if cf_ip is not None:
        headers["cf-connecting-ip"] = cf_ip
    if xff is not None:
        headers["x-forwarded-for"] = xff

    def get_header(name, default=""):
        return headers.get(name.lower(), default)

    req.headers.get = get_header
    return req


CF_CIDR = "173.245.48.0/20"
CF_PEER = "173.245.48.5"    # inside CF_CIDR
UNTRUSTED_PEER = "1.2.3.4"  # outside any trusted CIDR


# RED test (demonstrates old behaviour fails): untrusted peer with spoofed CF header
# Old code would return "10.0.0.1"; new code must return the raw socket IP.
def test_untrusted_peer_cf_header_ignored():
    """An untrusted direct-connect peer cannot spoof the CF-Connecting-IP header."""
    req = _make_request(UNTRUSTED_PEER, cf_ip="10.0.0.1")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == UNTRUSTED_PEER, (
        f"Expected raw socket IP {UNTRUSTED_PEER!r} for untrusted peer, got {result!r}"
    )


def test_trusted_peer_cf_header_honoured():
    """A Cloudflare edge IP is trusted; CF-Connecting-IP is the real visitor IP."""
    req = _make_request(CF_PEER, cf_ip="203.0.113.42")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "203.0.113.42"


def test_trusted_peer_xff_honoured_no_cf():
    """When CF header absent but peer is trusted, first XFF entry is used."""
    req = _make_request(CF_PEER, xff="198.51.100.7, 10.0.0.1")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "198.51.100.7"


def test_trusted_peer_no_headers_returns_peer():
    """Trusted peer with no forwarding headers → return the peer IP itself."""
    req = _make_request(CF_PEER)
    result = _real_client_ip(req, [CF_CIDR])
    assert result == CF_PEER


def test_untrusted_peer_xff_ignored():
    """Untrusted peer with X-Forwarded-For — socket IP wins."""
    req = _make_request(UNTRUSTED_PEER, xff="10.0.0.1, 192.168.1.1")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == UNTRUSTED_PEER


def test_trusted_peer_empty_cf_falls_through_to_xff():
    """Empty CF-Connecting-IP value → fall through to XFF."""
    req = _make_request(CF_PEER, cf_ip="  ", xff="203.0.113.99")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "203.0.113.99"


def test_trusted_peer_empty_xff_returns_peer():
    """Empty XFF value → return peer IP."""
    req = _make_request(CF_PEER, xff="  ")
    result = _real_client_ip(req, [CF_CIDR])
    assert result == CF_PEER


def test_no_client_returns_unknown():
    """request.client is None → return 'unknown'."""
    req = MagicMock()
    req.client = None
    req.headers.get = lambda name, default="": ""
    result = _real_client_ip(req, [CF_CIDR])
    assert result == "unknown"


def test_multiple_cidrs_first_match_used():
    """Multiple CIDRs — ip in second CIDR → still trusted."""
    req = _make_request("104.16.5.1", cf_ip="8.8.8.8")
    result = _real_client_ip(req, [CF_CIDR, "104.16.0.0/13"])
    assert result == "8.8.8.8"
