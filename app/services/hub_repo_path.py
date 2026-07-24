"""Repo-path resolution + validation for Hub snapshot rows (ponytail_0724).

Split out of ``hub_snapshot.py`` to keep that module under the 600-line
god-object cap (``tests/test_w0_2_pyfile_size_discipline.py``). This is a
cohesive unit anyway: it owns the single question "where does this skill
actually live inside its repo, and is that location safe to publish?"

## Why this exists

The upstream Hub snapshot carries two path-ish fields and only one is the truth:

- ``path``               — a skill *label* (e.g. ``"ponytail"``). Frequently
  NOT the directory the skill actually lives in.
- ``resolved_github_id`` — ``"<owner>/<repo>/<real/path>"``, the upstream's own
  resolution (e.g. ``"dietrichgebert/ponytail/skills/ponytail"``).

Trusting ``path`` minted ``/tree/main/ponytail`` → 404, while the truth was
``/tree/main/skills/ponytail``. That affected 16,006 of 90,605 snapshot rows
(17.7% of the corpus, ~80% of the skills.sh subset) — every skill nested in a
subdirectory.

## Threat model

Both fields are THIRD-PARTY data: the snapshot indexes arbitrary public GitHub
repositories, so their contents are attacker-influenced. Everything published
from them is validated by :func:`is_safe_repo_subpath` before it reaches a URL
or the database.
"""

from __future__ import annotations

from typing import Any

# Must equal FederationHubSkill.path's String(512) column (models.py).
# Validating against the SAME budget the column enforces means a path we accept
# is a path we can store WHOLE — the caller's ``[:512]`` truncation can no
# longer silently cut mid-component and mint a wrong-but-plausible URL.
MAX_REPO_PATH_LEN = 512

# Characters that turn a repo-relative path into URL syntax. Note ``@`` is
# deliberately ABSENT — npm-scoped directories (``v3/@claude-flow/cli/...``) are
# legal, common GitHub paths, and rejecting them regressed 32 real rows from a
# live 200 to a 404 fallback (measured against ruvnet/ruflo, 2026-07-24).
# ``@`` cannot introduce a URL authority without ``//``, which is rejected.
_URL_SYNTAX_CHARS = (":", "?", "#")


def is_safe_repo_subpath(path: str) -> bool:
    """True when ``path`` is a plain, contained, relative path inside a repo.

    The first cut of the ponytail_0724 fix only checked the owner/repo prefix
    and then trusted everything after it, so a crafted value like
    ``owner/repo/../../../evil`` yielded ``../../../evil`` and produced
    ``https://github.com/owner/repo/tree/main/../../../evil`` — a link that
    escapes the tree it claims to point into (GitHub normalises it, so the
    rendered destination is NOT the repo we told the user it was).

    Rejected: any ``.``/``..`` component, empty components (``a//b``), absolute
    paths, trailing slashes, backslashes, URL syntax, whitespace/control
    characters, and anything over the DB column budget.

    Accepted: dot-DIRECTORIES (``.claude/skills/x``) and npm scopes
    (``v3/@claude-flow/cli/x``) — both are legitimate and common.
    """
    if not path or len(path) > MAX_REPO_PATH_LEN:
        return False
    if path.startswith("/") or path.endswith("/"):
        return False
    if "\\" in path or "//" in path:
        return False
    if any(ch in path for ch in _URL_SYNTAX_CHARS):
        return False
    for ch in path:
        # Control chars and whitespace (incl. newline, tab, NBSP) are never
        # legitimate in a GitHub path component we mint a URL from.
        if ord(ch) < 0x21 or ord(ch) == 0x7F:
            return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def resolved_repo_path(row: dict[str, Any]) -> str:
    """Return the skill's REAL, VALIDATED path inside its repo.

    Trust ladder (each rung must pass :func:`is_safe_repo_subpath`):

    1. ``resolved_github_id``'s suffix, but ONLY when its first two segments
       case-insensitively match the row's own ``repo`` (defence against a
       poisoned row pointing us at a third-party repository) and ``repo`` is
       itself a well-formed ``owner/name`` pair.
    2. otherwise the flat ``path`` label.
    3. otherwise ``""`` — the caller then emits a bare repo URL and
       ``install_path_for_row`` degrades the row to deep-link. We never mint a
       path we could not validate.

    The owner/repo comparison is case-insensitive because GitHub owner names
    are, and upstream casing is inconsistent.
    """
    flat_path = (row.get("path") or "").strip()
    repo = (row.get("repo") or "").strip().strip("/")
    resolved = row.get("resolved_github_id")

    fallback = flat_path if is_safe_repo_subpath(flat_path) else ""

    if not isinstance(resolved, str) or not repo:
        return fallback
    # ``repo`` must itself be a plain two-component GitHub identifier, or the
    # prefix comparison below is meaningless.
    repo_parts = repo.split("/")
    if len(repo_parts) != 2 or not all(repo_parts):
        return fallback

    parts = resolved.strip().strip("/").split("/")
    if len(parts) < 3:
        # "owner/repo" with no path component, or plain garbage → no truth here.
        return fallback
    if "/".join(parts[:2]).lower() != repo.lower():
        # The resolved id belongs to a different repository — do not trust it.
        return fallback

    real_path = "/".join(parts[2:])
    return real_path if is_safe_repo_subpath(real_path) else fallback
