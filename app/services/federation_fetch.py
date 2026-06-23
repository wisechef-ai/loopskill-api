"""Federation security spine — SSRF-guarded HTTP + path-safety + license gate.

superset_0606 Phase A. Everything downstream (Phase C provider-facet adapter,
Phase D giants crawl, every origin SKILL.md resolver) fetches arbitrary content
from ~110k untrusted origins. The guards land HERE, BEFORE any new origin fetch,
exactly as the Hermes Skills Hub ordered it (tools/skills_hub.py). Ported
faithfully and self-contained — recipes-api cannot import from the Hermes tree.

Three responsibilities, kept in one place (no-redundant-concepts rule):

  1. ``guarded_get`` — the SSRF + redirect-target-revalidation HTTP fetch. Port
     of Hermes ``_guarded_http_get`` + ``is_safe_url``. Every federation origin
     fetch routes through it. Blocks cloud-metadata IPs, link-local, private,
     loopback, CGNAT, and re-validates EVERY redirect hop (manual follow, 5-hop
     cap). Fails closed on DNS failure / parse error.

  2. ``normalize_install_leaf`` — path-safety for the on-disk install leaf a
     federated skill materializes to. Port of Hermes
     ``_normalize_lock_install_path``: rejects empty / "." / absolute / ".."
     traversal / symlink-bearing / shared-root paths so a poisoned external slug
     can never make a later ``rmtree`` escape the skills root.

  3. ``resolve_license`` — the 4-step license-resolution order (decision #13):
     skill-dir LICENSE.txt → repo-root LICENSE → SKILL.md frontmatter `license:`
     → none. Returns ``(license_id, redistributable)``. ``redistributable`` is
     True ONLY for an explicit redistribution-permitting license (MIT / Apache /
     BSD / CC-BY / ISC / MPL / Unlicense / 0BSD / Zlib). Unknown / absent /
     source-available → False → the caller deep-links (installable=false).

Security floor is NON-NEGOTIABLE: cloud-metadata endpoints are always blocked,
there is no allow-private toggle in this surface — federation only ever fetches
public catalogs, so private resolution is always an attack.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────── SSRF policy ────────────────────────────────

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_FETCH_REDIRECTS = 5
_DEFAULT_TIMEOUT_S = 20.0

# Cloud-metadata hostnames — always blocked, no toggle (Hermes parity).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
    }
)

# Cloud-metadata / credential endpoints + the link-local range they live in.
# IPv4-mapped IPv6 variants included (resolvers may return ::ffff:x.x.x.x).
_ALWAYS_BLOCKED_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/DO/Oracle metadata
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata (task IAM creds)
        ipaddress.ip_address("169.254.169.253"),  # Azure IMDS wire server
        ipaddress.ip_address("fd00:ec2::254"),  # AWS metadata (IPv6)
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
        ipaddress.ip_address("::ffff:169.254.169.254"),
        ipaddress.ip_address("::ffff:169.254.170.2"),
        ipaddress.ip_address("::ffff:169.254.169.253"),
        ipaddress.ip_address("::ffff:100.100.100.200"),
    }
)
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
)

# 100.64.0.0/10 (CGNAT / RFC 6598) is NOT covered by ipaddress.is_private.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the IP should be blocked for SSRF protection (Hermes parity)."""
    # IPv4-mapped IPv6 (::ffff:x.x.x.x) → check the embedded IPv4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        e = ip.ipv4_mapped
        return (
            e.is_private
            or e.is_loopback
            or e.is_link_local
            or e.is_reserved
            or e.is_multicast
            or e.is_unspecified
            or e in _CGNAT_NETWORK
        )
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def is_safe_url(url: str) -> bool:
    """Return True iff ``url`` does not target a private/internal/metadata host.

    Resolves the hostname to every IP it answers and checks each against the
    block policy. Fails CLOSED: unsupported scheme, missing host, DNS failure,
    and any unexpected error all return False. No allow-private toggle — the
    federation surface only ever fetches public catalogs.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            logger.warning("federation_fetch: blocked unsupported scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("federation_fetch: blocked internal hostname: %s", hostname)
            return False

        # A bare IP literal in the host must be checked directly — getaddrinfo
        # would echo it, but checking here keeps the intent explicit.
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None and (
            literal in _ALWAYS_BLOCKED_IPS
            or any(literal in n for n in _ALWAYS_BLOCKED_NETWORKS)
            or _is_blocked_ip(literal)
        ):
            logger.warning("federation_fetch: blocked IP-literal host: %s", hostname)
            return False

        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("federation_fetch: blocked — DNS resolution failed for %s", hostname)
            return False

        for _family, _type, _proto, _canon, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip in _ALWAYS_BLOCKED_IPS or any(ip in n for n in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning("federation_fetch: blocked metadata address %s -> %s", hostname, ip_str)
                return False
            if _is_blocked_ip(ip):
                logger.warning("federation_fetch: blocked private/internal %s -> %s", hostname, ip_str)
                return False
        return True
    # Rationale: a parse/edge-case error must fail closed, never become an SSRF bypass.
    except Exception:  # noqa: BLE001
        logger.warning("federation_fetch: is_safe_url failed closed for %s", url, exc_info=True)
        return False


def guarded_get(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    """Fetch a URL with SSRF + redirect-target validation (Hermes parity).

    Every redirect hop is re-validated against ``is_safe_url`` BEFORE it is
    followed (manual follow, 5-hop cap) — a 302 to 169.254.169.254 is blocked
    just like a direct request. Returns the final ``Response`` on success, or
    ``None`` on any unsafe target, redirect-limit overflow, or transport error.
    """
    current_url = url
    for _ in range(_MAX_FETCH_REDIRECTS + 1):
        if not is_safe_url(current_url):
            logger.warning("federation_fetch: blocked unsafe URL: %s", current_url)
            return None
        try:
            resp = httpx.get(current_url, timeout=timeout, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            logger.debug("federation_fetch: transport error for %s: %s", current_url, exc)
            return None
        if resp.status_code in _REDIRECT_STATUS_CODES:
            location = resp.headers.get("location")
            if not location:
                return None
            current_url = urljoin(current_url, location)
            continue
        return resp
    logger.warning("federation_fetch: redirect limit exceeded for %s", url)
    return None


# ─────────────────────────────── Path safety ────────────────────────────────

# A single path component: no separators, no traversal, no NUL, no leading dot
# that could resolve to "." / "..". Mirrors Hermes _normalize_bundle_path intent.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalize_install_leaf(leaf: str) -> str:
    """Validate the on-disk install leaf a federated skill materializes to.

    Port of Hermes ``_normalize_lock_install_path`` intent for our flat install
    layout (``~/.claude/skills/<leaf>/SKILL.md``). The ``<leaf>`` is the ONLY
    attacker-influenced path segment, so it must be a single safe component:

      - non-empty, not "." / ".."
      - no path separators ("/" or "\\")
      - no NUL bytes
      - not absolute
      - matches ``^[A-Za-z0-9][A-Za-z0-9._-]*$``

    Raises ``ValueError`` on any violation so the caller refuses the install
    rather than letting a later ``rmtree`` / write escape the skills root.
    """
    raw = (leaf or "").strip()
    if not raw or raw in {".", ".."}:
        raise ValueError(f"Unsafe install leaf: {leaf!r}")
    if "\x00" in raw:
        raise ValueError(f"Unsafe install leaf (NUL byte): {leaf!r}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"Unsafe install leaf (path separator): {leaf!r}")
    if raw.startswith(("/", "~")):
        raise ValueError(f"Unsafe install leaf (absolute/home): {leaf!r}")
    if not _SAFE_COMPONENT_RE.match(raw):
        raise ValueError(f"Unsafe install leaf (bad chars): {leaf!r}")
    return raw


def safe_install_leaf(slug: str) -> str:
    """Derive a safe install-leaf from an external skill slug.

    External slugs are namespaced (``host.com--task``, ``owner/repo--skill``);
    the install leaf is the final token. We take the last ``--`` segment, then
    the last ``/`` segment, then validate. Raises ``ValueError`` if the result
    is not a safe single component (the caller refuses the install).
    """
    leaf = (slug or "").rsplit("--", 1)[-1].rsplit("/", 1)[-1]
    return normalize_install_leaf(leaf)


# ────────────────────────────── License gate ────────────────────────────────

# SPDX ids (and common aliases) that explicitly permit redistribution. The
# install gate sets installable=True ONLY when the resolved license is in here.
_REDISTRIBUTABLE_SPDX = frozenset(
    {
        "mit",
        "apache-2.0",
        "apache2.0",
        "apache 2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "bsd",
        "isc",
        "mpl-2.0",
        "unlicense",
        "0bsd",
        "zlib",
        "cc-by-4.0",
        "cc-by-3.0",
        "cc-by-sa-4.0",
        "cc0-1.0",
        "cc0",
        "wtfpl",
        "postgresql",
        "python-2.0",
    }
)

# Frontmatter license: line, e.g. ``license: MIT`` or ``license: Apache-2.0``.
_FRONTMATTER_LICENSE_RE = re.compile(r"(?im)^\s*license\s*:\s*['\"]?([A-Za-z0-9.\- ]+?)['\"]?\s*$")


def _canon_license(value: str | None) -> str | None:
    """Canonicalize a raw license string/SPDX id to lowercase, trimmed."""
    if not value:
        return None
    v = str(value).strip().lower()
    return v or None


def is_redistributable(license_id: str | None) -> bool:
    """True iff an explicit license permits redistribution (the install gate).

    Unknown / absent / source-available → False (conservative: deep-link). A
    license string that contains a redistributable SPDX token counts (handles
    "Apache-2.0 AND CC-BY-4.0" style compound declarations — NVIDIA's case).
    """
    canon = _canon_license(license_id)
    if canon is None:
        return False
    if canon in _REDISTRIBUTABLE_SPDX:
        return True
    # Compound / annotated declarations: split on common separators and check
    # each token. "apache-2.0 and cc-by-4.0" → both redistributable.
    tokens = re.split(r"[\s/,;]+|\band\b|\bor\b", canon)
    return any(t in _REDISTRIBUTABLE_SPDX for t in tokens if t)


def resolve_license(
    *,
    skill_dir_license: str | None = None,
    repo_root_license: str | None = None,
    skill_md: str | None = None,
) -> tuple[str | None, bool]:
    """Resolve a federated skill's license via the 4-step order (decision #13).

    Order (first hit wins):
      1. skill-dir ``LICENSE.txt`` SPDX (per-skill, e.g. anthropics/openai)
      2. repo-root ``LICENSE`` SPDX (whole-repo, e.g. gstack MIT)
      3. ``SKILL.md`` frontmatter ``license:`` field
      4. none found

    Returns ``(license_id, redistributable)``. ``redistributable`` is True ONLY
    when the resolved license is in the redistributable set; absent/unknown →
    ``(None, False)`` so the caller deep-links (installable=false).
    """
    for candidate in (skill_dir_license, repo_root_license):
        canon = _canon_license(candidate)
        if canon:
            return canon, is_redistributable(canon)
    if skill_md:
        m = _FRONTMATTER_LICENSE_RE.search(skill_md)
        if m:
            canon = _canon_license(m.group(1))
            if canon:
                return canon, is_redistributable(canon)
    return None, False
