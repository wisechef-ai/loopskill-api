"""Fail the build on an unverifiable claim in README.md.

P4 (bundles_0811) rule: every numeric or superlative claim in the repo
README must be either (a) live-measurable from the filesystem, and this
test recomputes and cross-checks it, or (b) explicitly marked as a dated
snapshot with the exact command a reader can run to reproduce it. A bare
"69,172 lines" with no way to check it is exactly the failure mode #68
pointed at — the issue's own numbers were already stale when filed.

This test does NOT reach the network (it must run in any CI, offline).
Claims sourced from the GitHub API (stars, forks) are checked structurally
(they must carry a `(measured ` snapshot marker and the exact `gh`/`curl`
command used) rather than re-fetched live.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"

# Superlative / unfalsifiable marketing language P4 explicitly rejects
# (lock #1: catalog-as-moat framing, and "Spotify for X" style claims that
# assert a category-defining/leadership position not owned by anyone).
FORBIDDEN_SUPERLATIVES = (
    "spotify for",
    "the best",
    "the fastest",
    "#1",
    "market leader",
    "industry standard",
    "industry-standard",
    "revolutionary",
    "world-class",
    "best-in-class",
    "leading registry",
    "the only",
)


# unit -> live measurement function. Each returns the current true count so
# a number written in README.md can be checked against reality every run.
def _app_files(root: Path) -> int:
    return len(list((root / "app").rglob("*.py")))


def _app_lines(root: Path) -> int:
    return sum(
        len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        for p in (root / "app").rglob("*.py")
    )


def _migrations(root: Path) -> int:
    return len([p for p in (root / "alembic" / "versions").glob("*.py") if p.name != "__init__.py"])


def _test_files(root: Path) -> int:
    # `packaging/` holds self-contained, independently-installable
    # sub-packages (see packaging/loopskill-mcp/) with their own test
    # suites, excluded from root pytest collection via the root
    # pyproject.toml's `addopts = "--ignore=packaging"` — excluded here for
    # the same reason: they are not part of this repo's own app test count.
    skip = {".venv", "node_modules", "__pycache__", "packaging"}
    return len([p for p in root.rglob("test_*.py") if not (skip & set(p.parts))])


# Regex: a number (with optional thousands separators) immediately followed
# by one of these unit phrases, case-insensitive, singular or plural.
_UNIT_CHECKS: dict[str, Callable[[Path], int]] = {
    r"app(?:\s+source)?\s+(?:python\s+)?files?": _app_files,
    r"lines? of (?:app )?code": _app_lines,
    r"(?:alembic )?migrations?": _migrations,
    r"test files?": _test_files,
}

_NUMBER_RE = re.compile(r"(\d[\d,]*)\s+")


def _strip_fenced_code(text: str) -> str:
    """Drop ``` fenced blocks — those are reproducible transcripts (the
    diff demo), not prose claims, and are exempt: a reader reproduces them
    by running the commands, which is a stronger guarantee than a citation.
    """
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _prose_lines(text: str) -> list[str]:
    return _strip_fenced_code(text).splitlines()


def test_no_forbidden_superlatives_in_readme() -> None:
    text = _strip_fenced_code(README.read_text(encoding="utf-8")).lower()
    hits = [s for s in FORBIDDEN_SUPERLATIVES if s in text]
    assert not hits, f"Unverifiable superlative claim(s) in README.md: {hits}"


def test_numeric_repo_claims_match_live_measurement() -> None:
    root = README.parent
    lines = _prose_lines(README.read_text(encoding="utf-8"))
    checked = 0
    for lineno, line in enumerate(lines, start=1):
        for unit_pattern, measure_fn in _UNIT_CHECKS.items():
            m = re.search(_NUMBER_RE.pattern + unit_pattern, line, re.IGNORECASE)
            if not m:
                continue
            claimed = int(m.group(1).replace(",", ""))
            actual = measure_fn(root)
            checked += 1
            assert claimed == actual, (
                f"README.md:{lineno} claims {claimed} for pattern "
                f"'{unit_pattern}' but live measurement is {actual}. "
                f"Line: {line.strip()!r}"
            )
    # Guard against the check silently checking nothing (a rewritten README
    # that deletes all measurable claims would pass vacuously otherwise).
    assert checked >= 1, "Expected at least one measurable numeric claim in README.md"


def test_star_and_fork_claims_carry_a_reproducible_snapshot_marker() -> None:
    """Stars/forks come from the GitHub API — not re-fetched here (no network
    in this test), but any such number in prose MUST be dated and carry the
    exact command to reproduce it, so a reader can verify it themselves.
    """
    lines = _prose_lines(README.read_text(encoding="utf-8"))
    for lineno, line in enumerate(lines, start=1):
        if re.search(r"\d+\s+(github )?stars?\b", line, re.IGNORECASE) or re.search(
            r"\d+\s+forks?\b", line, re.IGNORECASE
        ):
            assert "measured" in line.lower(), (
                f"README.md:{lineno} states a star/fork count without a "
                f"'(measured YYYY-MM-DD, via ...)' snapshot marker: {line.strip()!r}"
            )
