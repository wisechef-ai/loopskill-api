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
# {utm_source, utm_medium, utm_campaign, utm_content}. Minted by this repo's
# own /api/auth/{provider}/login initiation routes (see
# auth_routes.py::_set_utm_ctx_cookie) when the caller (the portal, a
# different repo) forwards utm_* query params onto the "Sign in" link —
# mirroring exactly how ?ref= already survives the OAuth round-trip via
# _set_utm_ref_cookie. Absence of this cookie is still a valid, common case
# (a signup with no UTM context, or a portal that hasn't wired the
# query-param passthrough yet); resolve_signup_attribution() degrades
# cleanly (falls through to query params, then to None) when it's missing.
_UTM_CTX_COOKIE_NAME = "recipes_utm_ctx"
_UTM_FIELD_MAX_LEN = 64  # bound any individual free-text utm_* value
_UTM_REF_COOKIE_MAX_LEN = 128  # bound before any further validation
_UTM_CTX_COOKIE_MAX_LEN = 2048  # bound the RAW cookie BEFORE json.loads ever runs
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


def _creator_handle_exists(handle: str, db) -> bool:
    """Check whether ``handle`` is a real ``Creator.handle`` row.

    Mirrors ``app._skill_helpers._resolve_ref_value``'s own Creator lookup
    exactly (same table, same column) so a ``creator:<handle>`` ref cookie
    is judged by the identical grammar/source of truth the cookie-setting
    path uses — not a re-invented, possibly-looser check. Returns False
    (never raises) when ``db`` is absent or the query fails; callers treat
    False as "cannot verify, drop the ref."
    """
    if db is None or not handle:
        return False
    from app.models import Creator

    try:
        return db.query(Creator.id).filter(Creator.handle == handle).first() is not None
    # Rationale: a lookup failure here must never block signup — the caller
    # (resolve_signup_attribution) treats False as "drop this one field."
    except Exception:  # noqa: BLE001
        return False


def resolve_signup_attribution(request, db=None) -> dict | None:
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

    ``db``: optional SQLAlchemy ``Session``. When provided, a
    ``creator:<handle>`` ref cookie is RE-VALIDATED against the live
    ``Creator.handle`` table (see ``_creator_handle_exists``) before being
    trusted — cookies are client-writable, so a stale "already validated
    when set" assumption is a trust bypass (a forger can mint
    ``recipes_utm_ref=creator:anything`` directly, no Creator row required).
    Without ``db`` (e.g. a unit test against a bare fake request), a
    ``creator:`` ref cannot be verified and is dropped rather than trusted
    blind — fail closed, not fail open.

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
    # P2-f: bound the RAW cookie length BEFORE it is ever handed to
    # json.loads. Cookies are fully client-controlled (forgeable, arbitrary
    # size) — parsing an unbounded value is a cheap denial-of-service lever
    # against every signup request. Oversized values are discarded outright,
    # same "drop this one field/signal, never block signup" contract as
    # every other malformed-input path in this module.
    if raw_ctx and len(raw_ctx) > _UTM_CTX_COOKIE_MAX_LEN:
        raw_ctx = None
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
        # if it happened to pass the generic field cleaner.
        if ref is not None and ref not in _UTM_REF_ALLOWLIST:
            if ref.startswith("creator:"):
                # P1-d: a stale comment used to claim "creator:<handle>"
                # cookies were pre-validated against the Creator table at
                # the point _set_utm_ref_cookie originally set them — but
                # the cookie is client-writable at the browser layer with
                # no signature, so a forger can mint
                # `recipes_utm_ref=creator:anything` directly and this
                # resolver has no way to tell that apart from a real one
                # without re-checking. Re-validate against the SAME table
                # _resolve_ref_value (app/_skill_helpers.py) checks; without
                # a db handle the claim is unverifiable and is dropped
                # (fail closed), not trusted blind (fail open).
                handle = ref.split("creator:", 1)[1]
                if not (handle and _creator_handle_exists(handle, db)):
                    ref = None
            else:
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


