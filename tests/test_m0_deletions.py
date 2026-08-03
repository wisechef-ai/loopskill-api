"""autopilot_0308 Phase M0 — deletion regression tests.

RED-proof for three decided deletions (hub D-007, D-019, D-018 #6):

  1. The carousel surface (D-007) — 36+ days of 404s on a client-facing
     front door. Route, MCP tool, and crons must be gone; the CarouselEntry
     DB table is explicitly NOT dropped (data-loss migrations are out of
     scope — see SHARED_CONTEXT §1).
  2. cost_per_accepted_change (D-019) — HIDE, not label. The client-reported,
     unverified metric must not appear in the fleet dashboard response, but
     the retained query helper (app.services.sync_report) and the rollup
     table must be untouched.

These tests are written to FAIL against pre-M0 code (RED) and PASS once the
deletions land (GREEN).
"""

from __future__ import annotations

import hashlib

import pytest


# ── Deletion 1 — the carousel surface (D-007) ──────────────────────────────


class TestCarouselRemoved:
    def test_carousel_package_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import app.carousel.routes  # noqa: F401

    def test_carousel_selector_cron_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import app.crons.carousel_selector  # noqa: F401

    def test_carousel_verdict_cron_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import app.crons.carousel_verdict  # noqa: F401

    def test_carousel_mcp_tool_module_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import app.mcp.tools.carousel_today  # noqa: F401

    def test_backfill_carousel_taglines_script_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import app.scripts.backfill_carousel_taglines  # noqa: F401

    def test_carousel_tool_not_registered_in_mcp_registry(self):
        from app.mcp.registry import _tool_definitions

        names = {t.name for t in _tool_definitions()}
        assert "loopskill_carousel_today" not in names

    def test_carousel_route_not_mounted(self, client):
        # Prior behavior: 404 {"detail": "No carousel entries for today"} —
        # a route that exists but has no data. Post-deletion: 404 because
        # FastAPI has no matching route at all (detail says so).
        resp = client.get("/api/carousel/today")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Not Found"

    def test_carousel_entry_table_still_defined(self):
        # DO NOT DROP: the DB model/table survives even though the surface
        # that reads it is gone (SHARED_CONTEXT §1 — no data-loss migrations
        # in this phase).
        from app.models import CarouselEntry

        assert CarouselEntry.__tablename__ == "carousel_entries"


# ── Deletion 2 — cost_per_accepted_change (D-019) ──────────────────────────


class TestCostPerAcceptedHidden:
    def _setup_fleet(self, db):
        from app.api_key_routes import _generate_key
        from app.models import APIKey, Fleet, User

        u = User(email="m0-dash@t.com", display_name="M0", subscription_tier="pro")
        db.add(u)
        db.flush()
        pt, pfx, hs = _generate_key()
        k = APIKey(user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True)
        db.add(k)
        db.flush()
        f = Fleet(
            owner_user_id=u.id,
            name="m0-dash-fleet",
            fleet_api_key_hash=hashlib.sha256(b"m0-dash").hexdigest(),
        )
        db.add(f)
        db.flush()
        db.commit()
        return pt, f

    def test_dashboard_response_omits_cost_per_accepted_change(self, db_session, monkeypatch):
        from tests._app_factory import build_test_app
        from fastapi.testclient import TestClient

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        client = TestClient(app)
        pt, f = self._setup_fleet(db_session)
        resp = client.get(f"/api/fleets/{f.id}/dashboard", headers={"x-api-key": pt})
        assert resp.status_code == 200
        assert "cost_per_accepted_change" not in resp.json()

    def test_sync_report_helper_and_rollup_table_untouched(self):
        # The retained query helper (option-B future corroboration) must
        # still exist under its original name.
        from app.services.sync_report import cost_per_accepted_change  # noqa: F401
        from app.models import LoopRunDailyRollup

        assert LoopRunDailyRollup.__tablename__ == "loop_run_daily_rollups"
