"""Phase E (loopskill_activate_0701) — RESIDENCY GATE tests.

Fail-closed server-side residency enforcement. EU fleets cannot receive
non-EU-tagged connectors or composite loops with derived non-EU residency.
"""

from __future__ import annotations

import pytest

from app.services.residency_gate import filter_diff_by_residency


class TestResidencyGate:
    def test_eu_fleet_filters_non_eu_connector(self):
        diff = {"add": [{"slug": "zai-websearch", "residency_tag": "non-eu"}]}
        filtered, blocked = filter_diff_by_residency(diff, "eu")
        assert filtered["add"] == []
        assert len(blocked) == 1
        assert blocked[0]["slug"] == "zai-websearch"
        assert blocked[0]["reason"] == "residency_blocked_non_eu"

    def test_eu_fleet_includes_eu_connector(self):
        diff = {"add": [{"slug": "eu-connector", "residency_tag": "eu"}]}
        filtered, blocked = filter_diff_by_residency(diff, "eu")
        assert len(filtered["add"]) == 1
        assert blocked == []

    def test_eu_fleet_includes_null_residency_skill(self):
        diff = {"add": [{"slug": "some-skill"}]}
        filtered, blocked = filter_diff_by_residency(diff, "eu")
        assert len(filtered["add"]) == 1
        assert blocked == []

    def test_eu_fleet_blocks_composite_loop_with_derived_non_eu(self):
        diff = {
            "add": [{"slug": "safe-conn"}],
            "composite_loops": {
                "add": [{"slug": "bad-loop", "residency": "non-eu"}],
            },
        }
        filtered, blocked = filter_diff_by_residency(diff, "eu")
        assert len(filtered["composite_loops"]["add"]) == 0
        assert any(b["type"] == "composite_loop" for b in blocked)

    def test_row_fleet_includes_non_eu_connector(self):
        diff = {"add": [{"slug": "zai", "residency_tag": "non-eu"}]}
        filtered, blocked = filter_diff_by_residency(diff, "row")
        assert len(filtered["add"]) == 1
        assert blocked == []

    def test_null_residency_fleet_unrestricted(self):
        diff = {"add": [{"slug": "anything", "residency_tag": "non-eu"}]}
        filtered, blocked = filter_diff_by_residency(diff, None)
        assert len(filtered["add"]) == 1
        assert blocked == []

    def test_unknown_residency_fail_closed(self):
        diff = {"add": [{"slug": "tagged", "residency_tag": "non-eu"}, {"slug": "untagged"}]}
        filtered, blocked = filter_diff_by_residency(diff, "unknown_value")
        assert len(filtered["add"]) == 1
        assert filtered["add"][0]["slug"] == "untagged"
        assert len(blocked) == 1
        assert blocked[0]["reason"] == "residency_blocked_unknown_fleet"
