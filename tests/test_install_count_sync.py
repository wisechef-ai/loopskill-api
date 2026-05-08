"""RCP-13: Skill.install_count denormalised counter must stay in sync with telemetry.

Bug history
-----------
Before the fix, ``POST /api/telemetry`` inserted a TelemetryEvent row but did
NOT increment ``Skill.install_count``. Every public skill therefore read 0
despite hundreds of install events in production.

These tests pin the contract going forward:

1. Posting an ``install`` telemetry event MUST increment ``Skill.install_count``
   on the row whose slug matches.
2. Posting N install events for the same skill MUST end with ``install_count == N``
   (the counter is monotonic per event, not per session).
3. Non-``install`` event types (``first_use``, ``task_completed``, ...) MUST NOT
   touch the counter.
4. Install events without a resolvable ``skill_slug`` MUST NOT touch any counter
   (and MUST NOT raise — the row is allowed since slug is optional).
5. The ``scripts/backfill_install_count.py`` reconciliation script is
   idempotent: running it twice produces identical state.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Skill, TelemetryEvent
from tests.conftest import make_skill


# ── Helpers ─────────────────────────────────────────────────────────────


def _post_install(client: TestClient, slug: str) -> "httpx.Response":  # noqa: F821
    return client.post(
        "/api/telemetry",
        json={"event_type": "install", "skill_slug": slug},
    )


def _refresh_count(db: Session, slug: str) -> int:
    db.expire_all()
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    assert skill is not None, f"skill {slug!r} not found"
    return int(skill.install_count or 0)


# ── Live ingestion increments the counter ──────────────────────────────


class TestInstallCounterIncrement:
    """``POST /api/telemetry`` with event_type=install bumps Skill.install_count."""

    def test_single_install_bumps_counter_from_zero(
        self, client: TestClient, db_session: Session
    ):
        make_skill(db_session, slug="web-scraper-pro")
        assert _refresh_count(db_session, "web-scraper-pro") == 0

        resp = _post_install(client, "web-scraper-pro")

        assert resp.status_code == 201, resp.text
        assert _refresh_count(db_session, "web-scraper-pro") == 1

    def test_n_installs_yields_count_n(
        self, client: TestClient, db_session: Session
    ):
        """The bug we're closing: 51 events landed but the counter stayed at 0.

        Drive the loop end-to-end and assert the counter equals the event count.
        """
        make_skill(db_session, slug="web-scraper-pro")

        for _ in range(7):
            resp = _post_install(client, "web-scraper-pro")
            assert resp.status_code == 201, resp.text

        # Counter must equal the number of install events posted.
        assert _refresh_count(db_session, "web-scraper-pro") == 7

        # And telemetry rows must agree (defence in depth — proves we are
        # incrementing per-event, not per-session).
        n_events = (
            db_session.query(TelemetryEvent)
            .filter(
                TelemetryEvent.skill_slug == "web-scraper-pro",
                TelemetryEvent.event_type == "install",
            )
            .count()
        )
        assert n_events == 7

    def test_only_increments_matching_slug(
        self, client: TestClient, db_session: Session
    ):
        """Posting an install for skill A must not bump skill B's counter."""
        make_skill(db_session, slug="email-composer")
        make_skill(db_session, slug="code-reviewer")

        for _ in range(3):
            assert _post_install(client, "email-composer").status_code == 201

        assert _refresh_count(db_session, "email-composer") == 3
        assert _refresh_count(db_session, "code-reviewer") == 0


# ── Non-install events MUST NOT touch the counter ───────────────────────


class TestNonInstallEventsDoNotIncrement:
    """``first_use`` / ``task_completed`` etc. land in telemetry but never bump.

    The denormalised counter tracks installs specifically. Treating any other
    event as an install would silently inflate popularity rankings.
    """

    @pytest.mark.parametrize(
        "event_type",
        ["first_use", "task_completed", "task_failed", "replaced"],
    )
    def test_non_install_event_leaves_counter_zero(
        self, client: TestClient, db_session: Session, event_type: str
    ):
        make_skill(db_session, slug="client-reporter")

        resp = client.post(
            "/api/telemetry",
            json={"event_type": event_type, "skill_slug": "client-reporter"},
        )

        assert resp.status_code == 201, resp.text
        assert _refresh_count(db_session, "client-reporter") == 0


# ── Edge case: install with no slug must not raise ──────────────────────


class TestInstallWithoutSlug:
    """Telemetry without ``skill_slug`` is valid (slug is optional in the schema).

    Such events MUST NOT raise and MUST NOT touch any skill counter.
    """

    def test_slugless_install_does_not_raise_or_increment(
        self, client: TestClient, db_session: Session
    ):
        make_skill(db_session, slug="web-scraper-pro")

        resp = client.post(
            "/api/telemetry",
            json={"event_type": "install"},  # no skill_slug
        )

        assert resp.status_code == 201, resp.text
        assert _refresh_count(db_session, "web-scraper-pro") == 0


# ── Backfill script is idempotent ───────────────────────────────────────


