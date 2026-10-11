"""perclient_1110 / M0.5.5 — the agent-facing docs must tell the truth about
`loopskill_sync` and about Free vs Pro.

Measured 2026-10-11 (perclient_1110 M1.6, loopskill-api#400): `loopskill_sync`
moves a bundle's version pins on the server and writes nothing on the agent's
machine; re-running `loopskill_bundle_install` is what updates local files.
The docs said "Auto-update installed skills" and documented a non-existent
`{"mode": "APPLY"}` argument (the real schema is `bundle_id` + `dry_run`).
These docs ship to agents verbatim (/skill serves docs/recipes-skill/SKILL.md).
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs" / "recipes-skill"
FILES = sorted(DOCS.glob("*.md"))


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_scans_real_files():
    names = {p.name for p in FILES}
    assert {"README.md", "SKILL.md", "QUICKSTART-subscriber.md"} <= names


def test_no_doc_claims_sync_auto_updates_installed_skills():
    bad = re.compile(r"auto-update|self-updating skill library|downloads and applies new versions", re.I)
    hits = [f"{p.name}: {m.group(0)}" for p in FILES for m in bad.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_no_doc_uses_the_nonexistent_mode_argument():
    hits = [
        p.name for p in FILES if re.search(r'"mode"\s*:\s*"(APPLY|DRY_RUN)"', p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_sync_docs_say_reinstall_updates_local_files():
    for name in ("README.md", "SKILL.md", "QUICKSTART-subscriber.md"):
        text = _read(name)
        assert "loopskill_sync" in text, name
        assert "loopskill_bundle_install" in text, name


def test_readme_pricing_names_both_caps_and_the_pro_only_tools():
    text = _read("README.md")
    assert re.search(r"API keys \(Free 1, Pro 10\)", text)
    assert re.search(r"private bundles \(Free 2, Pro 50\)", text)
    assert "forking a skill into your own editable copy" in text
    assert "whole difference" not in text.lower()


def test_tool_table_names_only_registered_mcp_tools():
    from app import mcp as mcp_pkg

    root = Path(mcp_pkg.__file__).resolve().parent
    src = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    table = _read("README.md").split("## MCP tools", 1)[1].split("\n---\n", 1)[0]
    names = re.findall(r"^\| `([a-z_]+)` \|", table, re.M)
    assert names, "tool table not found"
    missing = [n for n in names if f'"{n}"' not in src]
    assert missing == []
