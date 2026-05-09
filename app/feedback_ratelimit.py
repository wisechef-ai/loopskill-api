"""Multi-window rate limiter for feedback endpoints.

Windows (all enforced; any one fail -> block):
  dedup:        identical signature, 2 hits / 7d -> soft-block, return cached issue_url
  per-tool:     10 distinct submissions per tool / 24h -> hard-block
  cross-tool:   30 total / 24h across recipify-request + feedback + skill-error -> hard-block
  loop:         >=3 submissions in 5 min from same identity -> 15 min cooldown
                (can be overridden with force=True + non-empty confirmation)

Identity = api_key_id from middleware; fallback: agent_id; then peer IP.
All state is in-process (no Redis). Production should swap to a shared backend.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Window constants ────────────────────────────────────────────────────────

DEDUP_MAX = 2
DEDUP_WINDOW_S = 7 * 24 * 3600  # 7 days

PER_TOOL_MAX = 10
PER_TOOL_WINDOW_S = 24 * 3600  # 24h

CROSS_TOOL_MAX = 30
CROSS_TOOL_WINDOW_S = 24 * 3600  # 24h

LOOP_THRESHOLD = 3
LOOP_WINDOW_S = 5 * 60   # 5 min
LOOP_COOLDOWN_S = 15 * 60  # 15 min

# skill-error per-tool shares the same per-tool ceiling
SKILL_ERROR_RATE_MAX = 30  # bumped from 20/hr — now part of cross-tool
SKILL_ERROR_WINDOW_S = 3600  # still 1-hour backstop for skill-error

# ── Per-tool overrides ────────────────────────────────────────────────────
#
# Tools listed here override the module-level PER_TOOL_MAX / LOOP_THRESHOLD /
# LOOP_WINDOW_S defaults. All other behaviour (cross-tool ceiling, dedup
# window, cooldown duration) is unchanged.
#
# skill-patch: 1/24h per (identity, slug) — very tight because patches are
# heavier than plain feedback. Loop detector fires at 2 hits in 30 min.
TOOL_OVERRIDES: dict[str, dict] = {
    "skill-patch": {
        "per_tool_max": 1,           # 1 patch per 24h per (identity, slug)
        "loop_threshold": 2,         # loop fires at 2 rapid hits (not 3)
        "loop_window_s": 1800,       # 30-min window for loop detection
    },
}

# ── State stores ────────────────────────────────────────────────────────────

_lock = threading.Lock()

# dedup store: signature -> list of (timestamp, issue_url)
_dedup: dict[str, list[tuple[float, str]]] = defaultdict(list)

# per-tool store: (identity, tool) -> list[float]  (timestamps of submissions)
_per_tool: dict[tuple[str, str], list[float]] = defaultdict(list)

# cross-tool store: identity -> list[float]
_cross_tool: dict[str, list[float]] = defaultdict(list)

# loop store: identity -> list[float]
_loop: dict[str, list[float]] = defaultdict(list)

# cooldown store: identity -> float (expiry monotonic)
_cooldown: dict[str, float] = {}

# skill-error backstop (1-hour): identity -> list[float]
_skill_error_backstop: dict[str, list[float]] = defaultdict(list)


def _purge(ts_list: list[float], window_s: float, now: float) -> list[float]:
    cutoff = now - window_s
    return [t for t in ts_list if t > cutoff]


def _purge_dedup(entries: list, window_s: float, now: float) -> list:
    # Purge (timestamp, url) dedup entries outside the window.
    cutoff = now - window_s
    return [(ts, url) for (ts, url) in entries if ts > cutoff]


def make_signature(*parts: str) -> str:
    """sha256(part1|part2|...) hex."""
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Public API ───────────────────────────────────────────────────────────────

class RateLimitResult:
    __slots__ = (
        "allowed", "deduped", "issue_url", "hard_block", "force_available",
        "last_submissions", "retry_at", "loop_block",
    )

    def __init__(self):
        self.allowed: bool = True
        self.deduped: bool = False
        self.issue_url: str = ""
        self.hard_block: bool = False
        self.force_available: bool = False
        self.last_submissions: list[dict] = []
        self.retry_at: datetime | None = None
        self.loop_block: bool = False


def check_and_record(
    *,
    identity: str,
    tool: str,          # "feedback" | "recipify-request" | "skill-error" | "skill-patch"
    signature: str,
    issue_url: str = "",
    force: bool = False,
    confirmation: str | None = None,
) -> RateLimitResult:
    """Check all windows and record the submission if allowed.

    Returns a RateLimitResult describing the outcome.
    Call BEFORE persisting to DB; only records the hit when allowed=True.

    Per-tool overrides (see TOOL_OVERRIDES) let callers like 'skill-patch'
    use a tighter per-tool ceiling and a shorter loop-detection window without
    changing any behaviour for the existing feedback/recipify-request/skill-error tools.
    """
    result = RateLimitResult()
    now = time.monotonic()
    wall_now = datetime.now(timezone.utc)

    # Resolve per-tool overrides (or fall back to module defaults)
    _overrides = TOOL_OVERRIDES.get(tool, {})
    _per_tool_max = _overrides.get("per_tool_max", PER_TOOL_MAX)
    _loop_threshold = _overrides.get("loop_threshold", LOOP_THRESHOLD)
    _loop_window = _overrides.get("loop_window_s", LOOP_WINDOW_S)

    with _lock:
        # ── 1. Dedup check ───────────────────────────────────────────────
        hits = _purge_dedup(_dedup.get(signature, []), DEDUP_WINDOW_S, now)
        if len(hits) >= 1:  # 2nd identical hit within 7d -> soft-block
            # Soft-block: return the cached issue_url from the first hit
            result.allowed = False
            result.deduped = True
            for ts, url in hits:
                result.issue_url = url
                break
            return result

        # ── 2. Loop detector ─────────────────────────────────────────────
        loop_hits = _purge(_loop.get(identity, []), _loop_window, now)
        cooldown_expiry = _cooldown.get(identity, 0.0)

        if cooldown_expiry > now:
            # In cooldown
            if not (force and confirmation):
                result.allowed = False
                result.loop_block = True
                result.retry_at = wall_now + timedelta(
                    seconds=max(0, cooldown_expiry - now)
                )
                return result
            # force+confirmation overrides cooldown — proceed

        if len(loop_hits) >= _loop_threshold:
            # Trigger cooldown
            _cooldown[identity] = now + LOOP_COOLDOWN_S
            if not (force and confirmation):
                result.allowed = False
                result.loop_block = True
                result.retry_at = wall_now + timedelta(seconds=LOOP_COOLDOWN_S)
                return result
            # force+confirmation overrides — proceed

        # ── 3. Per-tool window ───────────────────────────────────────────
        tool_key = (identity, tool)
        tool_hits = _purge(_per_tool.get(tool_key, []), PER_TOOL_WINDOW_S, now)
        if len(tool_hits) >= _per_tool_max:
            result.allowed = False
            result.hard_block = True
            result.force_available = True
            # Return last 3 with wall-clock timestamps (approximate)
            result.last_submissions = [
                {"timestamp": (wall_now - timedelta(seconds=now - t)).isoformat()}
                for t in sorted(tool_hits)[-3:]
            ]
            return result

        # ── 4. Cross-tool ceiling ────────────────────────────────────────
        cross_hits = _purge(_cross_tool.get(identity, []), CROSS_TOOL_WINDOW_S, now)
        if len(cross_hits) >= CROSS_TOOL_MAX:
            result.allowed = False
            result.hard_block = True
            result.force_available = False
            return result

        # ── All checks passed — record ───────────────────────────────────
        # Record dedup entry (with issue_url placeholder; caller updates later)
        existing_dedup = list(hits)
        existing_dedup.append((now, issue_url))
        _dedup[signature] = existing_dedup

        # Record per-tool
        tool_hits.append(now)
        _per_tool[tool_key] = tool_hits

        # Record cross-tool
        cross_hits.append(now)
        _cross_tool[identity] = cross_hits

        # Record loop detector
        loop_hits.append(now)
        _loop[identity] = loop_hits

        result.allowed = True
        return result


def update_dedup_url(signature: str, issue_url: str) -> None:
    """Update the issue_url stored for the most recent dedup entry."""
    with _lock:
        entries = _dedup.get(signature, [])
        if entries:
            # Update the last entry's url
            ts, _ = entries[-1]
            entries[-1] = (ts, issue_url)
            _dedup[signature] = entries


def check_skill_error_backstop(identity: str) -> bool:
    """Skill-error specific 30/hr backstop (replaces old 20/hr).

    Returns True if allowed, False if exceeded.
    Records the hit when allowed.
    """
    now = time.monotonic()
    with _lock:
        hits = _purge(_skill_error_backstop.get(identity, []), SKILL_ERROR_WINDOW_S, now)
        if len(hits) >= SKILL_ERROR_RATE_MAX:
            return False
        hits.append(now)
        _skill_error_backstop[identity] = hits
        return True


def reset_all() -> None:
    """Clear all state (for tests only)."""
    with _lock:
        _dedup.clear()
        _per_tool.clear()
        _cross_tool.clear()
        _loop.clear()
        _cooldown.clear()
        _skill_error_backstop.clear()