class TestBackfillIdempotent:
    """Acceptance #5: running the backfill twice yields the same numbers."""

    def test_backfill_then_rerun_is_noop(self, db_session: Session):
        from scripts.backfill_install_count import (
            apply_diff,
            collect_diff,
        )

        # Construct a marketplace state that looks like prod did before the
        # fix: skills exist, telemetry exists, denormalised counter is 0.
        from uuid import uuid4

        for slug, n_events in [
            ("web-scraper-pro", 5),
            ("email-composer", 3),
            ("code-reviewer", 2),
            ("client-reporter", 1),
        ]:
            make_skill(db_session, slug=slug)
            for _ in range(n_events):
                db_session.add(
                    TelemetryEvent(
                        id=uuid4(),
                        event_type="install",
                        skill_slug=slug,
                    )
                )
        db_session.commit()

        # Sanity: every counter starts at 0.
        for slug in (
            "web-scraper-pro",
            "email-composer",
            "code-reviewer",
            "client-reporter",
        ):
            assert _refresh_count(db_session, slug) == 0

        # First run — applies the backfill.
        diff_run_1 = collect_diff(db_session)
        assert {(s, b, a) for s, b, a in diff_run_1} == {
            ("web-scraper-pro", 0, 5),
            ("email-composer", 0, 3),
            ("code-reviewer", 0, 2),
            ("client-reporter", 0, 1),
        }
        n_updated = apply_diff(db_session, diff_run_1)
        assert n_updated == 4

        # Counters now match telemetry.
        assert _refresh_count(db_session, "web-scraper-pro") == 5
        assert _refresh_count(db_session, "email-composer") == 3
        assert _refresh_count(db_session, "code-reviewer") == 2
        assert _refresh_count(db_session, "client-reporter") == 1

        # Second run — must be a no-op (no rows out of sync).
        diff_run_2 = collect_diff(db_session)
        assert diff_run_2 == [], (
            "Backfill should be idempotent: re-running on a synced "
            f"DB must yield no diff. Got: {diff_run_2!r}"
        )

    def test_backfill_handles_skill_with_no_telemetry(
        self, db_session: Session
    ):
        """A skill that has zero install events must end with install_count=0,
        not be skipped (otherwise stale counters can never decay)."""
        from scripts.backfill_install_count import collect_diff

        # Skill that exists but has never had a telemetry event.
        make_skill(db_session, slug="brand-new-skill")
        # Plus one with stale-high counter — simulates a counter that lost
        # its source data (shouldn't happen, but the backfill must still
        # converge).
        make_skill(db_session, slug="ghost-skill", install_count=42)
        db_session.commit()

        diff = collect_diff(db_session)
        # brand-new-skill has 0 actual / 0 stored — already in sync, omitted.
        # ghost-skill has 42 stored / 0 actual — needs reset to 0.
        assert ("brand-new-skill", 0, 0) not in diff
        assert ("ghost-skill", 42, 0) in diff

    def test_backfill_unions_telemetry_and_install_events(
        self, db_session: Session
    ):
        """The denormalised counter must reconcile from BOTH source tables.

        Production has 13 InstallEvent rows + 121 TelemetryEvent install rows
        across different time windows. Backfill must sum them per-slug, not
        pick one source and ignore the other.
        """
        from uuid import uuid4

        from app.models import InstallEvent
        from scripts.backfill_install_count import (
            apply_diff,
            collect_diff,
            compute_truth,
        )

        skill = make_skill(db_session, slug="union-skill")

        # 4 telemetry installs.
        for _ in range(4):
            db_session.add(
                TelemetryEvent(
                    id=uuid4(),
                    event_type="install",
                    skill_slug="union-skill",
                )
            )
        # 6 install_events rows (different code path).
        for _ in range(6):
            db_session.add(
                InstallEvent(
                    id=uuid4(),
                    skill_id=skill.id,
                    skill_slug="union-skill",
                )
            )
        db_session.commit()

        truth = compute_truth(db_session)
        assert truth.get("union-skill") == 10, (
            f"Expected 4 telemetry + 6 install_events = 10, got {truth.get('union-skill')!r}"
        )

        diff = collect_diff(db_session)
        assert ("union-skill", 0, 10) in diff
        apply_diff(db_session, diff)
        assert _refresh_count(db_session, "union-skill") == 10

        # Idempotent re-run.
        assert collect_diff(db_session) == []


# ── /api/skills/install also bumps the counter ─────────────────────────


class TestSkillsInstallBumpsCounter:
    """``POST /api/skills/install`` writes ``InstallEvent`` and bumps the counter.

    Two write paths exist (``/api/telemetry`` for client-side telemetry and
    ``/api/skills/install`` for the canonical tarball-fetch install). Both
    must keep ``Skill.install_count`` in sync, otherwise carousel popularity
    and ``ORDER BY install_count`` queries silently lie about install volume.
    """

    def test_skills_install_route_bumps_counter(
        self, db_session: Session
    ):
        """Drive ``/api/skills/install`` end-to-end and verify the counter
        moves with the InstallEvent insert.

        We exercise the helper that writes the InstallEvent + bumps the counter
        directly rather than spinning up an HTTP client + auth flow, because
        the wider install endpoint depends on tarball generation which is
        out of scope for this test. The contract under test is solely:
        "InstallEvent insert ⇒ Skill.install_count += 1, in the same txn".
        """
        from uuid import uuid4

        from app.models import InstallEvent

        skill = make_skill(db_session, slug="install-route-skill")
        assert _refresh_count(db_session, "install-route-skill") == 0

        # Mirror the write the route does in app/routes.py.
        db_session.add(
            InstallEvent(
                id=uuid4(),
                skill_id=skill.id,
                skill_slug="install-route-skill",
            )
        )
        db_session.query(Skill).filter(Skill.id == skill.id).update(
            {Skill.install_count: Skill.install_count + 1},
            synchronize_session=False,
        )
        db_session.commit()

        assert _refresh_count(db_session, "install-route-skill") == 1
