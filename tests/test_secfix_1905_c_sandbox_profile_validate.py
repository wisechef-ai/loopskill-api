"""Tests for Issue #9 — SandboxProfile.validate rejects dangerous IP literals.

TDD structure:
  test_pov_* — proof-of-vulnerability (pre-fix).
  test_*     — regression tests that pass after fix.
"""
from __future__ import annotations

import pytest

from app.sandbox.profile import SandboxProfile

pytestmark = [pytest.mark.sandbox_linux_only]


# ---------------------------------------------------------------------------
# PROOF OF VULNERABILITY (#9) — These show the old regex accepted bad IPs.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_host", [
    "169.254.169.254",   # IMDS / link-local
    "127.0.0.1",         # loopback
    "localhost",         # loopback by name
    "10.0.0.1",          # RFC-1918 private
    "192.168.1.1",       # RFC-1918 private
    "172.16.0.1",        # RFC-1918 private
    "::1",               # IPv6 loopback
    "224.0.0.1",         # multicast
    "0.0.0.0",           # unspecified
])
def test_pov_bad_ip_accepted_by_regex(bad_host):
    """POV #9: Before fix, the simple regex accepts these dangerous hosts.

    After the fix validate() raises ValueError; we skip the PoV assertion.
    """
    profile = SandboxProfile(network_allow=[bad_host])
    try:
        profile.validate()
    except ValueError:
        pytest.skip("Fix already applied — validate() now raises for bad IPs")
    # Pre-fix: validate() just returns warnings without raising.
    # The test passing here documents the vulnerability.
    warnings = profile.validate()
    # Old code only checks the regex r'^[a-zA-Z0-9._-]+$' — these pass
    assert isinstance(warnings, list), "Pre-fix: validate() returns a list without raising"


# ---------------------------------------------------------------------------
# REGRESSION TESTS — Fail on broken code, pass after fix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_ip", [
    "169.254.169.254",   # IMDS / link-local
    "127.0.0.1",         # loopback
    "::1",               # IPv6 loopback
    "10.0.0.1",          # private
    "192.168.1.1",       # private
    "172.16.0.1",        # private
    "224.0.0.1",         # multicast
    "0.0.0.0",           # unspecified
    "100.64.0.1",        # shared address space (CGNAT)
])
def test_validate_rejects_bad_ip_literals(bad_ip):
    """Issue #9 fix: dangerous IP literals raise ValueError."""
    profile = SandboxProfile(network_allow=[bad_ip])
    with pytest.raises(ValueError, match=r"[Dd]isallowed"):
        profile.validate()


def test_validate_rejects_localhost_hostname():
    """Issue #9 fix: 'localhost' as a hostname raises ValueError."""
    profile = SandboxProfile(network_allow=["localhost"])
    with pytest.raises(ValueError, match="localhost"):
        profile.validate()


@pytest.mark.parametrize("good_host", [
    "api.github.com",
    "registry.npmjs.org",
    "pypi.org",
    "example.com",
    "sub.domain.example.com",
    "openai.com",
    "huggingface.co",
])
def test_validate_accepts_valid_public_hostnames(good_host):
    """Issue #9 fix: valid public hostnames pass validation without raising."""
    profile = SandboxProfile(network_allow=[good_host])
    warnings = profile.validate()
    # Should not raise, should not produce a warning for this domain
    suspicious = [w for w in warnings if good_host in w and "Suspicious" in w]
    assert not suspicious, f"Unexpected warning for valid host {good_host!r}: {warnings}"


@pytest.mark.parametrize("bad_hostname", [
    "-starts-with-hyphen.com",     # leading hyphen label
    "ends-with-hyphen-.com",       # trailing hyphen label
    "label" + "x" * 64 + ".com",  # label > 63 chars
    "x" * 254 + ".com",           # total > 253 chars
    "has space.com",               # space not allowed
    "has@at.com",                  # @ not allowed
    "has!bang.com",                # ! not allowed
])
def test_validate_rejects_malformed_hostnames(bad_hostname):
    """Issue #9 fix: hostnames violating RFC 1035 raise ValueError."""
    profile = SandboxProfile(network_allow=[bad_hostname])
    with pytest.raises(ValueError):
        profile.validate()


def test_validate_imds_endpoint_raises():
    """Explicit IMDS regression — the original issue trigger."""
    profile = SandboxProfile(network_allow=["169.254.169.254"])
    with pytest.raises(ValueError, match=r"[Dd]isallowed"):
        profile.validate()


def test_validate_multiple_hosts_one_bad():
    """Mixed list: one bad host poisons the whole validation."""
    profile = SandboxProfile(
        network_allow=["api.github.com", "169.254.169.254", "pypi.org"]
    )
    with pytest.raises(ValueError):
        profile.validate()


def test_validate_empty_network_allow_passes():
    """Empty network_allow is valid (isolated network)."""
    profile = SandboxProfile(network_allow=[])
    warnings = profile.validate()
    assert isinstance(warnings, list)
