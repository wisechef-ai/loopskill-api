"""Tests for the lsrename_0713 full API wire cutover: recipes_* -> loopskill_*.

This sprint DROPPED the recipes_*/loopskill_* back-compat alias layer that a
prior phase (loopskill_0622) had shipped. Verifies:

  (a) The registry advertises loopskill_* canonical names ONLY.
  (b) No recipes_* names are advertised in tools/list any more.
  (c) normalize_tool_name no longer maps any recipes_*<->loopskill_* pair —
      it only resolves the (unrelated) legacy loop_* verifier names.
  (d) _dispatch raises 'unknown tool' for every old recipes_* name across the
      top-5 tools (install, bundle_install, search, sync, skillify) — the
      falsifiable proof that back-compat was DROPPED, not aliased.
"""

from __future__ import annotations

import pytest

from app.mcp._alias_map import LOOP_TO_VERIFIER, normalize_tool_name
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

    def test_loopskill_skillify_registered(self) -> None:
        assert "loopskill_skillify" in _tool_names()

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

    def test_loopskill_subskill_resolve_registered(self) -> None:
        assert "loopskill_subskill_resolve" in _tool_names()

    def test_loopskill_request_skill_registered(self) -> None:
        assert "loopskill_request_skill" in _tool_names()


# ── (b) back-compat recipes_* names are GONE from the registry ──────────────


# The full canonical verb map (lsrename_0713 §4 of the plan-doc) — every
# recipes_*/cookbook name that used to exist. None of these may appear in
# tools/list any more.
_DEAD_RECIPES_NAMES = [
    "recipes_install",
    "recipes_cookbook_install",
    "recipes_install_from_cookbook",
    "recipes_search",
    "recipes_sync",
    "recipes_recipify",
    "recipes_cookbook_attach",
    "recipes_compose_cookbook_from_links",
    "recipes_cookbook_handoff",
    "recipes_list_cookbook",
    "recipes_fleet_list",
    "recipes_fleet_sync",
    "recipes_fleet_subscribe",
    "recipes_fleet_create",
    "recipes_tailor",
    "recipes_tailor_version",
    "recipes_doctor",
    "recipes_feedback",
    "recipes_configure_feedback",
    "recipes_report_skill_error",
    "recipes_propose_skill_patch",
    "recipes_publish_request",
    "recipes_request_recipe",
    "recipes_subrecipe_resolve",
    "recipes_share_rotate",
    "recipes_share_revoke",
    "recipes_share_create",
    "recipes_share_list",
    "recipes_recall",
    "recipes_seeker",
    "recipes_carousel_today",
    "recipes_fork_list",
    "recipes_like",
    "recipes_pick_best_from_cookbook",
]


class TestRecipesNamesNoLongerRegistered:
    """No recipes_* name may survive in tools/list — back-compat is dropped."""

    @pytest.mark.parametrize("dead_name", _DEAD_RECIPES_NAMES)
    def test_recipes_name_not_in_registry(self, dead_name: str) -> None:
        assert dead_name not in _tool_names(), (
            f"{dead_name} is still advertised in tools/list — "
            "back-compat alias must be fully removed (lsrename_0713)"
        )

    def test_no_tool_name_starts_with_recipes_prefix(self) -> None:
        """Blanket sweep: nothing in the advertised catalogue may use the
        dead recipes_ prefix at all, registered or not explicitly listed above."""
        leftover = [n for n in _tool_names() if n.startswith("recipes_")]
        assert leftover == [], f"recipes_* tool names still registered: {leftover}"

    def test_no_tool_uses_cookbook_verb_prefix(self) -> None:
        """cookbook_* wire-name fragments (the recipes_cookbook_* pattern) must
        also be fully folded into bundle_* — this is the "fold cookbook->bundle
        in tool names" half of the cutover."""
        leftover = [n for n in _tool_names() if "cookbook" in n]
        assert leftover == [], f"cookbook-named tools still registered: {leftover}"


# ── (c) normalize_tool_name no longer knows about recipes_*/loopskill_* ─────


