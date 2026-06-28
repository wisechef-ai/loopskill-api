"""Tests for the loopskill_* MCP tool rename.

Verifies:
  (a) The registry advertises loopskill_* canonical names (loopskill_install,
      loopskill_search, and a sample of others).
  (b) Back-compat recipes_* names are still advertised so existing agents work.
  (c) normalize_tool_name correctly maps loopskill_* → recipes_* for dispatch.
  (d) Both loopskill_install and recipes_install are accepted by _dispatch
      without raising 'unknown tool' ValueError (end-to-end alias test).
"""

from __future__ import annotations

import pytest

from app.mcp._alias_map import LOOPSKILL_TO_RECIPES, normalize_tool_name
from app.mcp.registry import _tool_definitions


# ── helpers ──────────────────────────────────────────────────────────────────


def _tool_names() -> set[str]:
    return {t.name for t in _tool_definitions()}


# ── (a) canonical loopskill_* names are present in registry ─────────────────


class TestLoopskillNamesRegistered:
    """Primary loopskill_* names must appear in the advertised tool list."""

    def test_loopskill_install_registered(self) -> None:
        assert "loopskill_install" in _tool_names()

    def test_loopskill_search_registered(self) -> None:
        assert "loopskill_search" in _tool_names()

    def test_loopskill_recall_registered(self) -> None:
        assert "loopskill_recall" in _tool_names()

    def test_loopskill_bundle_install_registered(self) -> None:
        assert "loopskill_bundle_install" in _tool_names()

    def test_loopskill_sync_registered(self) -> None:
        assert "loopskill_sync" in _tool_names()

    def test_loopskill_recipify_registered(self) -> None:
        assert "loopskill_recipify" in _tool_names()

    def test_loopskill_carousel_today_registered(self) -> None:
        assert "loopskill_carousel_today" in _tool_names()

    def test_loopskill_feedback_registered(self) -> None:
        assert "loopskill_feedback" in _tool_names()

    def test_loopskill_report_skill_error_registered(self) -> None:
        assert "loopskill_report_skill_error" in _tool_names()

    def test_loopskill_configure_feedback_registered(self) -> None:
        assert "loopskill_configure_feedback" in _tool_names()

    def test_loopskill_install_from_bundle_registered(self) -> None:
        assert "loopskill_install_from_bundle" in _tool_names()

    def test_loopskill_pick_best_from_bundle_registered(self) -> None:
        assert "loopskill_pick_best_from_bundle" in _tool_names()

    def test_loopskill_compose_bundle_from_links_registered(self) -> None:
        assert "loopskill_compose_bundle_from_links" in _tool_names()


# ── (b) back-compat recipes_* names still advertised ────────────────────────


class TestRecipesAliasesStillRegistered:
    """Back-compat recipes_* names must remain in the tool list."""

    def test_recipes_install_alias_present(self) -> None:
        assert "recipes_install" in _tool_names()

    def test_recipes_search_alias_present(self) -> None:
        assert "recipes_search" in _tool_names()

    def test_recipes_recall_alias_present(self) -> None:
        assert "recipes_recall" in _tool_names()

    def test_recipes_cookbook_install_alias_present(self) -> None:
        assert "recipes_cookbook_install" in _tool_names()

    def test_recipes_sync_alias_present(self) -> None:
        assert "recipes_sync" in _tool_names()

    def test_recipes_recipify_alias_present(self) -> None:
        assert "recipes_recipify" in _tool_names()

    def test_recipes_carousel_today_alias_present(self) -> None:
        assert "recipes_carousel_today" in _tool_names()

    def test_recipes_install_from_cookbook_alias_present(self) -> None:
        assert "recipes_install_from_cookbook" in _tool_names()

    def test_recipes_pick_best_from_cookbook_alias_present(self) -> None:
        assert "recipes_pick_best_from_cookbook" in _tool_names()

    def test_recipes_compose_cookbook_from_links_alias_present(self) -> None:
        assert "recipes_compose_cookbook_from_links" in _tool_names()

    def test_all_alias_map_entries_have_compat_in_registry(self) -> None:
        """Every loopskill_* in the alias map that is in the registry must
        also have its recipes_* counterpart in the registry."""
        names = _tool_names()
        missing: list[str] = []
        for loopskill_name, recipes_name in LOOPSKILL_TO_RECIPES.items():
            if loopskill_name in names and recipes_name not in names:
                missing.append(recipes_name)
        assert missing == [], f"Missing compat aliases: {missing}"


# ── (c) normalize_tool_name maps loopskill_* → recipes_* ────────────────────


