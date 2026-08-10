"""bundles0811 Phase P3 — federated install-instruction resolution.

Covers item 1 of the P3 brief: resolve an install INSTRUCTION (never bytes)
for a federated bundle entry. Every network call is mocked — no test here
hits GitHub.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services import federation_hub_install as fhi


def setup_function(_fn):
    fhi._ref_cache.clear()


# ── repo+path present: direct resolution ────────────────────────────────


def test_direct_main_resolves_without_tree_walk():
    """The 84/88 case: main resolves on the first HEAD probe."""
    with (
        patch.object(fhi, "_probe_branch", side_effect=lambda repo, path, branch: branch == "main"),
        patch.object(fhi, "_tree_walk_fallback") as tree_walk,
    ):
        instr = fhi.resolve_install_instruction(
            repo="erichowens/some_claude_skills",
            path=".claude/skills/cv-creator",
            origin_url="https://github.com/erichowens/some_claude_skills",
        )
    assert instr.kind == "fetch"
    assert instr.branch == "main"
    assert instr.url == (
        "https://raw.githubusercontent.com/erichowens/some_claude_skills/main/"
        ".claude/skills/cv-creator/SKILL.md"
    )
    tree_walk.assert_not_called()  # never burn a tree call when direct hits


def test_direct_master_resolves_after_main_miss():
    """The 4/88 case: main misses, master hits — still zero tree calls."""

    def probe(repo, path, branch):
        return branch == "master"

    with (
        patch.object(fhi, "_probe_branch", side_effect=probe),
        patch.object(fhi, "_tree_walk_fallback") as tree_walk,
    ):
        instr = fhi.resolve_install_instruction(
            repo="owner/repo", path="skills/thing", origin_url="https://github.com/owner/repo"
        )
    assert instr.kind == "fetch"
    assert instr.branch == "master"
    tree_walk.assert_not_called()


def test_ref_cache_short_circuits_repeat_repo_lookups():
    """Once a repo's branch is known, a second call for the SAME repo must
    not re-probe every branch — this is the 'do not re-probe every request'
    requirement from the brief."""
    calls = []

    def probe(repo, path, branch):
        calls.append(branch)
        return branch == "main"

    with patch.object(fhi, "_probe_branch", side_effect=probe):
        fhi.resolve_install_instruction(
            repo="google/skills", path="skills/cloud/gke-networking", origin_url=""
        )
        first_call_count = len(calls)
        fhi.resolve_install_instruction(repo="google/skills", path="skills/cloud/other", origin_url="")

    # Second call should only re-validate the CACHED branch (one probe), not
    # loop through both candidates again.
    assert len(calls) == first_call_count + 1


# ── direct miss: bounded tree-walk fallback ──────────────────────────────


def test_direct_miss_recovers_via_one_tree_walk():
    with (
        patch.object(fhi, "_probe_branch", return_value=False),
        patch.object(
            fhi,
            "_tree_walk_fallback",
            side_effect=lambda repo, path, branch: "new/location" if branch == "main" else None,
        ) as tree_walk,
    ):
        instr = fhi.resolve_install_instruction(
            repo="owner/moved-repo", path="old/location", origin_url="https://github.com/owner/moved-repo"
        )
    assert instr.kind == "fetch"
    assert instr.path == "new/location"
    assert instr.branch == "main"
    assert tree_walk.call_count == 1  # bounded — stops at the first hit


def test_direct_and_tree_walk_miss_degrades_to_origin():
    """10/12 measured misses: stale index rows. Must degrade to origin, never
    fabricate a raw URL."""
    with (
        patch.object(fhi, "_probe_branch", return_value=False),
        patch.object(fhi, "_tree_walk_fallback", return_value=None),
    ):
        instr = fhi.resolve_install_instruction(
            repo="nvidia/skills", path="deleted/skill", origin_url="https://github.com/nvidia/skills"
        )
    assert instr.kind == "origin"
    assert instr.url == "https://github.com/nvidia/skills"


# ── no coordinates: origin-only sources (clawhub/lobehub/browse-sh) ─────


def test_no_coordinates_uses_origin_url():
    instr = fhi.resolve_install_instruction(
        repo=None, path=None, origin_url="https://clawhub.ai/skills/some-owner/some-skill"
    )
    assert instr.kind == "origin"
    assert instr.url == "https://clawhub.ai/skills/some-owner/some-skill"


def test_no_coordinates_no_origin_url_never_fabricates_a_url():
    instr = fhi.resolve_install_instruction(repo=None, path=None, origin_url=None)
    assert instr.kind == "origin"
    assert instr.url == ""


def test_repo_only_no_path_falls_back_to_repo_page():
    instr = fhi.resolve_install_instruction(repo="owner/repo", path=None, origin_url=None)
    assert instr.kind == "origin"
    assert instr.url == "https://github.com/owner/repo"


# ── zero-federated-bytes assertion (the hard requirement) ───────────────


def test_resolution_never_calls_a_content_fetcher():
    """LoopSkill fetches nothing and stores nothing for a federated entry.
    Assert the resolver's own network surface is HEAD-only + tree-listing
    JSON — no ``guarded_get`` (a body-returning GET) is ever imported or
    called from this module's resolution path."""
    import inspect

    src = inspect.getsource(fhi)
    assert "guarded_get(" not in src, "federation_hub_install must never call guarded_get (fetches a body)"
    assert "guarded_head" in src, "resolution must use the zero-byte HEAD probe"


def test_to_dict_shape_is_agent_executable_instruction():
    instr = fhi.InstallInstruction(kind="fetch", url="https://raw.githubusercontent.com/a/b/main/c/SKILL.md")
    d = instr.to_dict()
    assert d["kind"] == "fetch"
    assert d["url"].endswith("SKILL.md")
    assert set(d.keys()) == {"kind", "url", "repo", "path", "branch"}