class TestNormalizeToolName:
    """normalize_tool_name only resolves legacy loop_* verifier names now."""

    def test_loopskill_names_pass_through_unchanged(self) -> None:
        assert normalize_tool_name("loopskill_install") == "loopskill_install"
        assert normalize_tool_name("loopskill_search") == "loopskill_search"
        assert normalize_tool_name("loopskill_bundle_install") == "loopskill_bundle_install"
        assert normalize_tool_name("loopskill_sync") == "loopskill_sync"
        assert normalize_tool_name("loopskill_skillify") == "loopskill_skillify"

    def test_recipes_names_pass_through_unresolved(self) -> None:
        """Old recipes_* names are NOT mapped to anything — they pass through
        unchanged so _dispatch's if-chain (which now only tests loopskill_*)
        falls through to 'unknown tool'."""
        assert normalize_tool_name("recipes_install") == "recipes_install"
        assert normalize_tool_name("recipes_search") == "recipes_search"
        assert normalize_tool_name("recipes_cookbook_install") == "recipes_cookbook_install"
        assert normalize_tool_name("recipes_sync") == "recipes_sync"
        assert normalize_tool_name("recipes_recipify") == "recipes_recipify"

    def test_unrecognised_name_passes_through(self) -> None:
        assert normalize_tool_name("totally_unknown_tool") == "totally_unknown_tool"

    def test_loop_to_verifier_mapping_still_works(self) -> None:
        """activate_0701 Phase A1's legacy loop_* -> verifier mapping is
        unrelated to lsrename_0713 and must be untouched by this cutover."""
        assert normalize_tool_name("loopskill_search_loops") == "loopskill_search_verifiers"
        assert normalize_tool_name("loopskill_get_loop") == "loopskill_get_verifier"
        assert normalize_tool_name("loopskill_search_verifiers") == "loopskill_search_verifiers"
        assert normalize_tool_name("loopskill_get_personality") == "loopskill_get_personality"

    def test_loop_to_verifier_map_has_no_recipes_entries(self) -> None:
        """Guard against a future regression re-adding recipes_* entries to
        the (unrelated) loop-verifier map."""
        for k, v in LOOP_TO_VERIFIER.items():
            assert not k.startswith("recipes_"), f"unexpected recipes_* key in LOOP_TO_VERIFIER: {k}"
            assert not v.startswith("recipes_"), f"unexpected recipes_* value in LOOP_TO_VERIFIER: {v}"


# ── (d) _dispatch: new names work, OLD names are NOT dispatchable ───────────


class TestNewNamesDispatch:
    """loopskill_* names must dispatch successfully (no 'unknown tool')."""

    def _call(self, name: str, target: str, args: dict) -> object:
        from unittest.mock import MagicMock, patch

        import app.mcp.server as srv_mod

        sentinel = {"_test_sentinel": True}
        with patch.object(srv_mod, target, return_value=sentinel):
            db = MagicMock()
            return srv_mod._dispatch(name, db, args, {"scope": "master"})

    def test_loopskill_install_dispatches(self) -> None:
        result = self._call("loopskill_install", "loopskill_install", {"slug": "test-skill"})
        assert result == {"_test_sentinel": True}

    def test_loopskill_search_dispatches(self) -> None:
        result = self._call("loopskill_search", "loopskill_search", {"query": "python"})
        assert result == {"_test_sentinel": True}


class TestOldRecipesNamesNotDispatchable:
    """FALSIFIABLE PROOF that back-compat was DROPPED, not aliased.

    Every recipes_* name that used to dispatch successfully must now raise
    ValueError('unknown tool: ...') — there is no handler branch left for it
    and no alias map resolves it to one. Covers the top-5 tools named in the
    sprint brief: install, bundle_install, search, sync, skillify.
    """

    def _assert_unknown_tool(self, old_name: str, args: dict) -> None:
        from unittest.mock import MagicMock

        import app.mcp.server as srv_mod

        with pytest.raises(ValueError, match="unknown tool"):
            srv_mod._dispatch(old_name, MagicMock(), args, {"scope": "master"})

    def test_recipes_install_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_install", {"slug": "test-skill"})

    def test_recipes_cookbook_install_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_cookbook_install", {"cookbook_id": "x"})

    def test_recipes_search_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_search", {"query": "python"})

    def test_recipes_sync_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_sync", {"cookbook_id": "x"})

    def test_recipes_recipify_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_recipify", {"slug": "x", "content": "y"})

    def test_recipes_list_cookbook_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_list_cookbook", {})

    def test_recipes_recall_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_recall", {"query": "x"})

    def test_recipes_doctor_not_dispatchable(self) -> None:
        self._assert_unknown_tool("recipes_doctor", {"install_dir": "/tmp/x"})

    def test_unknown_tool_still_raises_for_truly_unknown_names(self) -> None:
        """Sanity: truly unknown tool names still raise ValueError (unchanged)."""
        from unittest.mock import MagicMock

        import app.mcp.server as srv_mod

        with pytest.raises(ValueError, match="unknown tool"):
            srv_mod._dispatch("no_such_tool_xyz", MagicMock(), {}, {"scope": "master"})


class TestCallToolSyncRejectsOldNames:
    """The public call_tool_sync entry point (used by the stdio loop / tests)
    must also reject old recipes_* names end-to-end, not just the internal
    _dispatch helper."""

    def test_call_tool_sync_rejects_recipes_install(self) -> None:
        from unittest.mock import MagicMock

        import app.mcp.server as srv_mod

        db = MagicMock()
        with pytest.raises(ValueError, match="unknown tool"):
            srv_mod.call_tool_sync(
                "recipes_install", {"slug": "test-skill"}, caller={"scope": "master"}, db=db
            )

    def test_call_tool_sync_rejects_recipes_cookbook_install(self) -> None:
        from unittest.mock import MagicMock

        import app.mcp.server as srv_mod

        db = MagicMock()
        with pytest.raises(ValueError, match="unknown tool"):
            srv_mod.call_tool_sync(
                "recipes_cookbook_install", {"cookbook_id": "x"}, caller={"scope": "master"}, db=db
            )