def stamp_utm_ctx_cookie(
    response,
    *,
    utm_source: str | None,
    utm_medium: str | None,
    utm_campaign: str | None,
    utm_content: str | None,
    secure: bool,
) -> None:
    """money-path-3 P1-c: capture UTM context AT the /login click and carry
    it through the OAuth round-trip via a cookie, so the callback (which
    receives no query params of its own — GitHub/Google strip them) can
    still resolve first-touch attribution.

    This repo does not serve the portal's landing page, so it cannot stamp
    this cookie at the moment a visitor first arrives with ``?utm_source=``
    in the URL — that page load never touches this API. What IS in this
    repo's power is capturing UTM context at the moment the visitor clicks
    "Sign in", the same way ``?ref=`` already survives the round-trip via
    ``app.auth_routes._stamp_referral_cookie`` (WIS-660) and the short-link
    redirector (``app/utm_redirects.py::_set_utm_ref_cookie``) stamps its
    ref cookie at click time. Contract for the portal (documented in the PR
    body): when a page has UTM context (from its own URL, e.g. a landing
    page hit via ?utm_source=twitter), forward utm_source/utm_medium/
    utm_campaign/utm_content as query params onto the "Sign in with
    GitHub/Google" link (``/api/auth/{provider}/login?utm_source=...&ref=
    ...``) exactly as it already must for ``?ref=``. No cookie contract is
    required of the portal — only a query-param passthrough on a link it
    already builds.

    Cookie shape/name matches exactly what ``resolve_signup_attribution``
    reads (``recipes_utm_ctx``, JSON dict) so this producer and that
    consumer stay in lock-step. Short-lived (matches ``oauth_state``/
    ``oauth_next`` in auth_routes.py): this cookie only needs to survive
    the few-second OAuth provider round-trip, not a multi-day
    landing-to-signup gap, since it is set at click time immediately
    before the redirect.
    """
    fields = {
        "utm_source": _clean_utm_field(utm_source),
        "utm_medium": _clean_utm_field(utm_medium),
        "utm_campaign": _clean_utm_field(utm_campaign),
        "utm_content": _clean_utm_field(utm_content),
    }
    if not any(fields.values()):
        return
    response.set_cookie(
        key=_UTM_CTX_COOKIE_NAME,
        value=json.dumps(fields),
        max_age=600,  # matches oauth_state / oauth_next — round-trip only
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def capture_signup_attribution(request, user, db) -> None:
    """money-path-3: persist first-touch UTM/ref attribution, write-once.

    Called from inside the OAuth callback, immediately after
    find_or_create_user_by_github/_by_google.

    WRITE-ONCE (first-touch), ATOMIC (P1-a): uses a conditional
    ``UPDATE ... WHERE id = :id AND signup_attribution IS NULL`` instead of
    a read-then-write ``if user.signup_attribution`` check. Two concurrent
    callbacks for the SAME (brand-new) user both read NULL before either
    writes; a plain read-then-write lets the second writer clobber the
    first writer's genuinely-first touch. The conditional UPDATE makes the
    write-once guarantee atomic at the database layer — whichever request's
    UPDATE lands first flips the row from NULL, and the loser's UPDATE
    matches zero rows (checked via ``rowcount``) and is a deliberate no-op.
    Portable across Postgres and SQLite (plain WHERE clause, no dialect-
    specific locking needed).

    Explicitly checks ``IS NULL`` at the SQL layer (not `if user.signup_
    attribution` in Python) so P2-e is structurally impossible: an empty
    dict ``{}`` is falsy in Python but IS NOT NULL in SQL, so it correctly
    blocks a second write the same as any other already-recorded value.

    SESSION-SAFE ON FAILURE (P1-b): commit failures (constraint violation,
    dropped connection, etc.) are caught HERE and immediately followed by
    ``db.rollback()`` before returning. Attribution capture runs after
    ``find_or_create_user_by_github``/``ensure_referral_code`` have already
    committed the user row, but BEFORE ``create_jwt(user)`` reads user
    attributes — a commit failure left un-rolled-back here would leave the
    session in SQLAlchemy's PendingRollbackError state, and the very next
    attribute access (inside create_jwt) would raise and 500 the entire
    signup. Rolling back here, inside this function, guarantees the session
    is usable again by the time control returns to the caller — the
    caller's own try/except (defense-in-depth, matches the referral-
    processing pattern in auth_routes.py) no longer has to be the one to
    fix the session, it only has to log.
    """
    import logging

    from sqlalchemy import update

    from app.models import User

    logger = logging.getLogger("app.auth_routes")

    attribution = resolve_signup_attribution(request, db=db)
    if not attribution:
        return
    try:
        result = db.execute(
            update(User)
            .where(User.id == user.id, User.signup_attribution.is_(None))
            .values(signup_attribution=attribution)
        )
        db.commit()
    # Rationale: a commit failure here must never poison the session for the
    # rest of the request (create_jwt reads user attributes right after) —
    # roll back immediately so signup can still complete. Attribution
    # capture must never be able to block sign-in.
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Signup attribution commit failed for user %s (rolled back, non-fatal)", user.id)
        return
    if result.rowcount:
        # Keep the in-memory object in sync with what was actually written,
        # in case anything downstream in this request reads it.
        user.signup_attribution = attribution
