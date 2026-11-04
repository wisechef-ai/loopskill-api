"""ah_1104 — GET /api/agents/enroll.sh: the one-command keyless enroller.

#445 points a keyless caller at POST /api/agents/register. That is a protocol,
not a client; this route serves the client. What is pinned here:

1. The served script carries the canonical prefix/version FROM the proof module
   (no restated copy that can drift) and parses as bash.
2. The route is public: a caller fetches it before it has any key.
3. THE LOAD-BEARING ONE: the script's real OpenSSL 3 output — raw-pubkey
   extraction, base64, nonce, timestamp, Ed25519 signature — is POSTed through
   the real register route and mints a key (201). This is the "the openssl raw
   signature verifies server-side" proof, run without touching production.
4. Unsafe names are refused before anything is generated or written.
5. The 401 breadcrumb and agent.json both advertise the script.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.agent_enroll_script_routes import ENROLL_SCRIPT_PATH, render_enroll_script
from app.main import create_app
from app.middleware._no_key_401 import no_key_401_body
from app.services.agent_registration_proof import (
    CANONICAL_PREFIX,
    CANONICAL_VERSION,
    verify_registration_signature,
)
from tests._app_factory import build_test_app

BASE = "https://enroll.test"


def _openssl_has_ed25519() -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    r = subprocess.run([exe, "genpkey", "-algorithm", "ed25519"], capture_output=True, text=True, check=False)
    return r.returncode == 0 and "PRIVATE KEY" in r.stdout


needs_openssl = pytest.mark.skipif(not _openssl_has_ed25519(), reason="OpenSSL with Ed25519 not available")


def _run(
    tmp_path, *args: str, dry_run: bool = True, path: str = "/usr/local/bin:/usr/bin:/bin"
) -> subprocess.CompletedProcess:
    script = tmp_path / "enroll.sh"
    script.write_text(render_enroll_script(BASE))
    env = {
        "PATH": path,
        "HOME": str(tmp_path / "home"),
        "LOOPSKILL_CONFIG_DIR": str(tmp_path / "conf"),
    }
    if dry_run:
        env["LOOPSKILL_ENROLL_DRY_RUN"] = "1"
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=30, check=False
    )


def test_rendered_script_carries_the_canonical_string_from_the_proof_module():
    body = render_enroll_script(BASE + "/")
    assert "@@" not in body, "an unreplaced template token would ship to callers"
    assert f'MESSAGE="{CANONICAL_PREFIX}:{CANONICAL_VERSION}:${{PUBKEY}}:' in body
    assert f'API_BASE="${{LOOPSKILL_API_BASE:-{BASE}}}"' in body, "trailing slash must be stripped"
    r = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_route_is_public_and_serves_plain_text():
    c = TestClient(create_app())
    r = c.get(ENROLL_SCRIPT_PATH)  # no x-api-key: the point of the route
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("#!/usr/bin/env bash")
    assert "/api/agents/register" in r.text


@needs_openssl
def test_script_output_is_accepted_by_the_real_register_route(tmp_path, db_session, monkeypatch):
    run = _run(tmp_path, "cold-agent-1", "ops@example.com")
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert set(payload) == {"pubkey", "timestamp", "nonce", "agent_name", "signature", "contact"}
    assert payload["agent_name"] == "cold-agent-1"

    # Pure verifier first: a failure here names the signing layer, not the DB.
    verify_registration_signature(
        pubkey=payload["pubkey"],
        timestamp=payload["timestamp"],
        nonce=payload["nonce"],
        agent_name=payload["agent_name"],
        signature=payload["signature"],
    )

    client = TestClient(
        build_test_app(db_session=db_session, monkeypatch=monkeypatch), raise_server_exceptions=False
    )
    r = client.post("/api/agents/register", json=payload)
    assert r.status_code in (200, 201), r.text
    assert r.json()["api_key"]
    # the private key the script generated is kept for rotation, owner-only
    key = tmp_path / "conf" / "agent-ed25519.pem"
    assert key.is_file() and (key.stat().st_mode & 0o077) == 0


@needs_openssl
def test_a_rerun_reuses_the_key_and_mints_a_fresh_nonce(tmp_path):
    a = json.loads(_run(tmp_path, "cold-agent-2").stdout)
    b = json.loads(_run(tmp_path, "cold-agent-2").stdout)
    assert a["pubkey"] == b["pubkey"], "a retry must not burn a new identity"
    assert a["nonce"] != b["nonce"]


@pytest.mark.parametrize("name", ['bad"name', "-leading-dash", "x" * 65, "has space", "a\\b"])
def test_unsafe_agent_names_are_refused_before_anything_is_written(tmp_path, name):
    run = _run(tmp_path, name)
    assert run.returncode == 2, (run.returncode, run.stderr)
    assert not (tmp_path / "conf").exists(), "nothing may be generated for a refused name"


def test_missing_name_is_usage_error(tmp_path):
    assert _run(tmp_path).returncode == 2


def test_existing_api_key_refuses_a_second_enrolment(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "api_key").write_text("lsk_existing\n")
    run = _run(tmp_path, "cold-agent-3", dry_run=False)
    assert run.returncode == 5, (run.returncode, run.stderr)
    assert (conf / "api_key").read_text() == "lsk_existing\n"


def test_breadcrumb_and_agent_json_advertise_the_script():
    hint = no_key_401_body()["how_to_get_a_key"]
    assert hint["enroll"] == "POST /api/agents/register"
    assert hint["enroll_script"].endswith("/api/agents/enroll.sh | bash -s -- <agent-name>")
    c = TestClient(create_app())
    reg = c.get("/.well-known/agent.json").json()["registration"]
    assert reg["enroll_script"]["url"].endswith(ENROLL_SCRIPT_PATH)
    assert "openssl>=3" in reg["enroll_script"]["requires"]


SECRET = "lsk_agent_ENROLLSECRET0123456789"


def _stub_curl(bindir, marker):
    """A curl that records the POSTed body and answers 201 with a key."""
    bindir.mkdir(exist_ok=True)
    stub = bindir / "curl"
    body = json.dumps({"agent_name": "x", "api_key": SECRET})
    stub.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do [ "$1" = --data ] && { shift; printf %s "$1" > '
        + str(marker)
        + "; }; shift; done\n"
        "cat <<'RESP'\n" + body + "\n201\nRESP\n"
    )
    stub.chmod(0o755)


@needs_openssl
def test_post_path_saves_the_key_owner_only_and_never_prints_it(tmp_path):
    marker = tmp_path / "posted.json"
    _stub_curl(tmp_path / "bin", marker)
    run = _run(
        tmp_path, "cold-agent-4", dry_run=False, path=f"{tmp_path / 'bin'}:/usr/local/bin:/usr/bin:/bin"
    )
    assert run.returncode == 0, run.stderr
    assert SECRET not in run.stdout + run.stderr, "the shown-once key must never reach stdout/stderr"
    conf = tmp_path / "conf"
    assert (conf / "api_key").read_text().strip() == SECRET
    for name in ("api_key", "enrollment.json", "agent-ed25519.pem"):
        assert (conf / name).stat().st_mode & 0o077 == 0, name
    posted = json.loads(marker.read_text())
    verify_registration_signature(
        **{k: posted[k] for k in ("pubkey", "timestamp", "nonce", "agent_name", "signature")}
    )


@needs_openssl
def test_a_reused_private_key_is_tightened_to_owner_only(tmp_path):
    assert _run(tmp_path, "cold-agent-5").returncode == 0
    key = tmp_path / "conf" / "agent-ed25519.pem"
    key.chmod(0o644)
    assert _run(tmp_path, "cold-agent-5").returncode == 0
    assert key.stat().st_mode & 0o077 == 0


@needs_openssl
def test_a_missing_parser_fails_before_the_server_mints_anything(tmp_path):
    # sed parses the shown-once key: without it the script must stop BEFORE the
    # POST, never after the server has issued a key it cannot save.
    bindir = tmp_path / "bin"
    marker = tmp_path / "posted.json"
    _stub_curl(bindir, marker)
    for tool in ("openssl", "base64", "tail", "tr", "date", "chmod", "mktemp", "mkdir", "rm", "sh", "bash"):
        exe = shutil.which(tool)
        assert exe, tool
        (bindir / tool).symlink_to(exe)
    run = _run(tmp_path, "cold-agent-6", dry_run=False, path=str(bindir))
    assert run.returncode == 3, (run.returncode, run.stderr)
    assert "sed" in run.stderr
    assert not marker.exists(), "no request may be sent when the key could not be saved"


@needs_openssl
@pytest.mark.parametrize("name", ["agent-ed25519.pem", "api_key", "enrollment.json"])
def test_a_dangling_symlink_at_any_secret_path_is_refused_not_followed(tmp_path, name):
    conf = tmp_path / "conf"
    conf.mkdir()
    target = tmp_path / "elsewhere"
    (conf / name).symlink_to(target)  # dangling: target absent
    marker = tmp_path / "posted.json"
    _stub_curl(tmp_path / "bin", marker)
    run = _run(
        tmp_path, "cold-agent-7", dry_run=False, path=f"{tmp_path / 'bin'}:/usr/local/bin:/usr/bin:/bin"
    )
    assert run.returncode == 3, (run.returncode, run.stderr)
    assert not target.exists(), "secrets must never be written through a symlink"
    assert not marker.exists(), "refuse before the server mints a key"
