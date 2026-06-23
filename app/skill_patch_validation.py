"""app/skill_patch_validation.py — pure validation module (no DB I/O).

Enforces R1 (path allowlist), R2 (forbidden-token scan), and R8 (size cap)
for incoming skill-patch submissions.

All functions are synchronous and import-safe (no FastAPI, no SQLAlchemy).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from typing import Any

# ── R1: Path allowlist / blocklist ────────────────────────────────────────

# Exact-match allowed paths
PATH_ALLOWLIST_EXACT: frozenset[str] = frozenset(
    {
        "SKILL.md",
        "recipe.yaml.frontmatter",
    }
)

# Glob patterns for allowed paths.
#
# templates/* is intentionally broad: real-world template filenames include
# extensionless ones (Modelfile, Dockerfile, Containerfile) and many config
# variants. The R1 blocklist below still rejects executable script paths,
# *.py, recipe.yaml, and install/uninstall.sh — so templates/* is "inert
# text consumed by code that lives elsewhere", which is the safe bucket.
PATH_ALLOWLIST_GLOBS: list[str] = [
    "references/*.md",
    "templates/*",
]

# Glob patterns that are explicitly blocked (blocklist takes priority)
PATH_BLOCKLIST_GLOBS: list[str] = [
    "scripts/**",
    "install.sh",
    "uninstall.sh",
    "*.py",
    "recipe.yaml",
]


def _matches_any_glob(path: str, globs: list[str]) -> bool:
    """Return True if path matches any of the given glob patterns."""
    for pattern in globs:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also support ** matching (translate to simple prefix match)
        if "**" in pattern:
            prefix = pattern.split("**")[0]
            if path.startswith(prefix):
                return True
    return False


def validate_path(path: str) -> tuple[bool, str]:
    """Validate a single file path against the allowlist.

    Returns (ok, reason). ok=True means the path is permitted.
    Blocklist is checked first; if a path is blocklisted it is rejected
    even if it would otherwise match an allowlist pattern.
    """
    # Normalize (no leading slash, no ..)
    if ".." in path or path.startswith("/"):
        return False, f"path '{path}' contains traversal or is absolute"

    # Blocklist check (takes priority)
    if path in ("install.sh", "uninstall.sh", "recipe.yaml"):
        return False, f"path '{path}' is explicitly blocked (use allowlist paths only)"
    if _matches_any_glob(path, PATH_BLOCKLIST_GLOBS):
        return False, (
            f"path '{path}' is blocked (scripts/*, install.sh, uninstall.sh, "
            "*.py, recipe.yaml are not patchable via this endpoint — "
            "describe script changes in the skill-error issue body instead)"
        )

    # Allowlist check
    if path in PATH_ALLOWLIST_EXACT:
        return True, ""
    if _matches_any_glob(path, PATH_ALLOWLIST_GLOBS):
        return True, ""

    return False, (
        f"path '{path}' is not on the allowlist. "
        "Allowed: SKILL.md, recipe.yaml.frontmatter, references/*.md, "
        "templates/*.{yml,yaml,sh,env,md}"
    )


# ── R2: Forbidden-token scan ──────────────────────────────────────────────

_FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bcurl\s+[^|]*\|\s*(ba)?sh", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bwget\s+[^|]*\|\s*(ba)?sh", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\beval\s*\(", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bbase64\s+-d\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bchmod\s+\+x\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bnc\s+-e\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bpython\s+-c\s+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\|\s*sh\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bsetsid\s+nohup", re.IGNORECASE | re.MULTILINE),
]

# Human-readable names for each pattern (same order as _FORBIDDEN_PATTERNS)
_FORBIDDEN_NAMES: list[str] = [
    "curl pipe to shell",
    "wget pipe to shell",
    "eval() call",
    "base64 -d (decode+exec pattern)",
    "chmod +x",
    "nc -e (netcat exec)",
    "python -c (inline exec)",
    "pipe to sh",
    "setsid nohup",
]


def scan_forbidden(content: str) -> list[str]:
    """Scan content for forbidden shell execution patterns.

    Returns a list of matched pattern names. Empty list means clean.
    """
    hits: list[str] = []
    for pattern, name in zip(_FORBIDDEN_PATTERNS, _FORBIDDEN_NAMES):
        if pattern.search(content):
            hits.append(name)
    return hits


# ── R8: Size cap ──────────────────────────────────────────────────────────

MAX_FILES = 3
MAX_LINES_PER_FILE = 200
MAX_TOTAL_LINES = 600


def check_size(files: list[dict[str, Any]]) -> tuple[bool, str]:
    """Check that the patch stays within hard size limits.

    files: list of {"path": str, "content": str}
    Returns (ok, reason). ok=True means size is acceptable.
    """
    if len(files) > MAX_FILES:
        return False, (
            f"too many files: {len(files)} (max {MAX_FILES}). Split into smaller patches by topic."
        )

    total_lines = 0
    for f in files:
        content = f.get("content", "")
        line_count = content.count("\n")
        if line_count > MAX_LINES_PER_FILE:
            return False, (
                f"file '{f.get('path', '?')}' has {line_count} lines "
                f"(max {MAX_LINES_PER_FILE} per file). Split the patch."
            )
        total_lines += line_count

    if total_lines > MAX_TOTAL_LINES:
        return False, (
            f"patch totals {total_lines} lines across all files "
            f"(max {MAX_TOTAL_LINES} total). Split into smaller patches by topic."
        )

    return True, ""


# ── R4: Canonical hash (dedup) ────────────────────────────────────────────


def canonical_hash(slug: str, files: list[dict[str, Any]]) -> str:
    """Compute a stable dedup hash for a skill-patch submission.

    Hash = sha256( slug + repr(sorted([(path, normalised_content)])) )
    Normalisation: strip, CRLF→LF.
    """
    normalised = sorted(
        (
            f["path"],
            f["content"].strip().replace("\r\n", "\n"),
        )
        for f in files
    )
    raw = slug + repr(normalised)
    return hashlib.sha256(raw.encode()).hexdigest()
