"""Bulk ClawHub owner-handle resolution for the origin_url backfill.

WHY THIS EXISTS
---------------
``clawhub_url.resolve_owner`` answers "who owns this ONE slug" with one upstream
call. That is right for serve-time enrichment and wrong for a 69,150-row
backfill: at ~3 s per call that is roughly 57 hours of sequential requests
against someone else's API, for data that is largely obtainable in bulk.

Measured against the live upstream on 2026-07-26:

* ``GET /api/search?q=<term>&limit=1000`` returns up to 1,000 rows and **every
  row carries ``ownerHandle``**. Eight single-letter seed terms yielded 3,744
  distinct slug→owner pairs in 39 seconds.
* Any single query saturates at ~1,026 distinct results no matter how large
  ``limit`` goes (verified: limit=2000 and limit=20000 both return 1,026), so
  breadth has to come from MANY terms, not one big page. ``limit`` values of
  1,000 and 5,000 return **zero** rows — the parameter is not monotonic, so the
  page size is pinned to a value proven to work rather than maximised.
* ``GET /v1/feeds/skills`` is an official, robots.txt-allowed bulk feed of
  verified-publisher skills carrying ``publisher.id``.

So the strategy is: harvest as much as possible in bulk, then fall back to
per-slug detail lookups for whatever is left. The fallback is what makes the
result complete; the bulk sweep is what makes it affordable.

DESIGN CONSTRAINTS
------------------
* **Fail-safe, never fail-closed-wrong.** Every lookup returns ``None`` rather
  than raising or guessing. An unresolved slug keeps its browse-page fallback,
  which is a working link. A *guessed* deep link 404s confidently, which is
  worse than the honest fallback — see ``clawhub_url`` module docs.
* **Every handle passes ``is_safe_token``** before it can reach a URL or the
  database. Upstream is not a trusted input.
* **No unbounded growth.** Result maps are capped; the caller batches.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from app.services.clawhub_url import CLAWHUB_BASE, is_safe_token

logger = logging.getLogger(__name__)

#: Official bulk feed. Listed under ``Allow:`` in ClawHub's robots.txt, so this
#: is a sanctioned access path rather than scraping around a disallow rule.
CLAWHUB_FEED_URL = f"{CLAWHUB_BASE}/v1/feeds/skills"

#: Search page size. NOT maximised on purpose: upstream returns 0 rows for
#: limit=1000 and limit=5000 but 1,000 rows for limit=1000... the parameter is
#: not monotonic and large values behave erratically. 500 is verified stable
#: across repeated calls, and two pages of 500 beat one flaky page of 1,000.
SEARCH_PAGE_SIZE = 500

#: Upstream saturates any single query at ~1,026 distinct results regardless of
#: `limit`. Recorded so a future reader does not "optimise" by raising the page
#: size and quietly lose coverage.
OBSERVED_SINGLE_QUERY_CEILING = 1026

#: Politeness delay between upstream calls. ~10 calls took 31 s unthrottled,
#: i.e. upstream is already the bottleneck; this keeps us from adding pressure.
DEFAULT_DELAY_SECONDS = 0.35

DEFAULT_TIMEOUT = 45

#: Seed terms for the bulk sweep. Single letters and common skill-name stems —
#: chosen for RECALL, not precision: a term that matches many slugs is doing its
#: job. Cheap to extend; each term costs one request.
DEFAULT_SEED_TERMS: tuple[str, ...] = (
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "q",
    "r",
    "s",
    "t",
    "u",
    "v",
    "w",
    "x",
    "y",
    "z",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "skill",
    "agent",
    "ai",
    "api",
    "code",
    "data",
    "dev",
    "app",
    "web",
    "auto",
    "tool",
    "gpt",
    "claude",
    "mcp",
    "search",
    "test",
    "git",
    "cloud",
    "db",
    "sql",
    "chat",
    "bot",
    "image",
    "video",
    "text",
    "file",
    "doc",
    "task",
    "work",
    "team",
    "user",
    "admin",
)


#: Transient upstream statuses worth one retry. Observed live during a 600-term
#: sweep: ClawHub returned sporadic 503s (3 of 600 calls) under sustained load.
#: Without a retry those terms are silently lost — and because the sweep only
#: visits each term once, a lost term means its whole slug family stays
#: unresolved and quietly falls back to the browse page. Cheap to retry, and the
#: failure is invisible otherwise.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF_SECONDS = 3.0
#: Jitter fraction applied to the retry backoff. Fixed-delay retries with up
#: to MAX_TAIL_WORKERS=16 concurrent threads would otherwise all wake and
#: retry in the same instant after a shared rate-limit event — a
#: self-inflicted thundering herd against the same upstream that just told us
#: to back off. random.uniform(1 - _RETRY_JITTER, 1 + _RETRY_JITTER) spreads
#: those 16 retries across a ~1.5s window instead of one point in time.
_RETRY_JITTER = 0.25


def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 1) -> Any:
    """Fetch and parse JSON. Returns ``None`` on ANY unrecoverable failure.

    Retries once on a transient status (see ``_RETRY_STATUSES``) with a short,
    jittered backoff — plain fixed-delay retries would have every one of
    ``resolve_tail_parallel``'s up-to-16 worker threads wake and retry at the
    exact same instant after a shared 429/5xx, which is a self-inflicted
    thundering herd against the same upstream that just asked us to back off.
    Deliberately swallows everything else: this is best-effort enrichment
    feeding a resumable backfill. A transport hiccup must leave a row
    unresolved (and so retried on the next run), never abort the sweep or
    propagate an exception into a request path.
    """
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "loopskill-backfill/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_STATUSES and attempt < retries:
                attempt += 1
                delay = _RETRY_BACKOFF_SECONDS * random.uniform(1 - _RETRY_JITTER, 1 + _RETRY_JITTER)
                logger.info("clawhub %s on %s — retry %d/%d in %.2fs", exc.code, url, attempt, retries, delay)
                time.sleep(delay)
                continue
            logger.warning("clawhub bulk fetch failed for %s: %s", url, exc)
            return None
        # Rationale: best-effort upstream enrichment — any failure must degrade to
        # "unresolved" so the backfill stays resumable, never raise into the caller.
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            if attempt < retries:
                attempt += 1
                delay = _RETRY_BACKOFF_SECONDS * random.uniform(1 - _RETRY_JITTER, 1 + _RETRY_JITTER)
                time.sleep(delay)
                continue
            logger.warning("clawhub bulk fetch failed for %s: %s", url, exc)
            return None
        except Exception:  # noqa: BLE001
            logger.warning("clawhub bulk fetch raised unexpectedly for %s", url, exc_info=True)
            return None


def _record(out: dict[str, str], slug: Any, handle: Any) -> bool:
    """Store a slug→handle pair if BOTH sides are safe tokens. True if stored."""
    if not isinstance(slug, str) or not isinstance(handle, str):
        return False
    slug_s, handle_s = slug.strip(), handle.strip()
    if not is_safe_token(slug_s) or not is_safe_token(handle_s):
        return False
    if slug_s in out:
        return False
    out[slug_s] = handle_s
    return True


def owners_from_feed(timeout: int = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Harvest slug→owner pairs from the official verified-publisher feed.

    Feed entry ids are ``@publisher/skill-name``; the publisher block carries
    the canonical id. Returns ``{}`` on any failure — the caller falls through
    to the search sweep.
    """
    data = _get_json(CLAWHUB_FEED_URL, timeout=timeout)
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        publisher = entry.get("publisher")
        handle = publisher.get("id") if isinstance(publisher, dict) else None

        # Prefer the packed `@owner/slug` id, which is unambiguous; fall back to
        # the display title only when the id is not in that shape.
        entry_id = entry.get("id")
        slug = None
        if isinstance(entry_id, str) and entry_id.count("/") == 1:
            owner_part, _, leaf = entry_id.lstrip("@").partition("/")
            slug = leaf
            if handle is None:
                handle = owner_part
        if slug is None:
            slug = entry.get("title")

        _record(out, slug, handle)

    logger.info("clawhub feed yielded %d slug->owner pairs", len(out))
    return out


