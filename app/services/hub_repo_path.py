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
# Non-ASCII characters that are genuinely dangerous in a link we present as a
# repo location — separator homoglyphs, bidi/direction overrides, and invisible
# joiners/spaces. We do NOT reject non-ASCII wholesale: CJK directory names are
# legitimate and real (`skills/01-内容创作/...` in vivy-yi/xiaohongshu-skills is
# a live 200), and a blanket rule cost 12 such rows in an earlier cut.
_UNSAFE_UNICODE = frozenset(
    "\uff0f"  # FULLWIDTH SOLIDUS — looks like '/'
    "\u2044"  # FRACTION SLASH
    "\u2215"  # DIVISION SLASH
    "\u29f8"  # BIG SOLIDUS
    "\uff3c"  # FULLWIDTH REVERSE SOLIDUS
    "\u2216"  # SET MINUS
    "\u29f5"  # REVERSE SOLIDUS OPERATOR
    "\u202a\u202b\u202c\u202d\u202e"  # bidi embedding/override
    "\u2066\u2067\u2068\u2069"  # bidi isolates
    "\u200e\u200f\u061c"  # LTR/RTL marks, arabic letter mark
    "\u206a\u206b\u206c\u206d\u206e\u206f"  # deprecated bidi controls
    "\u200b\u200c\u200d\u2060\ufeff"  # zero-width / invisible joiners
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"  # unicode whitespace
)

# GitHub owner/repo identifier charset. Owners are ASCII alphanumeric with
# single hyphens; repo names additionally allow ``.`` and ``_``. We validate
# against a deliberately CONSERVATIVE superset of both: anything outside it is
# not a repo we are willing to interpolate into a URL.
_GH_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


def is_safe_repo_ident(repo: str) -> bool:
    """True when ``repo`` is a plain ``owner/name`` pair safe to put in a URL.

    R2 review (Codex HIGH #1). Validating only the resolved-ID *suffix* left the
    ``repo`` field itself unchecked, and ``origin_url_for_row`` interpolates it
    RAW. Reproduced escapes:

        repo="owner?x/repo"  -> https://github.com/owner?x/repo/tree/main/safe
        repo="owner#x/repo"  -> https://github.com/owner#x/repo/tree/main/safe

    Both TRUNCATE the intended path — everything after ``?``/``#`` becomes a
    query/fragment, so the browser requests ``github.com/owner`` and the user
    lands somewhere other than the tree we advertised. ``/owner/repo/`` also
    produced a doubled-slash URL because the helper normalised slashes for its
    comparison while the URL builder did not.

    A row failing this check gets NO path (bare-repo URL) and is degraded to
    deep-link rather than fetch_origin — fail closed at the COMPLETE URL
    boundary, not just the suffix.
    """
    if not repo or len(repo) > 256:
        return False
    parts = repo.split("/")
    if len(parts) != 2:
        return False
    for part in parts:
        if not part or len(part) > 100:
            return False
        # Only the TRAVERSAL forms are dangerous. A leading dot is legitimate
        # and real: `travisjneuman/.claude` is a live repo (HTTP 200) that this
        # rule initially rejected, costing 12 rows. A trailing dot/hyphen is
        # still refused (not valid on GitHub, and a trailing-dot host component
        # is a normalisation hazard).
        if part in (".", ".."):
            return False
        if part[-1] in ".-" or part[0] == "-":
            return False
        if any(ch not in _GH_IDENT_CHARS for ch in part):
            return False
    return True


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
    if any(ch in path for ch in (":", "?", "#")):
        return False
    # Percent-encoding: we never decode, so `%2e%2e%2f` would pass a naive
    # component check while a decoding consumer sees `../`. Reject outright —
    # a legitimate GitHub directory name does not need it (R2 review).
    if "%" in path:
        return False
    for ch in path:
        # ASCII control chars and space are never legitimate in a path we mint
        # a URL from. Above ASCII we reject only the genuinely dangerous set
        # (separator homoglyphs, bidi overrides, invisible joiners/spaces) —
        # NOT all non-ASCII, because CJK directory names are real and live.
        if ord(ch) < 0x21 or ord(ch) == 0x7F:
            return False
        if ch in _UNSAFE_UNICODE:
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
    repo = (row.get("repo") or "").strip()
    resolved = row.get("resolved_github_id")

    # R2: an unsafe repo poisons the WHOLE url, not just its suffix — a `?`/`#`
    # in the owner truncates the path we advertise. Emit no path at all so the
    # caller degrades to a bare (and still-validated) repo URL / deep-link.
    if not is_safe_repo_ident(repo):
        return ""

    fallback = flat_path if is_safe_repo_subpath(flat_path) else ""

    if not isinstance(resolved, str):
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
