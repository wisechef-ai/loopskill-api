#!/usr/bin/env python3
"""skill_discipline_linter.py — Phase A.7 pre-publish discipline gate.

Enforces the Skill-Discipline Anti-Patterns reference (Phase A.7) so that every
skill works because of what's encoded in the install, not because of who's
running it. Used at publish-time by the FastAPI gate and during the one-time
sanitization sweep over the existing skill catalog.

Rules:
    no_user_names                  — Adam/Tori/Wise/Chef/Mariusz/Olek/Marco/Karol
    no_curl_bash                   — `curl ... | bash`, `wget ... | sh`
    no_hardcoded_home_paths        — /home/<user>/, /Users/<user>/
    no_internal_infra_refs         — Paperclip/wisechef-agents/wisechef-hq/adam-xps/obsidian-vault
    no_agent_discipline_text       — "the agent should always", "always ask", "when in doubt"
    no_external_promo              — non-allowlisted external links
    must_declare_compat            — recipe.yaml requires runtime.compatibility
    must_have_help_text            — referenced .py scripts must respond to --help (AST check)
    no_report_back_without_placeholder — "report to <name>", "tell <name>"

Usage:
    python scripts/skill_discipline_linter.py <path-to-skill-dir-or-readme.md>
    python scripts/skill_discipline_linter.py <path> --auto-fix   # print unified diff

Library:
    from scripts.skill_discipline_linter import lint_skill
    result = lint_skill(readme_text, recipe_yaml=None)
    # → {"ok": bool, "violations": [{"rule", "line", "snippet", "suggestion"}]}

Exit codes:
    0 — pass
    1 — violations found
    2 — usage error

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_NAMES = ("Adam", "Tori", "Wise", "Chef", "Mariusz", "Olek", "Marco", "Karol")
INTERNAL_INFRA = ("Paperclip", "wisechef-agents", "wisechef-hq", "adam-xps", "obsidian-vault")

# Tokens we're willing to accept inside emails/URLs even if they collide with
# a banned user name (e.g. adam@example.com, /Users/adam/.cache).
URL_OR_EMAIL_RE = re.compile(r"\S+@\S+\.\S+|https?://\S+")

ALLOWED_LINK_DOMAINS = (
    "github.com",
    "recipes.wisechef.ai",
    "wisechef.ai",
    "anthropic.com",
    "pypi.org",
    "npmjs.com",
    "crates.io",
    "registry.npmjs.org",
    "huggingface.co",
    "docs.python.org",
    "developer.mozilla.org",
)

# Placeholder / RFC-2606 reserved / docs-example domains. Always documentation,
# never promotional.
PLACEHOLDER_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "iana.org",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "yourdomain.com",
    "yourcompany.com",
    "yourclient.com",
    "yoursite.com",
})

LINK_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(/[^\s)\"'>]*)?")

CURL_BASH_RE = re.compile(r"\b(curl|wget)\s+[^|\n]*\|\s*(?:ba)?sh\b", re.IGNORECASE)

HOME_PATH_RE = re.compile(r"(?<!\$\{)(?:/home/|/Users/)([a-z][a-z0-9_-]*)/")

AGENT_DISCIPLINE_RE = re.compile(
    r"\b(the agent should always|always ask|when in doubt)\b",
    re.IGNORECASE,
)

# `report (back) to NAME` / `tell NAME` — flag unless NAME is a placeholder.
REPORT_BACK_RE = re.compile(
    r"\b(?:report\s+(?:back\s+)?to|tell)\s+(?!\$\{|the\s+user\b|your\b|a\b|an\b)([A-Za-z][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)

# Bare-name regex (per user-name); built dynamically because we want word boundaries
# but also need to skip occurrences inside emails/URLs.
def _name_regex(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    rule: str
    line: int
    snippet: str
    suggestion: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LintResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [v.as_dict() for v in self.violations]}


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------


def _strip_url_email_spans(text: str) -> str:
    """Replace URLs and emails with spaces of equal length so name-regexes
    don't match tokens that legitimately appear inside them
    (e.g. 'adam@example.com', 'github.com/adamhq/...')."""
    out = []
    pos = 0
    for m in URL_OR_EMAIL_RE.finditer(text):
        out.append(text[pos:m.start()])
        out.append(" " * (m.end() - m.start()))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _check_user_names(line: str, lineno: int) -> list[Violation]:
    masked = _strip_url_email_spans(line)
    found: list[Violation] = []
    for name in USER_NAMES:
        m = _name_regex(name).search(masked)
        if m:
            found.append(
                Violation(
                    rule="no_user_names",
                    line=lineno,
                    snippet=line.strip()[:200],
                    suggestion=(
                        f"Replace '{name}' with a role placeholder "
                        f"(e.g. ${{OPERATOR}}) or remove the reference."
                    ),
                )
            )
            break  # one violation per line is enough
    return found


def _check_curl_bash(line: str, lineno: int) -> list[Violation]:
    if CURL_BASH_RE.search(line):
        return [
            Violation(
                rule="no_curl_bash",
                line=lineno,
                snippet=line.strip()[:200],
                suggestion=(
                    "Replace `curl ... | bash` with a download → checksum → "
                    "execute three-step (sha256sum -c expected.sha256)."
                ),
            )
        ]
    return []


def _check_home_paths(line: str, lineno: int) -> list[Violation]:
    m = HOME_PATH_RE.search(line)
    if m:
        return [
            Violation(
                rule="no_hardcoded_home_paths",
                line=lineno,
                snippet=line.strip()[:200],
                suggestion="Replace hardcoded home path with ${HOME} or ~/.",
            )
        ]
    return []


def _check_internal_infra(line: str, lineno: int) -> list[Violation]:
    masked = _strip_url_email_spans(line)
    for token in INTERNAL_INFRA:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", masked):
            return [
                Violation(
                    rule="no_internal_infra_refs",
                    line=lineno,
                    snippet=line.strip()[:200],
                    suggestion=(
                        f"Replace '{token}' with a generic placeholder "
                        "(e.g. ${API_HOST}, ${TASK_TRACKER})."
                    ),
                )
            ]
    return []


def _check_agent_discipline(line: str, lineno: int) -> list[Violation]:
    if AGENT_DISCIPLINE_RE.search(line):
        return [
            Violation(
                rule="no_agent_discipline_text",
                line=lineno,
                snippet=line.strip()[:200],
                suggestion=(
                    "Encode the check as a deterministic script step rather than "
                    "relying on agent discipline."
                ),
            )
        ]
    return []


def _check_external_promo(line: str, lineno: int) -> list[Violation]:
    found: list[Violation] = []
    # Heuristic skips: CSV header rows (look like comma-separated identifiers),
    # placeholder/example URLs, and "--url <example>" usage strings. These are
    # documentation aids, not promotional links.
    stripped = line.strip()
    # CSV-shaped lines: 5+ commas and no markdown link syntax → likely a CSV row
    if stripped.count(",") >= 4 and "[" not in stripped and "(" not in stripped:
        return []
    for m in LINK_RE.finditer(line):
        host = m.group(1).lower()
        # Allowlisted placeholder/example domains — these are documentation, not promo.
        if host in PLACEHOLDER_DOMAINS:
            continue
        if host.endswith(".test") or host.endswith(".local") or host.endswith(".localhost"):
            continue
        if host.startswith("your") or host.startswith("sample.") or host.startswith("placeholder."):
            continue
        if any(host == d or host.endswith("." + d) for d in ALLOWED_LINK_DOMAINS):
            continue
        found.append(
            Violation(
                rule="no_external_promo",
                line=lineno,
                snippet=line.strip()[:200],
                suggestion=(
                    f"Domain '{host}' is not on the allowlist "
                    f"({', '.join(ALLOWED_LINK_DOMAINS)})."
                ),
            )
        )
    return found


def _check_report_back(line: str, lineno: int) -> list[Violation]:
    masked = _strip_url_email_spans(line)
    m = REPORT_BACK_RE.search(masked)
    if m:
        target = m.group(1)
        # Skip generic targets that aren't actually proper names.
        if target.lower() in {"someone", "anyone", "anybody", "back"}:
            return []
        return [
            Violation(
                rule="no_report_back_without_placeholder",
                line=lineno,
                snippet=line.strip()[:200],
                suggestion=(
                    "Use a placeholder such as ${OPERATOR_NOTIFY_CHANNEL} "
                    "rather than a hardcoded recipient."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Recipe / runtime checks
# ---------------------------------------------------------------------------


def _check_compat(recipe_yaml: str | None) -> list[Violation]:
    """recipe.yaml MUST declare runtime.compatibility.{os,arch,ram_gb,network}.

    Done with a regex scan to avoid a PyYAML dependency. We only verify that
    the four required keys appear under a `runtime:`/`compatibility:` block —
    a full YAML schema validator runs separately.
    """
    if recipe_yaml is None:
        return [
            Violation(
                rule="must_declare_compat",
                line=0,
                snippet="(no recipe.yaml provided)",
                suggestion=(
                    "Provide a recipe.yaml with a runtime.compatibility block "
                    "declaring os, arch, ram_gb, and network."
                ),
            )
        ]

    text = recipe_yaml
    # Locate the runtime: block first.
    runtime_m = re.search(r"^runtime\s*:\s*$", text, re.MULTILINE)
    if not runtime_m:
        return [
            Violation(
                rule="must_declare_compat",
                line=0,
                snippet="(missing top-level `runtime:` block)",
                suggestion="Add a runtime.compatibility section to recipe.yaml.",
            )
        ]

    # Extract everything indented under runtime.
    after = text[runtime_m.end():]
    # Stop at the next top-level key (line not starting with whitespace).
    next_top = re.search(r"^\S", after, re.MULTILINE)
    runtime_block = after[: next_top.start()] if next_top else after

    if not re.search(r"^\s+compatibility\s*:", runtime_block, re.MULTILINE):
        return [
            Violation(
                rule="must_declare_compat",
                line=0,
                snippet="runtime: block has no compatibility:",
                suggestion="Add runtime.compatibility with os, arch, ram_gb, network.",
            )
        ]

    missing = [
        key for key in ("os", "arch", "ram_gb", "network")
        if not re.search(rf"^\s+{key}\s*:", runtime_block, re.MULTILINE)
    ]
    if missing:
        return [
            Violation(
                rule="must_declare_compat",
                line=0,
                snippet=f"runtime.compatibility missing: {missing}",
                suggestion=(
                    "Add the missing keys ("
                    + ", ".join(missing)
                    + ") under runtime.compatibility."
                ),
            )
        ]
    return []


def _script_supports_help(path: Path) -> bool:
    """Static AST check: does this .py script handle --help?

    Heuristic: presence of argparse.ArgumentParser, click decorators, or a
    direct `--help` literal in the source. argparse and click both emit
    --help by default, so finding either means we trust the script handles it.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    # Cheap textual short-circuit before AST.
    if "argparse" in source or "click" in source or "--help" in source:
        return True

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        # `import argparse` / `from argparse import …`
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            names = [a.name for a in getattr(node, "names", [])]
            if mod.startswith("argparse") or any(n.startswith("argparse") for n in names):
                return True
            if mod.startswith("click") or any(n.startswith("click") for n in names):
                return True

    return False


