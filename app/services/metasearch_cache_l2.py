"""Redis L2 tier for the metasearch SWR cache (unisearch_0709 P1).

``metasearch_cache.py`` owns the cache semantics (freshness state machine,
single-flight, LRU). This module owns everything that touches the WIRE: the
versioned key format, the JSON payload codec, the sanitiser that runs before any
bytes are shared fleet-wide, and the thin Redis client wrapper that never lets a
Redis failure reach a caller. It lives apart from the cache so neither file goes
near the repo's 600-line god-object cap.

Why a shared tier at all: the in-process cache is per-worker, so a result
computed by worker A was invisible to worker B. The MCP search path (P2) reads
the cache ONLY — it never fans out — so on a per-worker cache it would answer
"cold" for a query the REST route had warmed one worker over.

Three invariants this module exists to hold:

1. **Absolute epoch.** ``computed_at`` is unix seconds. ``time.monotonic()`` is
   per-process and meaningless the moment a value crosses a process boundary.
2. **Sanitise before sharing.** A shared cache is a fleet-wide blast radius: one
   poisoned row would be served to every worker until its TTL. Rows are
   field-capped, string-capped, count-capped and version-tagged on the way IN,
   and re-capped on the way OUT (another writer's bytes are never trusted).
3. **Never raise, never block.** Redis unreachable — including the 30s
   ``app.middleware.get_redis()`` backoff window, where it returns ``None`` — is
   an honest ``degraded`` state, not an exception and not a blocking wait.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Versioned key prefix: a payload-schema change bumps the version and old
# entries are simply never read again (they TTL out). Schema drift can never
# make a new reader misparse an old writer's bytes.
KEY_PREFIX = "loopskill:metasearch:v1:"
SEQ_KEY = "loopskill:metasearch:v1:seq"
PAYLOAD_VERSION = 1

# Field caps. Generous enough that a legitimate metasearch row is untouched,
# tight enough that a hostile upstream cannot blow up every worker's memory or
# an agent's context window.
MAX_ROWS = 100  # rows per cached query (the fan-out itself caps well below this)
MAX_FIELDS = 40  # keys per row
MAX_KEY_LEN = 64  # length of a row's field name
MAX_STR_LEN = 2_000  # length of any string value
MAX_LIST_LEN = 20  # elements in a list value
MAX_SOURCES = 20  # entries in sources_ok / sources_degraded
MAX_PAYLOAD_BYTES = 256 * 1024  # hard ceiling on one cached entry

# ONE Lua script, both write modes, atomic on the server:
#   ARGV[4] == ""  → foreground put: land only if strictly newer (monotonic guard)
#   ARGV[4] != ""  → SWR refresh: land only if the stored entry is STILL the one
#                    this refresh started from (compare-and-set on seq)
# A missing entry fails a CAS refresh, mirroring the L1 semantics: the refresh
# result is dropped and the next request recomputes.
_CAS_SCRIPT = """
local cur = redis.call('GET', KEYS[1])
local expected = ARGV[4]
if cur then
  local ok, decoded = pcall(cjson.decode, cur)
  local stored = -1
  if ok and type(decoded) == 'table' and decoded.seq then
    stored = tonumber(decoded.seq)
  end
  if expected ~= '' then
    if stored ~= tonumber(expected) then return 0 end
  elseif stored >= tonumber(ARGV[2]) then
    return 0
  end
elseif expected ~= '' then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[3]))
return 1
"""


def redis_key(cache_key: str) -> str:
    """Map the cache's ``{normalized_query}|{sorted_sources}`` key into Redis."""
    return f"{KEY_PREFIX}{cache_key}"


# ── Sanitising codec ─────────────────────────────────────────────────────────


def _clean_str(value: str) -> str:
    # NULs and control characters have no place in a display string and can
    # confuse downstream consumers (logs, terminals, JSON tooling).
    stripped = "".join(ch for ch in value[:MAX_STR_LEN] if ch == "\n" or ch >= " ")
    return stripped[:MAX_STR_LEN]


def _clean_scalar(value: Any) -> Any | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, (int, float)):
        return value
    return None


