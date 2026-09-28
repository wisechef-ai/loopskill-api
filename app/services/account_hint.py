"""pricing0928 (t_7f5808d2, option C) — the one account line on the install path.

Q1 of the 2026-09-28 pricing reality check: 93 anonymous installs in 30 days
and 0 signups. The only prompt to sign in was a web-only "Add to bundle"
button, and agent traffic never sees it. This module gives an anonymous
install ONE honest, agent-readable sentence it can relay to its human.

It does not gate anything. Anonymous installs work exactly as before, and a
caller that is already signed in gets no hint.

Copy rules (claim-grounded, checked against the code on 2026-09-28):
* "save it to a bundle": /library, bundle add. Free includes private bundles.
* "keep it in sync across your agents": ``loopskill_sync`` / fleet sync update
  bundle members to their latest published version.
* NOT "breakage alerts". Skill-error reports route to the skill's creator, and
  nothing notifies the installer, so saying it would be a lie.

Attribution uses the signup UTM channel that already survives the OAuth hop
(``app/services/signup_attribution.py``: utm_* on /api/auth/*/login is stamped
into the ``recipes_utm_ctx`` cookie and written to ``User.utm_*`` on the
callback). ``?ref=`` is NOT used: it is the person-to-person referral code
(WIS-660), and /signin drops anything not matching ``^[A-Za-z0-9]{4,16}$``,
so ``ref=install:<slug>`` would never reach the server.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from app import config

UTM_SOURCE = "install"
UTM_MEDIUM = "agent"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def signin_url(slug: str) -> str:
    base = config.public_origin().rstrip("/")
    campaign = quote(slug, safe="") if _SLUG_RE.match(slug or "") else "unknown"
    return (
        f"{base}/signin?next=/library&utm_source={UTM_SOURCE}&utm_medium={UTM_MEDIUM}&utm_campaign={campaign}"
    )


def account_hint(slug: str, auth_ctx=None) -> str | None:
    """The one-line hint for an ANONYMOUS install, or None for a signed-in caller."""
    scope = getattr(auth_ctx, "scope", None)
    if scope not in (None, "anonymous"):
        return None
    return (
        f"Optional, free: sign in to save '{slug}' to a bundle and keep it in sync "
        f"across your agents: {signin_url(slug)} (installing never needs an account)."
    )
