"""evergreen_0206 Phase G — maintenance-gated conversion ladder."""
from __future__ import annotations

import app.services.conversion_gates as conversion_gates
from app.services.conversion_gates import gate_cookbook_create


def test_dead_ladder_predicates_stay_removed():
    """paywall_0925: the sync/cron/fleet predicates were never called and did not
    match config/tiers.yaml. Re-adding one is a pricing decision; this test
    makes that deliberate rather than a silent revival of dead code."""
    for name in ("gate_manual_sync", "gate_daemon_cron_install", "gate_fleet"):
        assert not hasattr(conversion_gates, name), name


class TestCookbookCreateGate:
    def test_free_under_limit_allowed(self):
        out = gate_cookbook_create("free", current_count=0, limit=1)
        assert out.allowed is True

    def test_free_at_limit_blocked(self):
        out = gate_cookbook_create("free", current_count=1, limit=1)
        assert out.allowed is False
        assert out.http_status == 403
        assert out.upgrade_to == "pro"

    def test_pro_at_limit_upgrades_to_pro_plus(self):
        out = gate_cookbook_create("pro", current_count=10, limit=10)
        assert out.allowed is False
        assert out.upgrade_to == "pro_plus"

    def test_unlimited_limit_none_always_allowed(self):
        out = gate_cookbook_create("pro_plus", current_count=999, limit=None)
        assert out.allowed is True
