"""Trusted-proxy-aware real-client-IP extraction (Issue #12).

Only honour CF-Connecting-IP / X-Forwarded-For when the direct TCP
peer (request.client.host) is in one of the ``trusted_cidrs`` CIDRs.
If the request arrives from an untrusted peer, return the raw socket
IP so spoofed headers cannot hijack rate-limit buckets or telemetry.
"""

import ipaddress
from typing import Sequence

from starlette.requests import Request


def _real_client_ip(
    request: Request,
    trusted_cidrs: Sequence[str],
) -> str:
    """Return the real visitor IP, honouring proxy headers only from trusted CIDR peers.

    Logic (Issue #12):
    1. Determine the direct TCP peer: ``request.client.host`` (fallback "unknown").
    2. If that peer address is covered by any CIDR in *trusted_cidrs*:
       a. Return ``CF-Connecting-IP`` if present and non-empty.
       b. Else return the first token of ``X-Forwarded-For`` if present and non-empty.
    3. Else return the raw socket IP (direct connection, headers not trusted).
    """
    raw_host: str = request.client.host if request.client else "unknown"

    if _is_trusted(raw_host, trusted_cidrs):
        # Peer is a trusted proxy — honour the forwarded headers.
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first

    return raw_host


def _is_trusted(host: str, trusted_cidrs: Sequence[str]) -> bool:
    """Return True if *host* falls inside any of *trusted_cidrs*."""
    if not trusted_cidrs or host in ("unknown", ""):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # hostname, not an IP — cannot match a CIDR
    for cidr in trusted_cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if addr in network:
                return True
        except ValueError:
            continue
    return False
