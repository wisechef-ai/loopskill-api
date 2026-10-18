"""Post-login ``next=`` allow-list — ONE check for the OAuth login and callback legs.

qa1018 C24 / BRK-14: ``/api/auth/{google,github}/login`` stored any ``next=``
(``https://evil.example``, ``//evil.example``) in the ``oauth_next`` cookie.
The callback already refused to redirect there, but the login leg kept the
value. Both legs now call :func:`is_safe_next`, so an off-site value is never
stored and never followed.
"""

from __future__ import annotations

# rev 7.3: /referrals so logged-in users coming from /referrals -> /signin land
# back where they started. Paths that are part of the authed app surface and
# safe to bounce to.
SAFE_NEXT_PREFIXES = (
    "/api/",
    "/library",
    "/skills/",
    "/billing/",
    "/publish",
    "/referrals",
    "/dashboard",
)

_MAX_NEXT_LEN = 2048


def is_safe_next(next_url: str | None) -> bool:
    """True only for a same-site, path-only target on the allow-list.

    Path-only: starts with a single ``/`` (``//host`` is protocol-relative),
    no backslash (browsers treat ``/\\host`` as ``//host``), no whitespace or
    control characters (header splitting / ``\\t`` stripping tricks), and one
    of :data:`SAFE_NEXT_PREFIXES`.
    """
    if not next_url or len(next_url) > _MAX_NEXT_LEN:
        return False
    if not next_url.startswith("/") or next_url.startswith("//"):
        return False
    if "\\" in next_url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in next_url):
        return False
    return any(next_url.startswith(p) for p in SAFE_NEXT_PREFIXES)
