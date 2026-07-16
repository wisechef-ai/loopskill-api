"""tests/test_fleetos_C_golden_bundle.py — fleetos_1607 Phase C gate suite.

RED-proofs golden-bundle composition + the bootstrap planner:
  * compose gathers all artifact types (loops, personalities, aggregated
    secret_refs) into one desired-state manifest.
  * plan_bootstrap validates the host_profile FIRST — an incompatible host
    yields ok=False with the NAMED unmet requirement (a restore never silently
    proceeds).
  * missing required secrets block the bootstrap with the named secret.
  * triage classifies loops portable vs host-bound (the coverage audit).
"""

from __future__ import annotations

from uuid import uuid4

from app.models import Bundle, LoopManifest, Personality, User
from app.services import golden_bundle as gb


def _mk_owner(db):
    u = User(id=uuid4(), display_name="o")
    db.add(u)
    db.flush()
    return u


def _mk_bundle(db, owner):
    b = Bundle(id=uuid4(), name="tori-golden", bundle_owner=owner.id)
    db.add(b)
    db.flush()
    return b


def _mk_loop(db, owner, loop_id, requires=None, secret_refs=None):
    m = LoopManifest(
        id=uuid4(),
        loop_id=loop_id,
        owner_user_id=owner.id,
        schedule="0 9 * * *",
        prompt="do",
        skills=[],
        requires=requires or {},
        secret_refs=secret_refs or [],
        reserved={},
    )
    db.add(m)
    db.flush()
    return m


def _mk_personality(db, slug):
    p = Personality(id=uuid4(), slug=slug, title="Tori", is_public=True, system_prompt="You are Tori.")
    db.add(p)
    db.flush()
    return p


# ── composition ──────────────────────────────────────────────────────────────


def test_compose_gathers_all_artifacts(db_session):
    owner = _mk_owner(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_loop(db_session, owner, "loop-a", secret_refs=[{"name": "OPENAI_API_KEY", "required": True}])
    _mk_loop(
        db_session,
        owner,
        "loop-b",
        secret_refs=[{"name": "OPENAI_API_KEY", "required": True}, {"name": "COGNEE_URL", "required": True}],
    )
    _mk_personality(db_session, "tori-soul")
    db_session.commit()

    golden = gb.compose_golden_bundle(db_session, bundle, host_profile_name="adam-xps")
    assert golden.coverage["loops"] == 2
    assert "tori-soul" in golden.personalities
    # secret_refs aggregated + deduped across loops
    names = {r["name"] for r in golden.secret_refs}
    assert names == {"OPENAI_API_KEY", "COGNEE_URL"}
    assert golden.host_profile_name == "adam-xps"


# ── bootstrap planner (host-first) ───────────────────────────────────────────


def test_bootstrap_ok_on_compatible_host(db_session):
    owner = _mk_owner(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_loop(db_session, owner, "loop-a", requires={"os": ["linux"], "runtime": {"python": ">=3.11"}})
    db_session.commit()
    golden = gb.compose_golden_bundle(db_session, bundle)
    plan = gb.plan_bootstrap(
        db_session,
        golden,
        host_profile={"os": {"os": "linux"}, "runtimes": {"python": "3.12.0"}, "packages": []},
        available_secret_names=[],
    )
    assert plan.ok is True
    assert plan.unmet_requirements == []
    # host_profile is the FIRST step
    assert plan.steps[0].kind == "host_profile"


def test_bootstrap_fails_named_on_incompatible_host(db_session):
    owner = _mk_owner(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_loop(db_session, owner, "loop-a", requires={"runtime": {"python": ">=3.13"}})
    db_session.commit()
    golden = gb.compose_golden_bundle(db_session, bundle)
    plan = gb.plan_bootstrap(
        db_session,
        golden,
        host_profile={"os": {"os": "linux"}, "runtimes": {"python": "3.11.9"}, "packages": []},
    )
    assert plan.ok is False
    assert any("python" in u for u in plan.unmet_requirements)
    assert plan.steps[0].blocking is True  # host_profile step blocks


def test_bootstrap_fails_on_missing_secret(db_session):
    owner = _mk_owner(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_loop(db_session, owner, "loop-a", secret_refs=[{"name": "MISSING_KEY", "required": True}])
    db_session.commit()
    golden = gb.compose_golden_bundle(db_session, bundle)
    plan = gb.plan_bootstrap(
        db_session,
        golden,
        host_profile={"os": {"os": "linux"}, "runtimes": {}, "packages": []},
        available_secret_names=[],  # MISSING_KEY not available
    )
    assert plan.ok is False
    assert "MISSING_KEY" in plan.missing_secrets


# ── triage (the coverage audit) ──────────────────────────────────────────────


def test_triage_portable_vs_host_bound(db_session):
    owner = _mk_owner(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_loop(db_session, owner, "portable-loop", requires={})  # no requires -> portable
    _mk_loop(db_session, owner, "bound-loop", requires={"packages": ["cuda"]})  # needs cuda
    db_session.commit()
    golden = gb.compose_golden_bundle(db_session, bundle)
    triage = gb.triage_loops_for_bundle(
        golden, host_profile={"os": {"os": "linux"}, "runtimes": {}, "packages": ["git"]}
    )
    assert triage["portable"] == ["portable-loop"]
    assert triage["host_bound"] == ["bound-loop"]