def _check_help_text(skill_dir: Path | None) -> list[Violation]:
    if skill_dir is None or not skill_dir.is_dir():
        return []
    findings: list[Violation] = []
    for py in sorted(skill_dir.rglob("*.py")):
        # Skip __init__/conftest/tests — they aren't user-facing executables.
        if py.name in {"__init__.py", "conftest.py"}:
            continue
        if "tests" in py.parts or "test" in py.stem.lower():
            continue
        if not _script_supports_help(py):
            findings.append(
                Violation(
                    rule="must_have_help_text",
                    line=0,
                    snippet=str(py.relative_to(skill_dir)),
                    suggestion=(
                        "Add argparse (or click) so the script responds to --help."
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


PER_LINE_CHECKS = (
    _check_user_names,
    _check_curl_bash,
    _check_home_paths,
    _check_internal_infra,
    _check_agent_discipline,
    _check_external_promo,
    _check_report_back,
)


def lint_skill(
    readme_text: str,
    recipe_yaml: str | None = None,
    skill_dir: Path | None = None,
) -> dict[str, Any]:
    """Lint a skill's README/SKILL.md text plus optional recipe.yaml.

    Returns {"ok": bool, "violations": [{rule, line, snippet, suggestion}, ...]}.
    """
    violations: list[Violation] = []
    for lineno, raw in enumerate(readme_text.splitlines(), start=1):
        for check in PER_LINE_CHECKS:
            violations.extend(check(raw, lineno))

    violations.extend(_check_compat(recipe_yaml))
    violations.extend(_check_help_text(skill_dir))

    result = LintResult(ok=not violations, violations=violations)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Tarball entry point — used by the FastAPI publish endpoint.
# ---------------------------------------------------------------------------


def lint_tarball_bytes(tarball_bytes: bytes) -> dict[str, Any]:
    """Lint a published tarball given as bytes.

    Extracts SKILL.md / README.md and recipe.yaml in-memory and runs lint_skill.
    Returns the same dict shape as lint_skill. The help-text check is skipped
    here because static AST inspection on tarball-extracted bytes is best done
    by writing to a temp dir; the publisher's other gates already enforce
    structure, so we accept that gap.
    """
    import io
    import tarfile

    readme_text: str | None = None
    recipe_yaml: str | None = None

    try:
        tf = tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError):
        # Fail-open on unreadable tarballs — matches scan_tarball_bytes.
        return {"ok": True, "violations": []}

    readme_priority = ("SKILL.md", "README.md", "skill.md", "readme.md")
    readme_candidates: dict[str, str] = {}

    with tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            base = member.name.rsplit("/", 1)[-1]
            if base in readme_priority or base == "recipe.yaml":
                try:
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        continue
                    text = fobj.read().decode("utf-8", errors="replace")
                except (tarfile.TarError, OSError, UnicodeDecodeError):
                    continue
                if base == "recipe.yaml" and recipe_yaml is None:
                    recipe_yaml = text
                elif base in readme_priority:
                    readme_candidates[base] = text

    for name in readme_priority:
        if name in readme_candidates:
            readme_text = readme_candidates[name]
            break

    if readme_text is None:
        # No SKILL.md / README.md → can't lint prose; only enforce recipe.yaml
        # compatibility check. (Other gates will already complain about a
        # missing manifest.)
        violations = [v.as_dict() for v in _check_compat(recipe_yaml)]
        return {"ok": not violations, "violations": violations}

    return lint_skill(readme_text, recipe_yaml=recipe_yaml, skill_dir=None)


# ---------------------------------------------------------------------------
# Auto-fix (mechanical replacements only)
# ---------------------------------------------------------------------------


_HOME_FIX_RE = re.compile(r"(?<!\$\{)(?:/home|/Users)/[a-z][a-z0-9_-]*/")


def auto_fix(text: str) -> str:
    """Mechanical fixes only — path/name substitutions safe to apply blindly.

    Currently:
      - /home/<u>/   →  ${HOME}/
      - /Users/<u>/  →  ${HOME}/
    """
    return _HOME_FIX_RE.sub("${HOME}/", text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_inputs(target: Path) -> tuple[str, str | None, Path | None]:
    """Given a path to a skill dir or a single readme, return
    (readme_text, recipe_yaml_text_or_None, skill_dir_or_None).

    When passed a bare .md file, skill_dir is None unless the parent directory
    looks like a skill bundle (has SKILL.md AND a sibling recipe.yaml or skill.py).
    Otherwise we'd recursively scan whatever cwd happens to contain — typically
    a repo root with hundreds of unrelated .py files — and produce noise.
    """
    if target.is_file():
        parent = target.parent
        # Only treat parent as a skill bundle if it has the expected shape.
        looks_like_bundle = (
            (parent / "SKILL.md").is_file()
            and (
                (parent / "recipe.yaml").is_file()
                or (parent / "skill.py").is_file()
                or (parent / "skill.yaml").is_file()
            )
        )
        skill_dir = parent if looks_like_bundle else None
        return target.read_text(encoding="utf-8"), None, skill_dir
    if target.is_dir():
        readme: Path | None = None
        for name in ("SKILL.md", "README.md", "skill.md", "readme.md"):
            cand = target / name
            if cand.is_file():
                readme = cand
                break
        if readme is None:
            raise FileNotFoundError(
                f"No SKILL.md or README.md found under {target}"
            )
        recipe = target / "recipe.yaml"
        recipe_text = recipe.read_text(encoding="utf-8") if recipe.is_file() else None
        return readme.read_text(encoding="utf-8"), recipe_text, target
    raise FileNotFoundError(f"Not a file or directory: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skill_discipline_linter",
        description="Phase A.7 skill-discipline linter.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a skill directory (SKILL.md + recipe.yaml) or a single readme file.",
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Print a unified diff of mechanical-only fixes (paths/names).",
    )
    args = parser.parse_args(argv)

    try:
        readme_text, recipe_text, skill_dir = _resolve_inputs(args.path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.auto_fix:
        fixed = auto_fix(readme_text)
        diff = difflib.unified_diff(
            readme_text.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=str(args.path),
            tofile=str(args.path) + " (auto-fixed)",
        )
        sys.stdout.writelines(diff)
        return 0 if fixed == readme_text else 1

    result = lint_skill(readme_text, recipe_yaml=recipe_text, skill_dir=skill_dir)
    print(json.dumps(result["violations"], indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
