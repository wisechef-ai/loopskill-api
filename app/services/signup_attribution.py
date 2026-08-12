"""money-path-3: first-touch UTM/ref attribution capture at signup.

2026-08-12 money-path audit, Fix #3: the short-link redirector
(``app/utm_redirects.py``) already sets the ``recipes_utm_ref`` cookie at the
moment a visitor clicks a social short-link, and ``app/referral.py`` already
captures person-to-person referral codes at signup — but nothing captured
platform/campaign UTM context AT signup itself. The only place ``User.utm_ref``
(``app/models.py``) was ever written was the Stripe subscription webhook,
weeks later at paid-conversion time, sourced from checkout session metadata —
and only if the redirector's cookie happened to still be alive when the user
eventually clicked Upgrade. Post→signup attribution was entirely dark.

This module resolves whatever attribution context is available in the SAME
request that creates the ``User`` row (the OAuth callback,
``app/auth_routes.py``) and is wired in as a write-once, best-effort side
effect — see ``app/auth_routes.py::_capture_signup_attribution``.

Separate module (not folded into ``app/_skill_helpers.py``) specifically to
stay clear of that file's proximity to the 600-line god-object gate
(``tests/test_w0_2_pyfile_size_discipline.py``, THRESHOLD=600, never waived
for new code) — this is new logic, not a natural extension of the skill
helpers, and belongs in its own reviewable, testable unit either way.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from app._skill_helpers import _UTM_COOKIE_NAME, _UTM_REF_ALLOWLIST

# Sibling cookie to _UTM_COOKIE_NAME (recipes_utm_ref), JSON-encoded
# {utm_source, utm_medium, utm_campaign, utm_content}. Nothing in this repo
# mints it yet (no page runs a landing-capture step) — it exists so a future
# landing/redirect surface can stamp granular UTM fields the exact same way
# utm_redirects.py already stamps the ref shortcode, and so a test can
# simulate "the visitor's browser already carries UTM context from an
# earlier page load" without inventing a parallel mechanism. Absence of this
# cookie is the common case today; resolve_signup_attribution() degrades
# cleanly (falls through to query params, then to None) when it's missing.
_UTM_CTX_COOKIE_NAME = "recipes_utm_ctx"
_UTM_FIELD_MAX_LEN = 64  # bound any individual free-text utm_* value
_UTM_REF_COOKIE_MAX_LEN = 128  # bound before any further validation
_UTM_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_utm_field(value: str | None) -> str | None:
    """Validate + bound a single free-text utm_* value.

    Rejects control characters (never store raw garbage sourced from a
    client-controlled cookie or query string), strips surrounding
    whitespace, and truncates to ``_UTM_FIELD_MAX_LEN``. Empty-after-strip
    returns None, never "".
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if _UTM_CONTROL_CHARS_RE.search(value):
        return None
    return value[:_UTM_FIELD_MAX_LEN]


def resolve_signup_attribution(request) -> dict | None:
    """Resolve first-touch UTM/ref attribution for a brand-new signup.

    Precedence (first-touch: cookie wins over the current request's own
    query params, because the cookie was stamped on an EARLIER visit — the
    true first touch — while a ``?utm_*=`` param on the OAuth callback URL
    itself would only ever be a same-request artifact, never a real campaign
    click):

      1. ``recipes_utm_ctx`` cookie (JSON: utm_source/utm_medium/
         utm_campaign/utm_content) — set by an earlier landing-page visit,
         if present.
      2. ``?utm_source=&utm_medium=&utm_campaign=&utm_content=`` query
         params on THIS request, used only when (1) yielded nothing.
      3. ``recipes_utm_ref`` cookie (the platform/creator ref shortcode,
         e.g. "li", "x", "creator:foo") — always captured as the ``ref``
         field independently of which UTM source won above.

    Returns ``None`` when there is nothing to attribute (no cookie, no query
    params, no ref) — never raises. Every value is validated + length-bounded
    via ``_clean_utm_field``; a malformed/oversized/control-char value is
    dropped for that ONE field rather than blocking or corrupting the whole
    result. This function itself never raises past a bad individual field,
    but callers (``app/auth_routes.py``) additionally wrap the call site —
    this is the last line of defense, not the only one; attribution must
    never be able to block sign-in.
    """
    utm_source = utm_medium = utm_campaign = utm_content = None
    source_is_cookie = False

    raw_ctx = request.cookies.get(_UTM_CTX_COOKIE_NAME)
    if raw_ctx:
        try:
            ctx = json.loads(raw_ctx)
        # Rationale: cookie is client-visible/tamperable; malformed JSON must
        # never block signup — fall through to query params instead.
        except (ValueError, TypeError):
            ctx = None
        if isinstance(ctx, dict):
            utm_source = _clean_utm_field(ctx.get("utm_source"))
            utm_medium = _clean_utm_field(ctx.get("utm_medium"))
            utm_campaign = _clean_utm_field(ctx.get("utm_campaign"))
            utm_content = _clean_utm_field(ctx.get("utm_content"))
            if any((utm_source, utm_medium, utm_campaign, utm_content)):
                source_is_cookie = True

    if not source_is_cookie:
        query = request.query_params
        utm_source = _clean_utm_field(query.get("utm_source"))
        utm_medium = _clean_utm_field(query.get("utm_medium"))
        utm_campaign = _clean_utm_field(query.get("utm_campaign"))
        utm_content = _clean_utm_field(query.get("utm_content"))

    raw_ref = request.cookies.get(_UTM_COOKIE_NAME)
    ref = None
    if raw_ref and len(raw_ref) <= _UTM_REF_COOKIE_MAX_LEN:
        ref = _clean_utm_field(raw_ref)
        # _clean_utm_field only bounds length/control-chars; also re-validate
        # against the allowlist shape here so an unvalidated free-text cookie
        # value (cookies are client-writable) never gets stored as a ref even
        # if it happened to pass the generic field cleaner. Creator-namespaced
        # refs ("creator:<handle>") are accepted as-is — they were already
        # validated against the Creator table by _resolve_ref_value
        # (app/_skill_helpers.py) at the point the cookie was originally set.
        if ref is not None and ref not in _UTM_REF_ALLOWLIST and not ref.startswith("creator:"):
            ref = None

    if not any((utm_source, utm_medium, utm_campaign, utm_content, ref)):
        return None

    return {
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": utm_campaign,
        "utm_content": utm_content,
        "ref": ref,
        "captured_at": datetime.now(UTC).isoformat(),
    }
