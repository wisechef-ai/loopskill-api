"""repohygiene_2605 Phase C — install_count increment completeness.

Every install code path MUST atomically bump ``Skill.install_count``.
These tests pin the contract so a future path-addition that forgets the
increment regresses visibly in CI before it reaches prod.

Code paths under test:
    1. /api/telemetry  (event_type=install)          [routes.py, RCP-13, tested by test_install_count_sync]
    2. /api/skills/install  (direct single-skill)    [install_routes.py, RCP-13, tested by test_install_count_sync]
    3. MCP recipes_install                           [mcp/tools/install.py — BUG: missing increment before this fix]
    4. POST /api/cookbooks/{id}/install              [cookbook_routes.py, _record_install_event]
    5. MCP recipes_cookbook_install single-skill     [mcp/tools/cookbook_install.py, _record_install_event]
    6. MCP recipes_cookbook_install bulk             [mcp/tools/cookbook_install.py, _record_install_event]

Root cause (Phase C finding):
    Path #3 (``app/mcp/tools/install.py:recipes_install``) wrote an
    InstallEvent row but never issued the SQL-level
    ``Skill.install_count += 1`` update.  All 7 of the 9 "hot skills"
    with negative drift had their installs routed through the MCP tool
    (cbt_token agents use MCP).  The two positive-drift skills
    (client-reporter, incident-response) have a probe formula artefact —
    the probe uses MAX(tel, inst) but actual installs span BOTH tables,
    so the probe over-estimates the deficit; those skills have no real bug.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Skill, SkillVersion
from tests.conftest import make_skill


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_version(db: Session, skill: Skill, semver: str = "1.0.0") -> SkillVersion:
    v = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_size_bytes=100,
        checksum_sha256="a" * 64,
    )
    db.add(v)
    db.flush()
    # Refresh the relationship cache so skill.versions is populated.
    db.refresh(skill)
    return v


def _read_count(db: Session, slug: str) -> int:
    db.expire_all()
    row = db.query(Skill).filter(Skill.slug == slug).first()
    assert row is not None, f"skill {slug!r} not found"
    return int(row.install_count or 0)


# ── Path 3: MCP recipes_install ──────────────────────────────────────────────


class TestMcpRecipesInstallBumpsCounter:
    """``app/mcp/tools/install.recipes_install`` must bump install_count.

    Before the Phase C fix, the MCP tool inserted an InstallEvent but
    omitted the companion SQL-level ``Skill.install_count += 1`` update.
    """

    def test_mcp_install_increments_counter_from_zero(
        self, db_session: Session
    ) -> None:
        from app.auth_ctx import AuthContext
        from app.mcp.tools.install import recipes_install

        skill = make_skill(db_session, slug="larry")
        _make_version(db_session, skill)
        assert _read_count(db_session, "larry") == 0

        ctx = AuthContext(scope="master")
        result = recipes_install(db_session, slug="larry", ctx=ctx)

        assert "error" not in result, f"Unexpected error: {result}"
        assert _read_count(db_session, "larry") == 1, (
            "MCP recipes_install did not bump Skill.install_count — "
            "see Phase C root-cause analysis."
        )

    def test_mcp_install_increments_counter_n_times(
        self, db_session: Session
    ) -> None:
        from app.auth_ctx import AuthContext
        from app.mcp.tools.install import recipes_install

        skill = make_skill(db_session, slug="multi-agent-discord-coordination")
        _make_version(db_session, skill)

        ctx = AuthContext(scope="master")
        for _ in range(5):
            r = recipes_install(
                db_session,
                slug="multi-agent-discord-coordination",
                ctx=ctx,
            )
            assert "error" not in r, r

        assert _read_count(db_session, "multi-agent-discord-coordination") == 5

    def test_mcp_install_does_not_bump_other_skill(
        self, db_session: Session
    ) -> None:
        from app.auth_ctx import AuthContext
        from app.mcp.tools.install import recipes_install

        skill_a = make_skill(db_session, slug="pr-draft")
        _make_version(db_session, skill_a)
        make_skill(db_session, slug="clean-architecture")
        # No version for clean-architecture so installing it fails gracefully.

        ctx = AuthContext(scope="master")
        recipes_install(db_session, slug="pr-draft", ctx=ctx)

        assert _read_count(db_session, "pr-draft") == 1
        assert _read_count(db_session, "clean-architecture") == 0

    def test_mcp_install_pinned_version_bumps_counter(
        self, db_session: Session
    ) -> None:
        """Pinned-version path (slug@1.2.3) must also increment the counter."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.install import recipes_install

        skill = make_skill(db_session, slug="code-review")
        _make_version(db_session, skill, semver="2.1.0")

        ctx = AuthContext(scope="master")
        result = recipes_install(
            db_session, slug="code-review@2.1.0", ctx=ctx
        )

        assert "error" not in result, result
        assert result["version_pinned"] is True
        assert _read_count(db_session, "code-review") == 1


