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
    bad = re.compile(
        r"auto-updat|self-updating skill library|downloads and applies new versions|zero config drift", re.I
    )
    hits = [f"{p.name}: {m.group(0)}" for p in FILES for m in bad.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_no_doc_uses_the_nonexistent_mode_argument():
    hits = [
        p.name for p in FILES if re.search(r'"mode"\s*:\s*"(APPLY|DRY_RUN)"', p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_sync_docs_say_reinstall_updates_local_files():
    """Each doc must say sync moves pins and that re-running bundle_install updates files."""
    for name in ("README.md", "SKILL.md", "QUICKSTART-subscriber.md"):
        text = _read(name)
        sync_lines = [ln for ln in text.splitlines() if "loopskill_sync" in ln and "pin" in ln.lower()]
        assert sync_lines, f"{name}: no line says loopskill_sync moves pins"
        assert re.search(r"re-run(ning)? `?loopskill_bundle_install", text, re.I), name


def test_quickstart_uses_real_argument_names():
    """Dispatch reads args['slug'] for install; recall requires `query` (else query_required)."""
    text = _read("QUICKSTART-subscriber.md")
    assert "skill_slug" not in text
    assert re.search(r'loopskill_install\(\{"slug":', text)
    assert re.search(r'loopskill_recall\(\{"query":', text)
    # the bundle used for updates must be created before it is updated
    assert text.index("loopskill_compose_bundle_from_links") < text.index("loopskill_sync")


def test_stub_tools_are_not_advertised():
    """loopskill_subskill_resolve ignores its input and returns a constant (Phase C stub)."""
    for p in FILES:
        text = p.read_text(encoding="utf-8")
        for stale in ("subskill_resolve", "subrecipe_resolve"):
            assert stale not in text, f"{p.name}: {stale}"


def test_readme_pricing_names_both_caps_and_the_pro_only_tools():
    text = _read("README.md")
    assert re.search(r"API keys \(Free 1, Pro 10\)", text)
    assert re.search(r"private bundles \(Free 2, Pro 50\)", text)
    # fork/recipify/sync-SSE are Pro-gated but tombstoned (docs/ops/tombstones.md): disclose, never sell
    assert "don't buy it for those" in text
    assert "unlocks the hosted authoring tools" not in text
    disclosure = re.search(r"endpoints \(([^)]*)\) also require Pro", text)
    assert disclosure, "Pro-gated endpoint disclosure missing"
    for name in ("skill forks", "draft-to-skill", "sync streaming", "bundle deploys"):
        assert name in disclosure.group(1), name
    assert "whole difference" not in text.lower()


def test_tool_table_names_only_registered_mcp_tools():
    from app.mcp.registry import _tool_definitions  # the exact list tools/list serves

    registered = {t.name for t in _tool_definitions()}
    table = _read("README.md").split("## MCP tools", 1)[1].split("\n---\n", 1)[0]
    names = re.findall(r"^\| `([a-z_]+)` \|", table, re.M)
    assert names, "tool table not found"
    missing = [n for n in names if n not in registered]
    assert missing == []


def test_skill_md_tool_names_are_registered():
    """SKILL.md is served verbatim at /skill — every `loopskill_*` it names must be a live tool."""
    from app.mcp.registry import _tool_definitions

    registered = {t.name for t in _tool_definitions()}
    named = set(re.findall(r"`(loopskill_[a-z_]+)`", _read("SKILL.md")))
    assert named, "no tool names found"
    assert sorted(named - registered) == []


def test_quickstart_scopes_its_two_machine_claim():
    text = _read("QUICKSTART-subscriber.md")
    assert "Every call below was run" not in text
    assert "bundle steps" in text


def test_skill_md_has_no_tool_inventory_count_and_recall_uses_tier_filter():
    text = _read("SKILL.md")
    assert not re.search(r"^#+\s*\d+\s+MCP tools", text, re.M)
    assert "`tier_filter`" in text
    assert "`tier` parameter of `loopskill_recall`" not in text
