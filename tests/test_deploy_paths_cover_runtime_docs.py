"""Every repo file the app reads at runtime must trigger a deploy when it changes.

perclient_1110 (2026-10-12): #403 changed docs/recipes-skill/SKILL.md, CI went
green, deploy.yml's paths filter skipped it, and /skill kept serving the old text.

The guard is derived from CODE, not comments: it walks the AST of every module
under app/, reconstructs path constructions (``ROOT / "docs" / "x" / "SKILL.md"``,
``Path("retired-skills.txt")``, ``os.path.join(..., "config", "tiers.yaml")``)
and string literals, keeps the ones that resolve to a real file in the repo, and
asserts each is covered by a deploy.yml ``on.push.paths`` pattern. Comments and
docstrings are not AST string nodes in a path position, so they cannot satisfy it.
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


def runtime_repo_files() -> set[str]:
    found: set[str] = set()
    for py in APP.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            seg = _str_segments(node)
            if not seg:
                continue
            parts = [p for s in seg for p in s.split("/") if p and p not in (".", "..")]
            # every suffix: the anchor (ROOT, parents[1], __file__...) is not a literal
            for i in range(len(parts)):
                rel = "/".join(parts[i:])
                if "\n" in rel or " " in rel or rel.endswith(".py") or rel.startswith(("tests/", ".git/")):
                    continue
                if (ROOT / rel).is_file():
                    found.add(rel)
    return found


def test_scanner_finds_known_runtime_reads():
    """Non-tautology anchor: each is read at runtime through a segment-built path."""
    files = runtime_repo_files()
    for rel in ("docs/recipes-skill/SKILL.md", "docs/fleet-skill/SKILL.md", "retired-skills.txt"):
        assert rel in files, f"scanner missed {rel}"


# Literals that happen to name a repo file but are NOT read from the repo at
# runtime. Each needs a reason; a stale entry fails test_exemptions_are_live.
NOT_RUNTIME_READS = {
    # skill_patch_validation.py blocks a skill-TARBALL member of this name.
    "install.sh": "app/skill_patch_validation.py",
    # @tombstoned(ledger=...) stores the path as a label in TOMBSTONES; never opened.
    "docs/ops/tombstones.md": "app/tombstone.py",
}


def test_exemptions_are_live():
    files = runtime_repo_files()
    for rel, why in NOT_RUNTIME_READS.items():
        assert rel in files, f"stale exemption {rel} ({why}): drop it"


def test_runtime_repo_files_trigger_deploy():
    paths = _deploy_paths()
    missing = sorted(f for f in runtime_repo_files() - NOT_RUNTIME_READS.keys() if not _covered(f, paths))
    assert missing == [], f"files read by app/ at runtime but not in deploy.yml paths: {missing}"


def test_deploy_runs_alembic_so_its_config_triggers_deploy():
    assert _covered("alembic.ini", _deploy_paths())