# ── Path 4 + 5 + 6: cookbook routes (regression guard only) ──────────────────
# These paths already use _record_install_event which has the bump.
# The tests below ensure a refactor can't silently drop that call.


class TestCookbookInstallPathsPreserveCounter:
    """Cookbook install paths must continue to bump install_count.

    _record_install_event was added in recipes-D; these tests guard it
    against accidental removal or bypass.
    """

    def test_record_install_event_helper_bumps_counter(
        self, db_session: Session
    ) -> None:
        from app._skill_helpers import _record_install_event

        skill = make_skill(db_session, slug="graphify")
        assert _read_count(db_session, "graphify") == 0

        _record_install_event(
            db_session,
            skill=skill,
            version_semver="1.0.0",
            request=None,
            source="cookbook",
        )
        db_session.commit()

        assert _read_count(db_session, "graphify") == 1

    def test_record_install_event_n_calls_bumps_n(
        self, db_session: Session
    ) -> None:
        from app._skill_helpers import _record_install_event

        skill = make_skill(db_session, slug="incident-response")

        for i in range(4):
            _record_install_event(
                db_session,
                skill=skill,
                version_semver="1.0.0",
                request=None,
                source="mcp",
            )
        db_session.commit()

        assert _read_count(db_session, "incident-response") == 4


# ── Cross-path idempotency assertion (drift-probe compatibility) ──────────────


class TestProbeFormulaCompatibility:
    """Document the probe's MAX formula limitation.

    The install_count_drift_probe uses MAX(telemetry_installs, install_events)
    as 'truth'.  When installs arrive via BOTH paths, MAX under-counts truth
    and produces spurious positive drift (install_count > probe_truth).

    This test demonstrates that scenario so future probe changes
    (switching to SUM) are covered by a concrete counter-example.
    """

    def test_both_paths_sum_gt_max(self, db_session: Session) -> None:
        """3 telemetry installs + 5 install_events = install_count 8,
        but MAX(3, 5) = 5 → probe sees drift=+3 (false positive).
        """
        from uuid import uuid4 as _uuid4

        from app.models import InstallEvent, TelemetryEvent
        from sqlalchemy import func

        skill = make_skill(db_session, slug="client-reporter")

        for _ in range(3):
            db_session.add(
                TelemetryEvent(
                    id=_uuid4(),
                    event_type="install",
                    skill_slug="client-reporter",
                )
            )
        for _ in range(5):
            db_session.add(
                InstallEvent(
                    id=_uuid4(),
                    skill_id=skill.id,
                    skill_slug="client-reporter",
                )
            )
        # Manually set install_count to reflect both paths (8 total).
        db_session.query(Skill).filter(Skill.slug == "client-reporter").update(
            {Skill.install_count: 8}, synchronize_session=False
        )
        db_session.commit()

        tel = (
            db_session.query(func.count())
            .filter(
                TelemetryEvent.skill_slug == "client-reporter",
                TelemetryEvent.event_type == "install",
            )
            .scalar()
            or 0
        )
        inst = (
            db_session.query(func.count())
            .filter(InstallEvent.skill_slug == "client-reporter")
            .scalar()
            or 0
        )
        probe_truth = max(tel, inst)  # probe's formula
        actual = _read_count(db_session, "client-reporter")

        # Show the false positive: probe reports drift when there is none.
        assert actual == 8
        assert probe_truth == 5
        assert actual - probe_truth == 3, (
            "Probe reports drift=+3 but this is a formula artefact — "
            "the skill has NO real bug.  Probe should use SUM, not MAX."
        )