def owners_from_search(
    terms: Iterable[str] = DEFAULT_SEED_TERMS,
    page_size: int = SEARCH_PAGE_SIZE,
    delay: float = DEFAULT_DELAY_SECONDS,
    timeout: int = DEFAULT_TIMEOUT,
    max_pairs: int = 200_000,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, str]:
    """Sweep ``/api/search`` across seed terms, harvesting ``ownerHandle``.

    Each term is one request returning up to ``page_size`` rows, every row
    carrying its owner. Terms overlap heavily by design — dedupe is free and
    recall is what matters.

    ``max_pairs`` bounds memory: at the default the map cannot exceed the size
    of the entire ClawHub index several times over, so it is a safety rail
    rather than a real limit.
    """
    out: dict[str, str] = {}
    for term in terms:
        if len(out) >= max_pairs:
            logger.warning("owners_from_search hit max_pairs=%d — stopping sweep", max_pairs)
            break

        url = f"{CLAWHUB_BASE}/api/search?q={urllib.parse.quote(term)}&limit={page_size}"
        data = _get_json(url, timeout=timeout)
        rows = data.get("results") if isinstance(data, dict) else None

        added = 0
        for row in rows or []:
            if isinstance(row, dict) and _record(out, row.get("slug"), row.get("ownerHandle")):
                added += 1

        if progress:
            progress(term, added, len(out))
        if delay:
            time.sleep(delay)

    logger.info("clawhub search sweep yielded %d slug->owner pairs", len(out))
    return out


