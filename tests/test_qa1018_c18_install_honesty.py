"""qa1018 C18 — the public bundle index + install.sh must be honest about
members whose SKILL.md is not a real skill.

Prod evidence (persona QA, 2026-10-18): ``seo-and-search-growth`` listed 8
members in its well-known index with no ``locked`` flag; ``install.sh``
downloaded all 8 and printed "installed 8 skill(s)", but three SKILL.md files
were the ~750-byte locked stub (live origin fetch failed) and one was an
upstream "<name> has moved" signpost. Nothing was verified by checksum.

Pinned here:
  * index entries carry ``installable`` (+ ``skip_reason``) and a ``sha256``
    only where it hashes exactly the served bytes; legacy keys unchanged;
  * the SKILL.md route labels what it served via response headers and
    records a federated member's failed / moved outcome so the index can show
    it; a later good fetch clears it;
  * the REAL install.sh, run with bash against a live server, installs only
    real skills, verifies sha256, never leaves empty dirs, prints an honest
    installed/skipped summary, and exits non-zero only when it was asked to
    install something and installed nothing.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bundle_install_script_routes import render_install_script
from app.database import get_db
from app.models import Base, Bundle, BundleSkill, Skill

MOVED_BODY = (
    "# `on-page-seo-auditor` has moved\n\n"
    "> [!IMPORTANT]\n"
    "> `seo-geo-claude-skills` is now a **signpost** repo — its skills are no longer developed here.\n\n"
    "Install the active skill from the bundle:\n"
)
REAL_EXT_BODY = "---\nname: real-ext\ndescription: a real federated skill\n---\n\n# Real ext\n\nbody\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── fixtures ────────────────────────────────────────────────────────────────


def _engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _r):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    return engine


def _local(db: Session, slug: str, *, tier: str = "free", readme: str | None = None) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=slug,
        tier=tier,
        is_public=True,
        readme=f"# {slug}\n\nreal body ✓\n" if readme is None else readme,
    )
    db.add(s)
    db.flush()
    return s


def _external(db: Session, slug: str) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=f"ext:hermes-hub:{slug}",
        title=slug,
        description="federated",
        tier="external",
        is_public=False,
        skill_variant="external",
        external_resources={
            "federation_source": "hermes-hub",
            "external_slug": slug,
            "install_path": "fetch_origin",
            "origin_url": f"https://github.com/example/{slug}",
            "redistributable": True,
            "scan_status": "clean",
        },
    )
    db.add(s)
    db.flush()
    return s


def _bundle(db: Session, slug: str, members: list[Skill]) -> Bundle:
    cb = Bundle(id=uuid4(), name=slug, slug=slug, visibility="public")
    db.add(cb)
    db.flush()
    for i, m in enumerate(members):
        db.add(BundleSkill(bundle_id=cb.id, skill_id=m.id, source="custom-added", install_order=i))
    db.commit()
    return cb


# Origin behaviour per external slug, swapped in for resolve_external_install.
ORIGIN: dict[str, str | None] = {}


@pytest.fixture()
def fake_origin(monkeypatch):
    from app.services import bundle_external as ce

    ORIGIN.clear()

    def _resolve(source, slug):
        body = ORIGIN.get(slug)
        return None if body is None else {"content": body, "raw_url": f"https://raw.example/{slug}"}

    monkeypatch.setattr(ce, "resolve_external_install", _resolve)
    return ORIGIN


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = _engine()
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _wk_app(db: Session) -> FastAPI:
    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    app.include_router(wk_router)
    return app


def _index(c: TestClient, slug: str) -> dict:
    r = c.get(f"/api/bundles/public/{slug}/.well-known/skills/index.json")
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["skills"]}


def _md(c: TestClient, slug: str, name: str):
    r = c.get(f"/api/bundles/public/{slug}/.well-known/skills/{name}/SKILL.md")
    assert r.status_code == 200, r.text
    return r


# ─── index + SKILL.md contract ───────────────────────────────────────────────


class TestIndexFlags:
    def test_free_local_member_is_installable_with_sha_of_served_bytes(self, db_session):
        free = _local(db_session, "free-one")
        cb = _bundle(db_session, "b-free", [free])
        with TestClient(_wk_app(db_session)) as c:
            entry = _index(c, cb.slug)["free-one"]
            served = _md(c, cb.slug, "free-one")
        assert entry["installable"] is True
        assert "skip_reason" not in entry and "locked" not in entry
        assert entry["sha256"] == hashlib.sha256(served.content).hexdigest()
        assert served.headers["x-loopskill-installable"] == "true"
        assert served.headers["x-loopskill-sha256"] == entry["sha256"]

    def test_paid_member_keeps_legacy_locked_keys_and_adds_skip_reason(self, db_session):
        paid = _local(db_session, "paid-one", tier="pro")
        cb = _bundle(db_session, "b-paid", [paid])
        with TestClient(_wk_app(db_session)) as c:
            entry = _index(c, cb.slug)["paid-one"]
            served = _md(c, cb.slug, "paid-one")
        assert entry["locked"] is True and entry["tier"] == "pro"
        assert entry["installable"] is False and entry["skip_reason"] == "locked"
        assert "sha256" not in entry
        assert served.headers["x-loopskill-installable"] == "false"
        assert served.headers["x-loopskill-skip-reason"] == "locked"

    def test_free_member_with_empty_body_is_not_installable(self, db_session):
        empty = _local(db_session, "empty-one", readme="")
        cb = _bundle(db_session, "b-empty", [empty])
        with TestClient(_wk_app(db_session)) as c:
            entry = _index(c, cb.slug)["empty-one"]
            served = _md(c, cb.slug, "empty-one")
        assert entry["installable"] is False and entry["skip_reason"] == "unavailable"
        assert served.headers["x-loopskill-skip-reason"] == "unavailable"

    def test_external_origin_failure_is_recorded_and_flagged_then_heals(self, db_session, fake_origin):
        ext = _external(db_session, "stale-one")
        cb = _bundle(db_session, "b-ext-fail", [ext])
        with TestClient(_wk_app(db_session)) as c:
            assert _index(c, cb.slug)[ext.slug]["installable"] is True  # never served yet: hint only
            served = _md(c, cb.slug, ext.slug)  # origin returns nothing
            assert served.headers["x-loopskill-installable"] == "false"
            assert served.headers["x-loopskill-skip-reason"] == "unavailable"
            # the stub must not pretend an API key would help — origin is down
            assert "authenticated LoopSkill key" not in served.text
            entry = _index(c, cb.slug)[ext.slug]
            assert entry["installable"] is False and entry["skip_reason"] == "unavailable"
            assert "locked" not in entry, "legacy locked flag stays tier-only"

            fake_origin["stale-one"] = REAL_EXT_BODY  # origin recovers
            healed = _md(c, cb.slug, ext.slug)
            assert healed.text == REAL_EXT_BODY
            assert healed.headers["x-loopskill-installable"] == "true"
            assert healed.headers["x-loopskill-sha256"] == _sha(REAL_EXT_BODY)
            assert _index(c, cb.slug)[ext.slug]["installable"] is True

    def test_external_moved_signpost_is_flagged_moved(self, db_session, fake_origin):
        ext = _external(db_session, "moved-one")
        fake_origin["moved-one"] = MOVED_BODY
        cb = _bundle(db_session, "b-ext-moved", [ext])
        with TestClient(_wk_app(db_session)) as c:
            served = _md(c, cb.slug, ext.slug)
            assert served.headers["x-loopskill-skip-reason"] == "moved"
            entry = _index(c, cb.slug)[ext.slug]
        assert entry["installable"] is False and entry["skip_reason"] == "moved"
        assert "sha256" not in entry, "a live-fetched body must not carry an index sha"


class TestMovedDetector:
    def test_detects_the_prod_signpost(self):
        from app.services.bundle_servability import is_moved_signpost

        assert is_moved_signpost(MOVED_BODY)

    def test_real_skill_mentioning_moved_is_not_a_signpost(self):
        from app.services.bundle_servability import is_moved_signpost

        assert not is_moved_signpost("---\nname: x\n---\n# The repo has moved\n")
        assert not is_moved_signpost("# Migration guide\n\nWhen a page has moved, add a 301.\n")
        assert not is_moved_signpost("# Thing has moved\n" + "x" * 5000)


# ─── install.sh, run for real against live servers ──────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app: FastAPI):
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture()
def live_real_routes(fake_origin):
    """The REAL well-known routes on a bound socket (urllib can't hit TestClient)."""
    engine = _engine()
    SL = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    seed = SL()
    free = _local(seed, "free-one")
    paid = _local(seed, "paid-one", tier="pro")
    stale = _external(seed, "stale-one")
    moved = _external(seed, "moved-one")
    good = _external(seed, "good-ext")
    _bundle(seed, "mixed", [free, paid, stale, moved, good])
    _bundle(seed, "all-broken", [_external(seed, "stale-two"), _external(seed, "moved-two")])
    _bundle(seed, "empty-bundle", [])
    seed.close()
    fake_origin.update({"moved-one": MOVED_BODY, "moved-two": MOVED_BODY, "good-ext": REAL_EXT_BODY})

    def _override():
        db = SL()
        try:
            yield db
        finally:
            db.close()

    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()
    app.dependency_overrides[get_db] = _override
    app.include_router(wk_router)
    server, thread, base = _serve(app)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        Base.metadata.drop_all(bind=engine)


def _run(api_base: str, slug: str, tmp: Path, **env_extra) -> subprocess.CompletedProcess:
    script = tmp / "install.sh"
    script.write_text(render_install_script(api_base))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["LOOPSKILL_INSTALL_DIR"] = str(tmp / "skills")
    env["LOOPSKILL_RETRY_SLEEP"] = "0"
    env.update(env_extra)
    return subprocess.run(["bash", str(script), slug], capture_output=True, text=True, timeout=60, env=env)


def _installed(tmp: Path) -> set[str]:
    root = tmp / "skills"
    return {p.name for p in root.iterdir()} if root.exists() else set()


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


@needs_bash
def test_mixed_bundle_installs_only_real_skills_and_reports_each_skip(live_real_routes, tmp_path):
    proc = _run(live_real_routes, "mixed", tmp_path)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert _installed(tmp_path) == {"free-one", "ext-hermes-hub-good-ext"}, (
        "no stub/signpost/empty dir on disk"
    )
    assert "installed 2 skill(s)" in out
    assert "skipped 3" in out
    assert "paid-one — locked" in out
    assert "ext:hermes-hub:stale-one — unavailable" in out
    assert "ext:hermes-hub:moved-one — moved" in out
    assert "installed 5" not in out


@needs_bash
def test_bundle_where_nothing_is_installable_exits_nonzero_with_reasons(live_real_routes, tmp_path):
    proc = _run(live_real_routes, "all-broken", tmp_path)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "installed 0 skill(s)" in proc.stdout
    assert "skipped 2" in proc.stdout
    assert _installed(tmp_path) == set()


@needs_bash
def test_empty_bundle_is_not_an_error(live_real_routes, tmp_path):
    proc = _run(live_real_routes, "empty-bundle", tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no skills" in proc.stdout


@pytest.fixture()
def live_fake_server():
    """A hand-rolled server to drive client paths the real routes never produce:
    an OLD server build (no headers / no installable keys, stub body), a
    checksum mismatch, and a transient 429."""
    good = "# good\n\nreal\n"
    old_stub = (
        "---\nname: old-stub\nmetadata:\n  loopskill:\n    locked: true\n---\n\n"
        "> 🔒 **external skill.** The full instructions for this skill are part of the "
        "LoopSkill **external** tier and are not served over the public bundle surface.\n"
    )
    hits: dict[str, int] = {}
    app = FastAPI()

    @app.get("/api/bundles/public/fake/.well-known/skills/index.json")
    def _index():
        return JSONResponse(
            {
                "skills": [
                    {"name": "good", "dir_name": "good", "files": ["SKILL.md"], "sha256": _sha(good)},
                    {"name": "old-stub", "dir_name": "old-stub", "files": ["SKILL.md"]},
                    {"name": "tampered", "dir_name": "tampered", "files": ["SKILL.md"], "sha256": "0" * 64},
                    {"name": "flaky", "dir_name": "flaky", "files": ["SKILL.md"]},
                ],
                "cookbook": {"slug": "fake", "name": "fake", "skill_count": 4},
            }
        )

    @app.get("/api/bundles/public/fake/.well-known/skills/{name}/SKILL.md")
    def _md(name: str):
        hits[name] = hits.get(name, 0) + 1
        if name == "old-stub":
            return PlainTextResponse(old_stub, media_type="text/markdown")
        if name == "flaky" and hits[name] == 1:
            return PlainTextResponse("slow down", status_code=429)
        return PlainTextResponse(good, media_type="text/markdown")

    server, thread, base = _serve(app)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@needs_bash
def test_client_fallbacks_stub_marker_checksum_and_retry(live_fake_server, tmp_path):
    proc = _run(live_fake_server, "fake", tmp_path)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert _installed(tmp_path) == {"good", "flaky"}
    assert "old-stub — locked" in out
    assert "tampered — checksum mismatch" in out
    assert "installed 2 skill(s)" in out and "skipped 2" in out


def test_script_never_uses_unverified_urlretrieve():
    script = render_install_script("https://example.loopskill.test")
    assert "urlretrieve" not in script
    assert "hashlib.sha256" in script


# ─── C03: the SEO bundle's source of truth + the tracked prod fix ────────────

_REPO = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_qa1018_{name}", _REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INTERNAL_NOTE_PHRASES = (
    "kept coming up empty",
    "headroom",
    "federated index",
    "searches",
    "LoopSkill kept",
    "please",
)


def test_seed_seo_theme_has_only_servable_unique_members_and_buyer_copy():
    seed = _load_script("seed_themed_public_bundles_0811")
    fix = _load_script("fix_qa1018_seo_bundle")
    theme = next(t for t in seed.THEMES if t["slug"] == "seo-and-search-growth")
    target = fix.FIXES["seo-and-search-growth"]

    assert ["ext:hermes-hub:" + s for s in theme["skills"]] == target["keep"]
    assert not set(target["keep"]) & set(target["drop"])
    for dropped in target["drop"]:
        assert dropped.removeprefix("ext:hermes-hub:") not in theme["skills"]
    names = ["ai-seo", "ecommerce-seo-audit", "firecrawl-seo-audit", "programmatic-seo"]
    matched = sorted(n for s in theme["skills"] for n in names if s.endswith("-" + n))
    assert matched == sorted(names), "exactly one member per skill name"

    assert theme["description"] == target["description"]
    for phrase in INTERNAL_NOTE_PHRASES:
        assert phrase.lower() not in theme["description"].lower(), phrase


def test_fix_script_dry_run_then_apply_is_exact_and_idempotent(db_session, capsys):
    fix = _load_script("fix_qa1018_seo_bundle")
    target = fix.FIXES["seo-and-search-growth"]
    members = [
        _external(db_session, s.removeprefix("ext:hermes-hub:"))
        for s in target["keep"] + list(target["drop"])
    ]
    # a skill shared with ANOTHER bundle must be left alone there
    other = _bundle(db_session, "other-bundle", [members[-1]])
    cb = _bundle(db_session, "seo-and-search-growth", members)
    cb.description = "old internal note: headroom"
    db_session.commit()

    def active(bundle_id):
        return sorted(
            sk.slug
            for bs, sk in db_session.query(BundleSkill, Skill)
            .join(Skill, Skill.id == BundleSkill.skill_id)
            .filter(BundleSkill.bundle_id == bundle_id, BundleSkill.source != "disabled")
        )

    assert fix.apply(dry_run=True, db=db_session) == 0
    assert len(active(cb.id)) == 8, "dry-run must not write"

    assert fix.apply(dry_run=False, db=db_session) == 0
    assert active(cb.id) == sorted(target["keep"])
    assert db_session.get(Bundle, cb.id).description == target["description"]
    assert active(other.id) == [members[-1].slug], "other bundles untouched"
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).count() == 8, (
        "disable, never delete"
    )

    capsys.readouterr()
    assert fix.apply(dry_run=False, db=db_session) == 0
    assert "0 change(s) planned" in capsys.readouterr().out


def test_fix_script_leaves_personal_bundles_alone_unless_asked(db_session):
    fix = _load_script("fix_qa1018_seo_bundle")
    target = fix.FIXES["agent-marketing"]
    assert target["owner_bundle"] is True
    members = []
    for slug in target["keep"] + list(target["drop"]):
        sk = _external(db_session, slug.split(":", 2)[2])
        sk.slug = slug
        members.append(sk)
    db_session.flush()
    cb = _bundle(db_session, "agent-marketing", members)

    def n_active():
        return (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )

    assert fix.apply(dry_run=False, db=db_session) == 0
    assert n_active() == len(members), "personal bundle untouched by default"
    assert fix.apply(dry_run=False, db=db_session, include_owner_bundles=True) == 0
    assert n_active() == len(target["keep"])
