"""Every repo file the app reads at runtime must trigger a deploy when it changes.

perclient_1110 (2026-10-12): #403 changed docs/recipes-skill/SKILL.md, CI went
green, deploy.yml's paths filter skipped it, and /skill kept serving the old text.

The guard is derived from CODE, not comments: it walks the AST of every module
under app/, reconstructs path constructions (``ROOT / "docs" / "x" / "SKILL.md"``,
``Path("retired-skills.txt")``, ``os.path.join(..., "config", "tiers.yaml")``)
and string literals, keeps the ones that resolve to a real file in the repo, and
asserts each is covered by a deploy.yml ``on.push.paths`` pattern. Comments and
docstrings are not AST string nodes in a path position, so they cannot satisfy it.

Known blind spots (none exist in app/ today, 2026-10-12 review): whole-directory
reads (glob/iterdir/StaticFiles mounts), f-strings with variables, .with_suffix(),
keyword-argument paths. Add a case here if app/ starts using one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def _deploy_paths() -> list[str]:
    wf = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    return on["push"]["paths"]


def _covered(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        rx = "^" + re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
        if re.match(rx, rel):
            return True
    return False


def _str_segments(node: ast.AST) -> list[str] | None:
    """String segments of a ``a / "x" / "y"`` chain, a join(...) call, or a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = _str_segments(node.right)
        if right is None:
            return None
        return (_str_segments(node.left) or []) + right
    if isinstance(node, ast.Call):
        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        return args or None
    return None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def runtime_repo_reads() -> set[tuple[str, str]]:
    """(module relative to repo root, repo file) for every runtime repo read."""
    found: set[tuple[str, str]] = set()
    for py in APP.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            seg = _str_segments(node)
            if not seg:
                continue
            # URL / HTTP route literals ("/api/bundles/install.sh", "https://...")
            # name endpoints, not repo files.
            if any(s.startswith(("/api/", "/install", "http://", "https://")) or "://" in s for s in seg):
                continue
            parts = [p for s in seg for p in s.split("/") if p and p not in (".", "..")]
            # every suffix: the anchor (ROOT, parents[1], __file__...) is not a literal
            for i in range(len(parts)):
                rel = "/".join(parts[i:])
                if "\n" in rel or " " in rel or rel.endswith(".py") or rel.startswith(("tests/", ".git/")):
                    continue
                if (ROOT / rel).is_file():
                    found.add((py.relative_to(ROOT).as_posix(), rel))
    return found


def runtime_repo_files() -> set[str]:
    return {rel for _mod, rel in runtime_repo_reads()}


def test_scanner_finds_known_runtime_reads():
    """Non-tautology anchor: each is read at runtime through a segment-built path."""
    files = runtime_repo_files()
    for rel in ("docs/recipes-skill/SKILL.md", "docs/fleet-skill/SKILL.md", "retired-skills.txt"):
        assert rel in files, f"scanner missed {rel}"


# Literals that happen to name a repo file but are NOT read from the repo at
# runtime. Each needs a reason; a stale entry fails test_exemptions_are_live.
# Exempt per (FILE, MODULE PREFIX): a real read of the same file from any other
# module is NOT exempt. The ledger path appears in every @tombstoned route
# module, so that entry is scoped to the decorator's ledger= keyword usage below.
NOT_RUNTIME_READS: dict[str, tuple[str, ...]] = {
    # skill_patch_validation.py blocks a skill-TARBALL member of this name.
    "install.sh": ("app/skill_patch_validation.py",),
    # @tombstoned(ledger=...) stores the path as a label in TOMBSTONES; never opened.
    "docs/ops/tombstones.md": ("@tombstoned-ledger",),
}


def _tombstone_ledger_only(mod: str) -> bool:
    """True if every occurrence of the ledger path in *mod* is a ledger= keyword."""
    tree = ast.parse((ROOT / mod).read_text(encoding="utf-8"))
    total = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith("tombstones.md")
    )
    as_ledger = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.keyword)
        and n.arg == "ledger"
        and isinstance(n.value, ast.Constant)
        and str(n.value.value).endswith("tombstones.md")
    )
    return total == as_ledger


def _exempt(mod: str, rel: str) -> bool:
    allowed = NOT_RUNTIME_READS.get(rel)
    if not allowed:
        return False
    if allowed == ("@tombstoned-ledger",):
        return _tombstone_ledger_only(mod)
    return mod in allowed


def test_exemptions_are_live():
    reads = runtime_repo_reads()
    for rel in NOT_RUNTIME_READS:
        assert any(r == rel and _exempt(m, r) for m, r in reads), f"stale exemption {rel}: drop it"


def test_runtime_repo_files_trigger_deploy():
    paths = _deploy_paths()
    missing = sorted(
        f"{rel} (read in {mod})"
        for mod, rel in runtime_repo_reads()
        if not _exempt(mod, rel) and not _covered(rel, paths)
    )
    assert missing == [], f"files read by app/ at runtime but not in deploy.yml paths: {missing}"


def test_deploy_runs_alembic_so_its_config_triggers_deploy():
    assert _covered("alembic.ini", _deploy_paths())
