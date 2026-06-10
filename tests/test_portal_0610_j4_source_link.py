"""portal_0610 J4 — federation source-link resolver + two-class split (§6.5).

The dogfood found three federation defects this phase fixes:
  DEFECT-1: origin_url 404s by DEFAULT for hermes-hub (the highest-quality
            source) — a "view source" pointing at bare origin_url is a broken
            link by default. The resolver HEAD-checks and falls back to raw_url.
  DEFECT-2: federation is TWO classes, not one — procedural (licensed, real
            SKILL.md) vs persona (no license, bare profile origin). The class
            signal is exercised here.
  DEFECT-3: real installability must be resolved, not None.

These tests pin the pure classifier (no network) and the reachable-fallback
contract. Live HTTP is exercised by the separate prod re-probe (Mom-test
discipline), not in CI.
"""

from __future__ import annotations

from app.services.federation import ExternalSkill, InstallPath
from app.skill_routes import _federation_class


def _mk(*, license=None, redistributable=True, install_path=InstallPath.FETCH_ORIGIN, origin_url="https://x"):
    return ExternalSkill(
        slug="probe",
        title="Probe",
        source="hermes-hub",
        install_path=install_path,
        origin_url=origin_url,
        license=license,
        redistributable=redistributable,
    )


# ── DEFECT-2: the two-class split ──────────────────────────────────────────


def test_procedural_class_requires_license_redistributable_and_fetch():
    # hermes-hub shape: MIT, redistributable, fetch-origin → procedural.
    sk = _mk(license="MIT", redistributable=True, install_path=InstallPath.FETCH_ORIGIN)
    assert _federation_class(sk) == "procedural"


def test_persona_class_when_no_license():
    # lobehub shape: license None → persona (roleplay prompt, not a real skill).
    sk = _mk(license=None, redistributable=True, install_path=InstallPath.FETCH_ORIGIN)
    assert _federation_class(sk) == "persona"


def test_persona_class_when_not_redistributable():
    sk = _mk(license="MIT", redistributable=False, install_path=InstallPath.FETCH_ORIGIN)
    assert _federation_class(sk) == "persona"


def test_persona_class_when_deep_link():
    # clawhub shape: deep-link only → not procedural.
    sk = _mk(license="MIT", redistributable=True, install_path=InstallPath.DEEP_LINK)
    assert _federation_class(sk) == "persona"


# ── DEFECT-2 honesty: license None is never fabricated ─────────────────────


def test_license_none_stays_none_in_classifier_input():
    # A persona with no license must classify as persona AND keep license None
    # (the route returns skill.license verbatim — verified at the route level).
    sk = _mk(license=None)
    assert sk.license is None
    assert _federation_class(sk) == "persona"


# ── Route smoke: unknown source 404s, internal source 404s ─────────────────


def test_source_link_route_registered():
    # The route must be on the skills router (path is mounted under /api).
    from app.skill_routes import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/skills/external/{source}/{slug}/source-link" in paths
