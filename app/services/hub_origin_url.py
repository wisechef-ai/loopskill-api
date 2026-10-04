"""Origin-page URLs for Hermes Hub snapshot rows that answer HTTP 200.

``hub_snapshot.origin_url_for_row`` owns the per-upstream decision; this module
owns the two URL shapes it now emits for the upstreams whose old shape 404'd.
Measured 2026-10-04 on random samples of the live snapshot, fetching every URL:

==========  ======================================  =========  ==========
upstream    old shape                               old 200    new 200
==========  ======================================  =========  ==========
skills.sh   github.com/<repo>/tree/main/<path>      4-7/40     40/40
official    hermes-agent.nousresearch.com/skills/x  0/15       15/15
==========  ======================================  =========  ==========

Why the old shapes fail:

- skills.sh stores ``path`` relative to the repo's skills directory, not to the
  repo root, and many repos default to ``master``. Zero of the 20,000 skills.sh
  rows carry ``resolved_github_id`` (the real in-repo path), so no GitHub URL can
  be derived reliably at ingest. The skills.sh page is the source registry's own
  page for the skill — it always resolves and links on to the repository.
- ``hermes-agent.nousresearch.com`` never served per-skill pages. Official rows
  carry real ``repo`` + ``path`` coordinates; ``tree/HEAD`` resolves the default
  branch server-side, so the URL survives a branch rename.

Install is unaffected: the installer resolves ``repo`` + ``path`` itself (branch
probe, then tree walk) and only falls back to the origin URL when both fail.
"""

from __future__ import annotations

import re

SKILLS_SH_PAGE_BASE = "https://www.skills.sh"

_SKILLS_SH_PREFIX = "skills-sh/"
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._-]+")


def _safe_token(token: str) -> bool:
    return bool(_SAFE_TOKEN.fullmatch(token)) and token not in {".", ".."}


def skills_sh_page_url(identifier: str, repo: str | None = None, path: str | None = None) -> str | None:
    """``skills-sh/<owner>/<repo>/<skill>`` → the skills.sh page, else None.

    Every token must be a plain path segment. A token with ``?``, ``#``, ``/``,
    spaces or ``..`` would make the URL point somewhere other than the skill,
    so such an identifier gets no page URL and the caller falls back.

    When the row carries ``repo`` / ``path``, the identifier's owner/repo must
    equal ``repo`` and its skill token must equal the last ``path`` segment
    (case-insensitive): an internally inconsistent row must not link a
    DIFFERENT skill's page. Calibrated 2026-10-04: 0 of 20,000 live rows differ
    on either check.
    """
    ident = (identifier or "").strip()
    if not ident.startswith(_SKILLS_SH_PREFIX):
        return None
    tokens = ident[len(_SKILLS_SH_PREFIX) :].split("/")
    if len(tokens) != 3 or not all(_safe_token(t) for t in tokens):
        return None
    if repo and f"{tokens[0]}/{tokens[1]}".lower() != repo.strip().lower():
        return None
    leaf = (path or "").strip("/").rsplit("/", 1)[-1]
    if leaf and tokens[2].lower() != leaf.lower():
        return None
    return f"{SKILLS_SH_PAGE_BASE}/{'/'.join(tokens)}"


def github_tree_url(repo: str, path: str, *, ref: str) -> str:
    """``https://github.com/<repo>`` plus ``/tree/<ref>/<path>`` when a path is
    known. The caller has already validated ``repo`` and ``path``."""
    base = f"https://github.com/{repo}"
    return f"{base}/tree/{ref}/{path}" if path else base