def _clean_value(value: Any, *, depth: int = 0) -> Any | None:
    """Cap one field value. Returns None for anything not safely serialisable."""
    scalar = _clean_scalar(value)
    if scalar is not None or value is None:
        return scalar
    if isinstance(value, (list, tuple)):
        if depth >= 1:
            return None
        out = [_clean_value(v, depth=depth + 1) for v in list(value)[:MAX_LIST_LEN]]
        return [v for v in out if v is not None]
    if isinstance(value, dict):
        if depth >= 1:
            return None
        return _clean_row(value, depth=depth + 1)
    return None


def _clean_row(row: dict[Any, Any], *, depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if len(out) >= MAX_FIELDS:
            break
        if not isinstance(key, str) or len(key) > MAX_KEY_LEN:
            continue
        cleaned = _clean_value(value, depth=depth)
        if cleaned is None and value is not None:
            continue  # unserialisable (object, deep nesting) → dropped, never stored raw
        out[_clean_str(key)] = cleaned
    return out


def sanitize_skills(skills: Any) -> list[dict[str, Any]]:
    """Cap + clean a result list so it is safe to share with every worker.

    Non-dict rows are REJECTED outright (a malformed row is not repairable); the
    surviving rows are field-capped. Applied at ``put()`` AND at read, because a
    shared tier's bytes may have been written by a process we do not control.
    """
    if not isinstance(skills, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in skills[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        rows.append(_clean_row(row))
    return rows


def _clean_sources(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_clean_str(v) for v in values[:MAX_SOURCES] if isinstance(v, str)]


def encode_payload(
    *,
    skills: Any,
    sources_ok: Any,
    sources_degraded: Any,
    computed_at: float,
    ttl_s: float,
    stale_grace_s: float,
    seq: int,
    state: str = "fresh",
) -> str | None:
    """Serialise one cache entry. Returns None if it cannot be made small enough.

    ``state`` is the writer's own verdict at write time (always ``fresh``) and is
    advisory provenance only — every reader recomputes freshness from
    ``computed_at`` against its own clock.
    """
    payload = {
        "v": PAYLOAD_VERSION,
        "skills": sanitize_skills(skills),
        "sources_ok": _clean_sources(sources_ok),
        "sources_degraded": _clean_sources(sources_degraded),
        "computed_at": float(computed_at),
        "ttl_s": float(ttl_s),
        "stale_grace_s": float(stale_grace_s),
        "seq": int(seq),
        "state": state,
    }
    encoded = json.dumps(payload, separators=(",", ":"), default=str)
    while len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES and payload["skills"]:
        # Drop from the tail: the list is ranked, so the least valuable rows go
        # first. An entry that cannot fit even empty is not written at all.
        payload["skills"] = payload["skills"][: len(payload["skills"]) // 2]
        encoded = json.dumps(payload, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        logger.warning("metasearch cache payload too large to share; skipping L2 write")
        return None
    return encoded


def decode_payload(raw: str | bytes | None) -> dict[str, Any] | None:
    """Parse + re-sanitise a shared payload. Returns None for anything we cannot
    trust: unparseable bytes, a different schema version, or a wrong-typed field.
    A None here is a MISS, never an exception — poisoned bytes must not 500 a
    reader, and they must not be served either."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    # Rationale: any unparseable shared payload is treated as a cache miss —
    # a poisoned/truncated value must never propagate as an exception.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch cache payload unparseable; treating as miss")
        return None
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
        return None
    if not isinstance(payload.get("skills"), list):
        return None
    try:
        return {
            "skills": sanitize_skills(payload["skills"]),
            "sources_ok": _clean_sources(payload.get("sources_ok")),
            "sources_degraded": _clean_sources(payload.get("sources_degraded")),
            "computed_at": float(payload["computed_at"]),
            "ttl_s": float(payload["ttl_s"]),
            "stale_grace_s": float(payload["stale_grace_s"]),
            "seq": int(payload["seq"]),
        }
    # Rationale: a wrong-typed field from another writer is a miss, not a 500.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch cache payload malformed; treating as miss")
        return None


# ── Redis wrapper ────────────────────────────────────────────────────────────


class RedisL2:
    """The shared tier. Every method returns an explicit ``ok`` flag instead of
    raising, so the cache can report ``degraded`` rather than fail a request.

    ``client_factory`` defaults to ``app.middleware.get_redis`` — imported LATE
    so ``patch("app.middleware.get_redis")`` is honoured at call time, and so a
    process that never uses the cache never opens a connection. That factory
    returns ``None`` both when Redis is unconfigured and during its 30s failure
    backoff; ``None`` is treated as unavailable, never as an empty cache.
    """

    def __init__(self, client_factory: Any = None) -> None:
        self._client_factory = client_factory

    def _client(self) -> Any:
        factory = self._client_factory
        if factory is None:
            from app import middleware as _mw

            factory = _mw.get_redis
        try:
            return factory()
        # Rationale: client construction/health-check failures degrade to
        # L1-only; the caller must never see a Redis error.
        except Exception:  # noqa: BLE001
            logger.warning("metasearch cache: redis client unavailable", exc_info=True)
            return None

    def next_seq(self) -> tuple[bool, int | None]:
        """Allocate the next SHARED write generation (Redis ``INCR``).

        The seq must be fleet-global: with a per-process counter, worker A's
        stale refresh carries a number that means nothing to worker B, and the
        compare-and-set degenerates into "last writer wins".
        """
        client = self._client()
        if client is None:
            return False, None
        try:
            return True, int(client.incr(SEQ_KEY))
        # Rationale: a failed INCR degrades to the local counter (L1-only mode).
        except Exception:  # noqa: BLE001
            logger.warning("metasearch cache: shared seq INCR failed", exc_info=True)
            return False, None

    def read(self, key: str) -> tuple[bool, dict[str, Any] | None]:
        """Return ``(reachable, payload)``. ``(True, None)`` is a real miss;
        ``(False, None)`` means we could not look — the caller reports degraded."""
        client = self._client()
        if client is None:
            return False, None
        try:
            return True, decode_payload(client.get(key))
        # Rationale: a read failure degrades to L1-only; never raise to a caller.
        except Exception:  # noqa: BLE001
            logger.warning("metasearch cache: redis read failed", exc_info=True)
            return False, None

    def write(
        self, key: str, payload: str | None, *, seq: int, ttl_s: float, expected_seq: int | None = None
    ) -> tuple[bool, bool]:
        """Compare-and-set write. Returns ``(reachable, landed)``.

        The Redis key TTL is ``ttl_s + stale_grace_s`` (the caller passes the
        sum), so the shared tier keeps an entry for exactly as long as it is
        servable — fresh window plus stale window — and Redis expires it for us.
        TTL eviction, never LRU eviction, on this side.
        """
        if payload is None:
            return True, False
        client = self._client()
        if client is None:
            return False, False
        try:
            landed = client.eval(
                _CAS_SCRIPT,
                1,
                key,
                payload,
                str(int(seq)),
                str(max(1, int(ttl_s))),
                "" if expected_seq is None else str(int(expected_seq)),
            )
            return True, bool(int(landed))
        # Rationale: a failed shared write degrades to L1-only — the entry is
        # still cached in-process and the caller is told the tier is degraded.
        except Exception:  # noqa: BLE001
            logger.warning("metasearch cache: redis write failed", exc_info=True)
            return False, False

    def drop_matching(self, pattern: str = "*") -> bool:
        """Delete every entry whose cache key matches ``pattern`` (admin/test
        reset, or one query's worth). Returns reachability.

        SCAN, never KEYS — a blocking KEYS on a shared production Redis is how
        you take out every other consumer on the box. The shared sequence
        counter is never deleted: it must keep advancing across invalidations or
        a post-reset write could reuse a seq an in-flight refresh is comparing
        against.
        """
        client = self._client()
        if client is None:
            return False
        try:
            keys = [k for k in client.scan_iter(match=f"{KEY_PREFIX}{pattern}") if k != SEQ_KEY]
            if keys:
                client.delete(*keys)
            return True
        # Rationale: best-effort reset; a down shared tier is not a caller error.
        except Exception:  # noqa: BLE001
            logger.warning("metasearch cache: redis scan/delete failed", exc_info=True)
            return False
