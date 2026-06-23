"""evergreen_0206 Phase G — maintenance-gated conversion ladder."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.conversion_gates import (
    gate_cookbook_create,
    gate_daemon_cron_install,
    gate_fleet,
    gate_manual_sync,
)


class TestManualSyncGate:
    def test_free_first_sync_allowed(self):
        out = gate_manual_sync("free", free_sync_used_at=None)
        assert out.allowed is True
        assert out.http_status == 200

    def test_free_second_sync_402(self):
        out = gate_manual_sync("free", free_sync_used_at=datetime.now(timezone.utc))
        assert out.allowed is False
        assert out.http_status == 402
        assert out.upgrade_to == "pro"

    def test_pro_always_allowed(self):
        out = gate_manual_sync("pro", free_sync_used_at=datetime.now(timezone.utc))
        assert out.allowed is True

    def test_none_tier_first_sync_allowed(self):
        out = gate_manual_sync(None, free_sync_used_at=None)
        assert out.allowed is True


class TestDaemonCronGate:
    def test_free_cron_install_blocked_402(self):
        out = gate_daemon_cron_install("free")
        assert out.allowed is False
        assert out.http_status == 402
        assert out.upgrade_to == "pro"

    def test_pro_cron_install_allowed(self):
        assert gate_daemon_cron_install("pro").allowed is True

    def test_pro_plus_cron_install_allowed(self):
        assert gate_daemon_cron_install("pro_plus").allowed is True


class TestFleetGate:
    def test_free_fleet_403(self):
        out = gate_fleet("free")
        assert out.allowed is False
        assert out.http_status == 403
        assert out.upgrade_to == "pro_plus"

    def test_pro_fleet_403(self):
        """Fleet is Pro+ ONLY — Pro is also blocked."""
        out = gate_fleet("pro")
        assert out.allowed is False
        assert out.http_status == 403

    def test_pro_plus_fleet_allowed(self):
        assert gate_fleet("pro_plus").allowed is True

    def test_legacy_operator_fleet_allowed(self):
        """Legacy 'operator' slug resolves to pro_plus."""
        assert gate_fleet("operator").allowed is True


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
        out = gate_cookbook_create("pro", current_count=50, limit=50)
        assert out.allowed is False
        assert out.upgrade_to == "pro_plus"

    def test_unlimited_limit_none_always_allowed(self):
        out = gate_cookbook_create("pro_plus", current_count=999, limit=None)
        assert out.allowed is True
