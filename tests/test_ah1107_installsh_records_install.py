"""ah_1107 — a first install through install.sh is RECORDED, and attributed
when the agent has enrolled.

Before: the keyless first-install line (enroll.sh -> install.sh, #451) worked
end to end (a throwaway-HOME run installed 3 skills) but wrote no
InstallEvent — ``bundle_wellknown_routes.py`` had no recorder and install.sh
sent no key, so a stranger's first install was invisible to every funnel.

After:
  * the well-known SKILL.md route records an InstallEvent (bundle-stamped,
    provenance-minted) when it serves a REAL body to the installer;
  * install.sh forwards ``$LOOPSKILL_API_KEY`` or the key enroll.sh stored at
    ``${LOOPSKILL_CONFIG_DIR:-~/.config/loopskill}/api_key`` as x-api-key, and
    the route attributes the event to that key;
  * a non-installer GET (crawler/browser) of the same public page records
    nothing, so the public install counter cannot be crawler-inflated;
  * placeholders (locked stubs) are never counted;
  * a recorder failure never breaks serving the file.

The end-to-end tests run the REAL rendered bash script against a real uvicorn
server, the same pattern as test_fi_first_impression_install_sh_pro_warning.py.
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
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bundle_install_script_routes import render_install_script
from app.database import get_db
from app.models import APIKey, Base, Bundle, BundleSkill, InstallEvent, Skill, User

AGENT_KEY = "rec_agent_" + "a" * 32


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def world():
    """In-memory DB + a mixed bundle (1 free, 1 pro) + one enrolled agent key."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _r):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    seed = SessionLocal()
    bundle = Bundle(id=uuid4(), name="Mixed", slug="mixed-bundle", visibility="public")
    seed.add(bundle)
    seed.flush()
    free = Skill(
        id=uuid4(),
        slug="free-one",
        title="free-one",
        tier="free",
        is_public=True,
        readme="# free-one\n\nbody",
    )
    pro = Skill(
        id=uuid4(), slug="pro-two", title="pro-two", tier="pro", is_public=True, readme="# pro\n\nsecret"
    )
    seed.add_all([free, pro])
    seed.flush()
    seed.add(BundleSkill(bundle_id=bundle.id, skill_id=free.id, source="custom-added"))
    seed.add(BundleSkill(bundle_id=bundle.id, skill_id=pro.id, source="custom-added"))
    user = User(id=uuid4(), display_name="stranger agent", email=f"{uuid4()}@agents.example")
    seed.add(user)
    seed.flush()
    key = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix=AGENT_KEY[:8],
        key_hash=hashlib.sha256(AGENT_KEY.encode()).hexdigest(),
        is_active=True,
    )
    seed.add(key)
    seed.commit()
    ids = {"bundle": bundle.id, "free": free.id, "pro": pro.id, "key": key.id, "user": user.id}
    seed.close()

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from app.auth_ctx import AuthContext
    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()

    # Stand-in for APIKeyMiddleware's PUBLIC_PREFIXES branch: opportunistic
    # auth stamps request.state.auth_ctx (with api_key_id) and NOTHING else —
    # exactly the shape the real middleware leaves on /api/bundles/public/*.
    @app.middleware("http")
    async def _opportunistic_auth(request: Request, call_next):
        sent = request.headers.get("x-api-key")
        if sent == AGENT_KEY:
            request.state.auth_ctx = AuthContext(scope="user", user_id=ids["user"], api_key_id=ids["key"])
        else:
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(wk_router)

    def events():
        db = SessionLocal()
        try:
            return [(e.skill_id, e.api_key_id, e.bundle_id) for e in db.query(InstallEvent).all()]
        finally:
            db.close()

    yield app, ids, events
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def live(world):
    import uvicorn

    app, ids, events = world
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
    else:
        raise RuntimeError("server did not come up")
    try:
        yield f"http://127.0.0.1:{port}", ids, events
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _run(api_base: str, tmp: Path, *, env_key: str | None = None, file_key: str | None = None):
    script = tmp / "install.sh"
    script.write_text(render_install_script(api_base))
    home = tmp / "home"
    home.mkdir()
    # Strip every credential alias from the inherited env (pitfall: a shell
    # RECIPES_/LOOPSKILL_ key would silently attribute the "anonymous" run).
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and not k.startswith("LOOPSKILL_")}
    env["HOME"] = str(home)
    env["LOOPSKILL_INSTALL_DIR"] = str(tmp / "skills")
    if env_key is not None:
        env["LOOPSKILL_API_KEY"] = env_key
    if file_key is not None:
        conf = home / ".config" / "loopskill"
        conf.mkdir(parents=True)
        (conf / "api_key").write_text(file_key + "\n")
    return subprocess.run(
        ["bash", str(script), "mixed-bundle"], capture_output=True, text=True, timeout=30, env=env
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_anonymous_first_install_is_recorded_with_bundle(live, tmp_path):
    base, ids, events = live
    proc = _run(base, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed 1 skill(s)" in proc.stdout
    assert events() == [(ids["free"], None, ids["bundle"])], (
        "exactly one event: the free member, anonymous, bundle-stamped"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_enrolled_key_file_attributes_the_install(live, tmp_path):
    base, ids, events = live
    proc = _run(base, tmp_path, file_key=AGENT_KEY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert events() == [(ids["free"], ids["key"], ids["bundle"])]
    assert AGENT_KEY not in proc.stdout + proc.stderr, "the key is never printed"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_env_key_wins_over_the_key_file(live, tmp_path):
    base, ids, events = live
    proc = _run(base, tmp_path, env_key=AGENT_KEY, file_key="rec_agent_" + "b" * 32)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert events() == [(ids["free"], ids["key"], ids["bundle"])]


def test_crawler_get_of_the_public_page_records_nothing(world):
    app, _ids, events = world
    c = TestClient(app)
    r = c.get(
        "/api/bundles/public/mixed-bundle/.well-known/skills/free-one/SKILL.md",
        headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
    )
    assert r.status_code == 200 and "body" in r.text
    assert events() == []


def test_locked_stub_is_never_counted_even_for_the_installer(world):
    app, _ids, events = world
    c = TestClient(app)
    r = c.get(
        "/api/bundles/public/mixed-bundle/.well-known/skills/pro-two/SKILL.md",
        headers={"User-Agent": "loopskill-install.sh"},
    )
    assert r.status_code == 200 and "secret" not in r.text
    assert events() == []


def test_recorder_failure_still_serves_the_file(world, monkeypatch):
    import app.services.provenance as prov

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(prov, "record_install_with_provenance", _boom)
    app, _ids, events = world
    r = TestClient(app).get(
        "/api/bundles/public/mixed-bundle/.well-known/skills/free-one/SKILL.md",
        headers={"User-Agent": "loopskill-install.sh"},
    )
    assert r.status_code == 200 and "body" in r.text
    assert events() == []


def test_forged_installer_ua_loop_cannot_pump_the_counter(world):
    """Review finding (PR #452): the UA is caller-controlled. Looping the
    public GET with it must yield ONE event per (skill, caller) per 24h."""
    app, ids, events = world
    c = TestClient(app)
    for _ in range(5):
        r = c.get(
            "/api/bundles/public/mixed-bundle/.well-known/skills/free-one/SKILL.md",
            headers={"User-Agent": "loopskill-install.sh"},
        )
        assert r.status_code == 200
    assert events() == [(ids["free"], None, ids["bundle"])]


def test_a_keyed_caller_is_a_distinct_install_from_the_anonymous_one(world):
    app, ids, events = world
    c = TestClient(app)
    url = "/api/bundles/public/mixed-bundle/.well-known/skills/free-one/SKILL.md"
    c.get(url, headers={"User-Agent": "loopskill-install.sh"})
    for _ in range(3):
        c.get(url, headers={"User-Agent": "loopskill-install.sh", "x-api-key": AGENT_KEY})
    assert sorted(events(), key=str) == sorted(
        [(ids["free"], None, ids["bundle"]), (ids["free"], ids["key"], ids["bundle"])], key=str
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rerunning_install_sh_is_the_same_install(live, tmp_path):
    base, ids, events = live
    for i in range(2):
        sub = tmp_path / f"run{i}"
        sub.mkdir()
        proc = _run(base, sub)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    assert events() == [(ids["free"], None, ids["bundle"])]
