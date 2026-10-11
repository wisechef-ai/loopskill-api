"""perclient_1110 — page-view beacon helpers (parse, sanitize, classify, limit).

app.loopskill.io has no access log, so the static portal fires a same-origin
beacon (``POST /api/pv``, see app/pageview_routes.py) and we persist one
``telemetry_events`` row per qualified view. Everything here is pure and
side-effect free except the bounded in-process rate limiter.

Privacy stance: the client IP is stored exactly like every other
telemetry_events row; no cookies are read or set, no fingerprinting, and the
referrer is reduced to its HOST before storage. Query strings and fragments
are stripped from the path so a URL-borne email/token is never persisted.

Junk never yields an error: every reject path returns ``None`` and the route
answers 204 regardless, so the endpoint is not an oracle for what it accepts.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from app.services.funnel_ledger import classify

#: Only these portal sections are counted. Keep it a constant — widening it is
#: a deliberate, reviewed change.
ALLOWED_PATH_PREFIXES: tuple[str, ...] = ("/use-cases/", "/blog/")

MAX_BODY_BYTES = 2048
MAX_PATH_LEN = 200
MAX_UTM_LEN = 64
MAX_REF_HOST_LEN = 120
UTM_FIELDS: tuple[str, ...] = ("utm_source", "utm_medium", "utm_campaign", "utm_content")

BOT_UA_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|headless|curl|wget|python-requests|httpx|go-http",
    re.IGNORECASE,
)

#: Per-IP ceiling on recorded views. Over the limit → silent 204, no row.
RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW_S = 60.0
#: Hard bound on tracked IPs so the limiter cannot grow without limit.
RATE_LIMIT_MAX_KEYS = 10_000

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}


def reset_rate_limiter() -> None:
    """Test hook — forget every tracked IP."""
    with _lock:
        _hits.clear()


def allow(ip_key: str, now: float | None = None) -> bool:
    """Sliding-window check-and-record for one IP. True → may write a row."""
    now = time.monotonic() if now is None else now
    cutoff = now - RATE_LIMIT_WINDOW_S
    with _lock:
        window = _hits.get(ip_key)
        if window is None:
            if len(_hits) >= RATE_LIMIT_MAX_KEYS:
                _evict_idle(cutoff)
                if len(_hits) >= RATE_LIMIT_MAX_KEYS:
                    _hits.clear()  # bounded memory beats perfect accounting
            window = _hits[ip_key] = deque()
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= RATE_LIMIT_MAX:
            return False
        window.append(now)
        return True


def _evict_idle(cutoff: float) -> None:
    for key in [k for k, w in _hits.items() if not w or w[-1] <= cutoff]:
        del _hits[key]


def _clip(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _clean_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.split("?", 1)[0].split("#", 1)[0].strip()
    if not path.startswith(ALLOWED_PATH_PREFIXES):
        return None
    if any(ord(ch) < 32 for ch in path):
        return None
    return path[:MAX_PATH_LEN]


def _ref_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return host.lower()[:MAX_REF_HOST_LEN]


def parse_beacon(raw: bytes | None) -> dict[str, Any] | None:
    """Decode + validate a beacon body. ``None`` = junk, write nothing."""
    if not raw or len(raw) > MAX_BODY_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    path = _clean_path(body.get("path"))
    if path is None:
        return None
    out: dict[str, Any] = {"path": path}
    for field in UTM_FIELDS:
        out[field] = _clip(body.get(field), MAX_UTM_LEN)
    out["ref_host"] = _ref_host(body.get("referrer"))
    return out


def is_bot(user_agent: str | None) -> bool:
    return bool(user_agent) and bool(BOT_UA_RE.search(user_agent))


def build_payload(beacon: dict[str, Any], *, client_ip: str | None, user_agent: str | None) -> str:
    """Final JSON payload for the telemetry_events row.

    ``class`` reuses funnel_ledger.classify (the one fleet/stranger/unknown
    classifier); a bot UA always forces ``unknown`` so crawlers can never
    count as stranger traffic.
    """
    ua_bot = is_bot(user_agent)
    klass = "unknown" if ua_bot else classify(ip=client_ip)[0]
    return json.dumps({**beacon, "ua_bot": ua_bot, "class": klass}, separators=(",", ":"))