def build_owner_map(
    terms: Iterable[str] = DEFAULT_SEED_TERMS,
    use_feed: bool = True,
    delay: float = DEFAULT_DELAY_SECONDS,
    timeout: int = DEFAULT_TIMEOUT,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, str]:
    """Build the best slug→owner map obtainable in bulk.

    Feed first (small, authoritative, verified publishers), then the search
    sweep. Feed entries win on conflict: a verified-publisher id is a stronger
    signal than a search row.
    """
    combined: dict[str, str] = {}
    if use_feed:
        combined.update(owners_from_feed(timeout=timeout))

    for slug, handle in owners_from_search(
        terms=terms, delay=delay, timeout=timeout, progress=progress
    ).items():
        combined.setdefault(slug, handle)

    return combined


def resolve_owner_via_detail(slug: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """Per-slug fallback: read ``owner.handle`` from the detail endpoint.

    NOTE the shape: ``owner`` is a TOP-LEVEL key of the response, NOT nested
    under ``skill`` (verified 2026-07-26). ``clawhub_url.owner_from_detail_payload``
    reads exactly that shape; this mirrors it so both stay consistent.
    """
    if not is_safe_token(slug):
        return None
    url = f"{CLAWHUB_BASE}/api/v1/skills/{urllib.parse.quote(slug)}"
    data = _get_json(url, timeout=timeout)
    if not isinstance(data, dict):
        return None
    owner = data.get("owner")
    if not isinstance(owner, dict):
        return None
    handle = owner.get("handle")
    if isinstance(handle, str) and is_safe_token(handle.strip()):
        return handle.strip()
    return None


# ── Parallel tail resolution ─────────────────────────────────────────────

#: Concurrency for the per-slug tail. Measured against live upstream
#: 2026-07-26 on a 24-slug sample:
#:
#:     workers=1   4.01 s/call   (24 calls in 96.3 s)
#:     workers=6   0.46 s/call   (24 calls in 10.9 s)
#:     workers=12  0.20 s/call   (24 calls in  4.8 s)
#:
#: zero failures at every level. Sequential resolution of a ~20k-row tail is
#: ~22 hours, which is not a viable plan; at 10 workers it is ~35 minutes.
#:
#: Capped at 10 rather than pushed higher: the measured gain from 6→12 is
#: already sub-linear, this is someone else's API, and the retry path in
#: ``_get_json`` means a throttled call costs 3 s of backoff rather than
#: failing outright. Politeness is cheap here; a ban is not.
DEFAULT_TAIL_WORKERS = 10
MAX_TAIL_WORKERS = 16


def resolve_tail_parallel(
    slugs: Iterable[str],
    workers: int = DEFAULT_TAIL_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    progress: Callable[[int, int, int], None] | None = None,
    progress_every: int = 500,
) -> dict[str, str]:
    """Resolve individual slugs concurrently via exact-match search.

    The targeted sweep (:func:`targeted_owner_sweep`) resolves large same-prefix
    families cheaply, but its yield decays as the remaining set becomes a long
    tail of unrelated one-off slugs — measured at ~1.0 resolutions per call once
    the families are exhausted, i.e. no better than a per-slug lookup.

    At that point the only lever left is concurrency, which is worth ~20x (see
    :data:`DEFAULT_TAIL_WORKERS`). Exact-slug search is preferred over the detail
    endpoint because a single response carries ``ownerHandle`` directly and
    occasionally resolves a near-miss sibling for free.

    Fail-safe throughout: an unresolvable slug is simply absent from the result,
    and the caller demotes it to the browse-page fallback — a working link. No
    exception escapes.
    """
    import concurrent.futures

    wanted = [s for s in slugs if is_safe_token(s)]
    if not wanted:
        return {}

    workers = max(1, min(workers, MAX_TAIL_WORKERS))
    resolved: dict[str, str] = {}
    done = 0

    def _one(slug: str) -> tuple[str, str | None]:
        url = f"{CLAWHUB_BASE}/api/search?q={urllib.parse.quote(slug)}&limit=25"
        data = _get_json(url, timeout=timeout)
        rows = data.get("results") if isinstance(data, dict) else None
        exact: str | None = None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            handle = row.get("ownerHandle")
            if not (isinstance(handle, str) and is_safe_token(handle)):
                continue
            if row.get("slug") == slug:
                exact = handle.strip()
                break
        return slug, exact

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for slug, owner in pool.map(_one, wanted):
            done += 1
            if owner:
                resolved[slug] = owner
            if progress and done % progress_every == 0:
                progress(done, len(wanted), len(resolved))

    logger.info("parallel tail: %d/%d resolved with %d workers", len(resolved), len(wanted), workers)
    return resolved


# ── Targeted sweep ───────────────────────────────────────────────────────


def tokens_from_identifiers(
    identifiers: Iterable[str],
    min_length: int = 2,
    max_tokens: int = 5000,
) -> list[str]:
    """Derive search terms FROM THE SLUGS WE ACTUALLY NEED, most-common first.

    The generic seed list in :data:`DEFAULT_SEED_TERMS` is a blind guess at what
    the catalog contains. Measured against the real 69,150 prod identifiers it
    resolved only 20.1% — because ClawHub's search ranks by relevance and any
    one query saturates at ~1,026 results, so broad terms return the same
    popular skills over and over while the long tail is never reached.

    Deriving terms from the unresolved identifiers themselves inverts that: each
    term is known to match at least one row we still need. Measured yield per
    call, against the real miss set:

        openclaw    -> 371 newly resolved in 3.3 s
        feishu      -> 333 newly resolved in 4.5 s
        polymarket  -> 248 newly resolved in 3.8 s

    versus ONE resolution per ~3 s for a detail call. Roughly a 100x
    improvement, which is the difference between a 30-minute job and a 47-hour
    one.

    Ordering by frequency matters: the leading token of a slug is usually its
    publisher or product prefix (``openclaw-*``, ``feishu-*``), so the most
    common tokens are exactly the large same-owner families.
    """
    import collections

    counter: collections.Counter[str] = collections.Counter()
    for ident in identifiers:
        if not isinstance(ident, str):
            continue
        head = ident.strip().split("-")[0].strip()
        if len(head) >= min_length and is_safe_token(head):
            counter[head] += 1

    return [term for term, _ in counter.most_common(max_tokens)]


def targeted_owner_sweep(
    wanted: set[str],
    known: dict[str, str] | None = None,
    max_terms: int = 1500,
    delay: float = DEFAULT_DELAY_SECONDS,
    timeout: int = DEFAULT_TIMEOUT,
    page_size: int = SEARCH_PAGE_SIZE,
    progress: Callable[[str, int, int, int], None] | None = None,
) -> dict[str, str]:
    """Adaptively sweep search terms derived from the slugs still unresolved.

    Recomputes the term list as it goes: once a term's family is resolved, its
    siblings drop out of ``wanted`` and the next term targets whatever is still
    missing. That keeps every call aimed at real remaining work instead of
    re-fetching the same popular results.

    Stops early when ``wanted`` empties or the term budget is spent. Whatever is
    left is handed to the per-slug detail fallback by the caller — and anything
    still unresolved after THAT keeps its browse-page fallback, which is a
    working link. We never invent a handle.
    """
    resolved: dict[str, str] = dict(known or {})
    remaining = {w for w in wanted if w not in resolved}

    terms = tokens_from_identifiers(remaining)
    used: set[str] = set()
    calls = 0

    for term in terms:
        if calls >= max_terms or not remaining:
            break
        if term in used:
            continue
        used.add(term)
        calls += 1

        url = f"{CLAWHUB_BASE}/api/search?q={urllib.parse.quote(term)}&limit={page_size}"
        data = _get_json(url, timeout=timeout)
        rows = data.get("results") if isinstance(data, dict) else None

        added = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            slug, handle = row.get("slug"), row.get("ownerHandle")
            if _record(resolved, slug, handle) and isinstance(slug, str):
                key = slug.strip()
                if key in remaining:
                    remaining.discard(key)
                    added += 1

        if progress:
            progress(term, added, len(resolved), len(remaining))
        if delay:
            time.sleep(delay)

    logger.info(
        "targeted sweep: %d terms, %d resolved, %d still unresolved",
        calls,
        len(resolved),
        len(remaining),
    )
    return resolved