class TestNormalizeToolName:
    """normalize_tool_name must map canonical → dispatch names correctly."""

    def test_loopskill_install_maps_to_recipes_install(self) -> None:
        assert normalize_tool_name("loopskill_install") == "recipes_install"

    def test_loopskill_search_maps_to_recipes_search(self) -> None:
        assert normalize_tool_name("loopskill_search") == "recipes_search"

    def test_loopskill_bundle_install_maps_to_recipes_cookbook_install(self) -> None:
        assert normalize_tool_name("loopskill_bundle_install") == "recipes_cookbook_install"

    def test_loopskill_install_from_bundle_maps(self) -> None:
        assert normalize_tool_name("loopskill_install_from_bundle") == "recipes_install_from_cookbook"

    def test_loopskill_list_bundle_maps(self) -> None:
        assert normalize_tool_name("loopskill_list_bundle") == "recipes_list_cookbook"

    def test_loopskill_compose_bundle_from_links_maps(self) -> None:
        assert normalize_tool_name("loopskill_compose_bundle_from_links") == "recipes_compose_cookbook_from_links"

    def test_loopskill_pick_best_from_bundle_maps(self) -> None:
        assert normalize_tool_name("loopskill_pick_best_from_bundle") == "recipes_pick_best_from_cookbook"

    def test_loopskill_sync_maps(self) -> None:
        assert normalize_tool_name("loopskill_sync") == "recipes_sync"

    def test_loopskill_configure_feedback_maps(self) -> None:
        assert normalize_tool_name("loopskill_configure_feedback") == "recipes_configure_feedback"

    def test_loopskill_bundle_attach_maps(self) -> None:
        assert normalize_tool_name("loopskill_bundle_attach") == "recipes_cookbook_attach"

    def test_loopskill_bundle_handoff_maps(self) -> None:
        assert normalize_tool_name("loopskill_bundle_handoff") == "recipes_cookbook_handoff"

    def test_recipes_names_pass_through_unchanged(self) -> None:
        """recipes_* names are already the dispatch names — must not be modified."""
        assert normalize_tool_name("recipes_install") == "recipes_install"
        assert normalize_tool_name("recipes_search") == "recipes_search"
        assert normalize_tool_name("recipes_cookbook_install") == "recipes_cookbook_install"

    def test_unrecognised_name_passes_through(self) -> None:
        """Unknown names pass through so _dispatch can raise 'unknown tool'."""
        assert normalize_tool_name("totally_unknown_tool") == "totally_unknown_tool"

    def test_loopskill_non_aliased_passes_through(self) -> None:
        """loopskill_search_loops / loopskill_get_loop are NOT aliased — they
        have their own dispatch branches and must pass through unchanged."""
        assert normalize_tool_name("loopskill_search_loops") == "loopskill_search_loops"
        assert normalize_tool_name("loopskill_get_personality") == "loopskill_get_personality"


# ── (d) _dispatch accepts both loopskill_* and recipes_* without 'unknown tool' ─


class TestDispatchAliasRouting:
    """Both canonical and back-compat names must NOT raise 'unknown tool'."""

    def _call(self, name: str, args: dict) -> object:
        """Invoke call_tool_sync with a stub DB that immediately raises StopIteration
        so we can detect whether _dispatch reached a handler vs raised ValueError."""
        from unittest.mock import MagicMock, patch

        import app.mcp.server as srv_mod

        # Patch the target function so it returns a sentinel instead of hitting DB.
        sentinel = {"_test_sentinel": True}

        if name in ("loopskill_install", "recipes_install"):
            target = "recipes_install"
        elif name in ("loopskill_search", "recipes_search"):
            target = "recipes_search"
        else:
            target = name  # shouldn't reach this in these tests

        with patch.object(srv_mod, target, return_value=sentinel):
            db = MagicMock()
            return srv_mod._dispatch(name, db, args, {"scope": "master"})

    def test_loopskill_install_dispatches_without_unknown_tool(self) -> None:
        result = self._call("loopskill_install", {"slug": "test-skill"})
        assert result == {"_test_sentinel": True}

    def test_recipes_install_still_dispatches(self) -> None:
        result = self._call("recipes_install", {"slug": "test-skill"})
        assert result == {"_test_sentinel": True}

    def test_loopskill_search_dispatches_without_unknown_tool(self) -> None:
        result = self._call("loopskill_search", {"query": "python"})
        assert result == {"_test_sentinel": True}

    def test_recipes_search_still_dispatches(self) -> None:
        result = self._call("recipes_search", {"query": "python"})
        assert result == {"_test_sentinel": True}

    def test_unknown_tool_still_raises(self) -> None:
        """Sanity: truly unknown tool names still raise ValueError."""
        from unittest.mock import MagicMock

        import app.mcp.server as srv_mod

        with pytest.raises(ValueError, match="unknown tool"):
            srv_mod._dispatch("no_such_tool_xyz", MagicMock(), {}, {"scope": "master"})
