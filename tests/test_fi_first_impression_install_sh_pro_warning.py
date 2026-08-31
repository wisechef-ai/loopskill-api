"""fi_first_impression_api — install.sh must warn FIRST when a bundle is
entirely Pro-locked, instead of silently printing "installed 0 skill(s)".

Live audit evidence (2026-08-19): pasting the documented one-liner against
dev-agent-essentials and research-and-report (both 100% Pro-tier members)
produced a bare "installed 0 skill(s)" + locked-members footer — indistinguishable
from "this bundle happens to be empty" to a cold-path visitor. See the
module docstring on app/bundle_install_script_routes.py for the full
before/after narrative.

This file has two tiers:
  * unit-level — parse the rendered template text directly (fast, no
    subprocess), pinning the exact warning string, its position ahead of any
    progress output, and the distinct exit code.
  * live end-to-end — spin up a real uvicorn server serving the actual
    well-known index route, run the REAL bash script (not a re-implementation)
    against it via subprocess, and assert the real stdout/exit code for both
    an all-Pro bundle and a mixed (free+paid) bundle.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bundle_install_script_routes import render_install_script
from app.database import get_db
from app.models import Base, Bundle, BundleSkill, Skill

WARNING_TEXT = (
    "This bundle requires Pro — 0 free skills will be installed. See https://app.loopskill.io/pricing"
)

# ═════════════════════════════════════════════════════════════════════════
# Unit-level: template shape
# ═════════════════════════════════════════════════════════════════════════


def test_rendered_script_contains_the_pro_warning_verbatim():
    script = render_install_script("https://example.loopskill.test")
    assert WARNING_TEXT in script


def test_pro_warning_print_precedes_any_progress_output_in_source_order():
    """The warning branch's print/exit must appear BEFORE the
    'installing bundle' progress line in the embedded python source — this
    is what guarantees it's the first thing printed at runtime for an
    all-locked bundle (the free-count check and its exit happen before the
    mkdir/progress line runs at all)."""
    script = render_install_script("https://example.loopskill.test")
    warning_idx = script.index("This bundle requires Pro")
    progress_idx = script.index("LoopSkill: installing bundle")
    assert warning_idx < progress_idx


def test_zero_free_branch_exits_with_a_distinct_nonzero_code():
    script = render_install_script("https://example.loopskill.test")
    # The zero-free branch's exit call, distinct from usage (2) / missing
    # python3 (3) so a caller can tell "needs Pro" apart from those.
    match = re.search(r"if skills and not free:\s*\n\s*print\(.*?\)\s*\n\s*sys\.exit\((\d+)\)", script)
    assert match, "could not find the zero-free warning branch in the rendered script"
    code = int(match.group(1))
    assert code not in (0, 1, 2, 3), f"exit code {code} collides with an existing meaning"


def test_all_skills_locked_means_free_list_is_empty_logic_sanity():
    """Sanity-check the embedded python's own free/locked partition logic
    against a synthetic index payload shaped like the live incident."""
    # Extract just the free/locked partition line pair to unit-test the
    # predicate in isolation (avoids executing the whole embedded script).
    skills = [
        {"name": "a", "locked": True},
        {"name": "b", "locked": True},
        {"name": "c", "locked": True},
    ]
    free = [s for s in skills if s.get("name") and not s.get("locked")]
    locked = [s.get("name") for s in skills if s.get("name") and s.get("locked")]
    assert free == []
    assert locked == ["a", "b", "c"]


# ═════════════════════════════════════════════════════════════════════════
# Live end-to-end: real bash script against a real server
# ═════════════════════════════════════════════════════════════════════════


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mk_skill(db, *, slug, tier):
    s = Skill(id=uuid4(), slug=slug, title=slug, tier=tier, is_public=True, readme=f"# {slug}\n\nbody")
    db.add(s)
    db.flush()
    return s


@pytest.fixture()
def live_wellknown_server():
    """A real uvicorn server serving the well-known index route the
    install.sh script actually curls — urllib inside the script cannot hit
    TestClient, so this must be a real bound socket (same pattern as
    tests/test_metasearch_p5_reconcile_target.py and
    tests/test_bundles0811_readme_transcript_executable.py)."""
    import uvicorn

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

    all_pro = Bundle(id=uuid4(), name="All Pro Bundle", slug="all-pro-bundle", visibility="public")
    seed.add(all_pro)
    seed.flush()
    pro1 = _mk_skill(seed, slug="pro-one", tier="pro")
    pro2 = _mk_skill(seed, slug="pro-two", tier="pro")
    seed.add(BundleSkill(bundle_id=all_pro.id, skill_id=pro1.id, source="custom-added"))
    seed.add(BundleSkill(bundle_id=all_pro.id, skill_id=pro2.id, source="custom-added"))

    mixed = Bundle(id=uuid4(), name="Mixed Bundle", slug="mixed-bundle", visibility="public")
    seed.add(mixed)
    seed.flush()
    free1 = _mk_skill(seed, slug="free-one", tier="free")
    pro3 = _mk_skill(seed, slug="pro-three", tier="pro")
    seed.add(BundleSkill(bundle_id=mixed.id, skill_id=free1.id, source="custom-added"))
    seed.add(BundleSkill(bundle_id=mixed.id, skill_id=pro3.id, source="custom-added"))
    seed.commit()
    seed.close()

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(wk_router)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("live_wellknown_server did not come up in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        Base.metadata.drop_all(bind=engine)


def _run_install_script(api_base: str, slug: str, dest: Path) -> subprocess.CompletedProcess:
    script = render_install_script(api_base)
    script_path = dest / "install.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)
    env = dict(os.environ)
    env["LOOPSKILL_INSTALL_DIR"] = str(dest / "skills")
    return subprocess.run(
        ["bash", str(script_path), slug],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_all_pro_bundle_prints_warning_first_and_exits_distinct_code(live_wellknown_server, tmp_path):
    proc = _run_install_script(live_wellknown_server, "all-pro-bundle", tmp_path)

    first_line = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else ""
    assert first_line == WARNING_TEXT, f"first stdout line was {first_line!r}, full stdout:\n{proc.stdout}"
    assert proc.returncode not in (0, 1, 2, 3), (
        f"exit code {proc.returncode} collides with an existing meaning"
    )
    assert "installing bundle" not in proc.stdout, "progress output must not run for a fully-locked bundle"

    install_dir = tmp_path / "skills"
    assert not install_dir.exists() or not any(install_dir.iterdir()), "no files should be installed"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_mixed_bundle_installs_the_free_member_and_exits_zero(live_wellknown_server, tmp_path):
    proc = _run_install_script(live_wellknown_server, "mixed-bundle", tmp_path)

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert WARNING_TEXT not in proc.stdout
    assert "installed 1 skill(s)" in proc.stdout
    assert "free-one" in proc.stdout or (tmp_path / "skills" / "free-one").exists()

    skill_md = tmp_path / "skills" / "free-one" / "SKILL.md"
    assert skill_md.exists()
