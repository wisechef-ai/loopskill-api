"""Command-line verification harness for LoopSkill's critical product flows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("./verify.db")
VERIFY_API_KEY = "rec_verify_local_agent_key"
VERIFY_MASTER_KEY = "rec_verify_local_master_key"
VERIFY_SKILL_SLUG = "verify-git-skill"
VERIFY_PAID_SLUG = "verify-paid-secret-skill"
VERIFY_BUNDLE_SLUG = "verify-agent-bundle"
PAID_MARKER = "VERIFY_PAID_BODY_MUST_NEVER_LEAK_7f03"
TARBALL_BYTES = b"loopskill deterministic verify tarball\n"


class VerificationFailure(RuntimeError):
    """A product-flow invariant failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


_VERIFY_ENV_KEYS = (
    "WR_DATABASE_URL",
    "WR_COOKIES_SECURE",
    "WR_API_KEY",
    "WR_REDIS_URL",
    "WR_METASEARCH_SHARED_CACHE",
)


def _verify_env_values(db_path: Path) -> dict[str, str]:
    return {
        "WR_DATABASE_URL": f"sqlite:///{db_path.resolve()}",
        "WR_COOKIES_SECURE": "false",
        "WR_API_KEY": VERIFY_MASTER_KEY,
        "WR_REDIS_URL": "",
        "WR_METASEARCH_SHARED_CACHE": "false",
    }


@contextmanager
def _temporary_verify_env(db_path: Path):
    """Point ``app.config.Settings`` at the verify DB for the duration only.

    The lazy settings singleton is built on first access; when this harness is
    the first thing in the process, that construction must see a sqlite URL or
    the production-secrets gate refuses to boot. The values are RESTORED on
    exit: a permanent ``WR_COOKIES_SECURE=false`` in ``os.environ`` makes every
    later ``Settings()`` under a non-sqlite ``DATABASE_URL`` (the Postgres CI
    lane) raise, which is invisible in the sqlite lane and fails in the other.
    """
    previous = {key: os.environ.get(key) for key in _VERIFY_ENV_KEYS}
    os.environ.update(_verify_env_values(db_path))
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class VerifyContext:
    """One production-app TestClient wired to a disposable SQLite database."""

    db_path: Path

    def __post_init__(self) -> None:
        with _temporary_verify_env(self.db_path):
            self._build()

    def _build(self) -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app import config, database
        from app.database import get_db
        from app.main import create_app
        from app.middleware import api_key as api_key_middleware

        self._original_session_local = database.SessionLocal
        self._original_api_key = config.settings.API_KEY
        self._original_database_url = config.settings.DATABASE_URL
        self._original_get_redis = api_key_middleware.get_redis

        # The verify harness is network-free; last-used tracking is ancillary to
        # every asserted product flow, so keep its lazy Redis client disabled.
        api_key_middleware.get_redis = lambda: None

        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        self.session_factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        database.SessionLocal = self.session_factory
        config.settings.API_KEY = VERIFY_MASTER_KEY
        config.settings.DATABASE_URL = f"sqlite:///{self.db_path}"

        app = create_app()

        def override_get_db():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app, raise_server_exceptions=True)

    def session(self):
        return self.session_factory()

    def close(self) -> None:
        from app import config, database
        from app.middleware import api_key as api_key_middleware

        self.client.close()
        self.engine.dispose()
        database.SessionLocal = self._original_session_local
        config.settings.API_KEY = self._original_api_key
        config.settings.DATABASE_URL = self._original_database_url
        api_key_middleware.get_redis = self._original_get_redis


FlowRunner = Callable[[VerifyContext], dict[str, Any]]
InvariantCheck = Callable[[dict[str, Any]], None]


def _json(response, label: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise VerificationFailure(f"{label}: response was not JSON: {response.text[:200]}") from exc
    _require(isinstance(body, dict), f"{label}: expected a JSON object, got {type(body).__name__}")
    return body


def flow_health(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get("/api/healthz")
    body = _json(response, "health")
    _require(response.status_code == 200, f"health: HTTP {response.status_code}: {body}")
    _require(body.get("status") == "ok", f"health: status={body.get('status')!r}")
    _require(body.get("db") == "ok", f"health: db={body.get('db')!r}")
    _require(bool(body.get("version")), "health: missing version")
    return {
        "ok": True,
        "steps": [f"GET /api/healthz -> status={body['status']}, db={body['db']}, version={body['version']}"],
    }


def flow_skill_search(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get("/api/skills/search", params={"q": "Deterministic", "hybrid": "false"})
    body = _json(response, "skill_search")
    results = body.get("results")
    _require(response.status_code == 200, f"skill_search: HTTP {response.status_code}: {body}")
    _require(isinstance(results, list) and results, "skill_search: no result objects")
    slugs = [row.get("slug") for row in results if isinstance(row, dict)]
    _require(VERIFY_SKILL_SLUG in slugs, f"skill_search: {VERIFY_SKILL_SLUG} absent from {slugs}")
    return {
        "ok": True,
        "steps": [
            f"search q=Deterministic hybrid=false -> total={body.get('total')}, "
            f"results={len(results)}, first={slugs[0]}"
        ],
    }


def flow_skill_detail(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get(f"/api/skills/{VERIFY_SKILL_SLUG}")
    body = _json(response, "skill_detail")
    versions = body.get("versions")
    _require(response.status_code == 200, f"skill_detail: HTTP {response.status_code}: {body}")
    _require(body.get("slug") == VERIFY_SKILL_SLUG, f"skill_detail: slug={body.get('slug')!r}")
    _require("Deterministic verification" in (body.get("readme") or ""), "skill_detail: readme body missing")
    _require(isinstance(versions, list) and versions, "skill_detail: versions missing")
    return {
        "ok": True,
        "steps": [
            f"GET /api/skills/{VERIFY_SKILL_SLUG} -> slug={body['slug']}, "
            f"versions={len(versions)}, latest={versions[0].get('semver')}"
        ],
    }


def flow_skill_install_signed_url_roundtrip(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get(
        "/api/skills/install",
        params={"slug": VERIFY_SKILL_SLUG},
        headers={"x-api-key": VERIFY_API_KEY},
    )
    body = _json(response, "skill_install")
    _require(response.status_code == 200, f"skill_install: HTTP {response.status_code}: {body}")
    _require(body.get("slug") == VERIFY_SKILL_SLUG, f"skill_install: slug={body.get('slug')!r}")
    expected_sha = hashlib.sha256(TARBALL_BYTES).hexdigest()
    _require(body.get("checksum_sha256") == expected_sha, "skill_install: advertised checksum drifted")
    parsed = urlparse(body.get("tarball_url") or "")
    token = parse_qs(parsed.query).get("token", [None])[0]
    _require(bool(token), f"skill_install: tarball_url has no signed token: {body.get('tarball_url')!r}")

    downloaded = ctx.client.get("/api/skills/_download", params={"token": token})
    _require(
        downloaded.status_code == 200, f"skill_download: HTTP {downloaded.status_code}: {downloaded.text}"
    )
    actual_sha = hashlib.sha256(downloaded.content).hexdigest()
    _require(downloaded.content == TARBALL_BYTES, "skill_download: body bytes differ from seeded tarball")
    _require(actual_sha == expected_sha, "skill_download: downloaded checksum differs")
    return {
        "ok": True,
        "steps": [
            f"install slug={VERIFY_SKILL_SLUG} -> version={body.get('version')}, signed_url=true, sha256={expected_sha}",
            f"download signed token -> bytes={len(downloaded.content)}, sha256={actual_sha}",
        ],
    }


def flow_bundle_wellknown_paid_stub_never_leaks_readme(ctx: VerifyContext) -> dict[str, Any]:
    base = f"/api/bundles/public/{VERIFY_BUNDLE_SLUG}/.well-known/skills"
    index_response = ctx.client.get(f"{base}/index.json")
    index = _json(index_response, "bundle_wellknown index")
    skills = index.get("skills")
    _require(index_response.status_code == 200, f"bundle_wellknown index: HTTP {index_response.status_code}")
    _require(isinstance(skills, list), "bundle_wellknown index: skills is not a list")
    paid = next((row for row in skills if row.get("name") == VERIFY_PAID_SLUG), None)
    _require(isinstance(paid, dict), "bundle_wellknown index: paid skill absent")
    _require(paid.get("locked") is True, f"bundle_wellknown index: paid locked={paid.get('locked')!r}")

    stub_response = ctx.client.get(f"{base}/{VERIFY_PAID_SLUG}/SKILL.md")
    stub = stub_response.text
    _require(stub_response.status_code == 200, f"bundle_wellknown stub: HTTP {stub_response.status_code}")
    _require("locked: true" in stub, "bundle_wellknown stub: locked marker missing")
    _require(PAID_MARKER not in stub, "bundle_wellknown stub leaked the paid readme marker")
    return {
        "ok": True,
        "steps": [
            f"bundle index slug={VERIFY_BUNDLE_SLUG} -> skills={len(skills)}, paid_locked={paid['locked']}",
            f"paid SKILL.md -> bytes={len(stub.encode())}, locked_marker=true, paid_marker_present=false",
        ],
    }


def flow_agent_register_ed25519_roundtrip(ctx: VerifyContext) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from app.models import AgentIdentity
    from app.services.agent_registration import canonical_registration_string

    session = ctx.session()
    try:
        ordinal = session.query(AgentIdentity).count() + 1
    finally:
        session.close()
    seed = hashlib.sha256(f"loopskill-verify-agent-{ordinal}".encode()).digest()
    private = Ed25519PrivateKey.from_private_bytes(seed)
    pubkey = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    timestamp = datetime.now(UTC).isoformat()
    nonce = hashlib.sha256(f"loopskill-verify-nonce-{ordinal}".encode()).hexdigest()[:32]
    agent_name = f"verify-agent-{ordinal}"
    canonical = canonical_registration_string(
        pubkey=pubkey,
        timestamp=timestamp,
        nonce=nonce,
        agent_name=agent_name,
    )
    signature = base64.b64encode(private.sign(canonical.encode())).decode()
    response = ctx.client.post(
        "/api/agents/register",
        json={
            "pubkey": pubkey,
            "timestamp": timestamp,
            "nonce": nonce,
            "agent_name": agent_name,
            "signature": signature,
        },
    )
    body = _json(response, "agent_register")
    _require(response.status_code == 201, f"agent_register: HTTP {response.status_code}: {body}")
    _require(
        (body.get("api_key") or "").startswith("rec_agent_"), "agent_register: key prefix is not rec_agent_"
    )
    _require(body.get("scope") == "user", f"agent_register: scope={body.get('scope')!r}")
    _require(body.get("tier") == "free", f"agent_register: tier={body.get('tier')!r}")
    return {
        "ok": True,
        "steps": [
            f"sign canonical={canonical.split(':', 2)[0]}:{canonical.split(':', 2)[1]}:<fields> -> algorithm=Ed25519",
            f"POST /api/agents/register -> HTTP 201, agent={agent_name}, key_prefix=rec_agent_, scope=user, tier=free",
        ],
    }


def flow_checkout_rejects_api_key(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.post(
        "/api/checkout/pro",
        json={},
        headers={"x-api-key": VERIFY_API_KEY},
    )
    body = _json(response, "checkout_rejects_api_key")
    _require(response.status_code == 401, f"checkout_rejects_api_key: HTTP {response.status_code}: {body}")
    _require(body.get("detail") == "login_required", f"checkout fence body={body}")
    _require("url" not in body, "checkout fence returned a checkout URL")
    return {
        "ok": True,
        "steps": [
            "POST /api/checkout/pro with x-api-key only -> HTTP 401, detail=login_required, url_present=false"
        ],
    }


def flow_mcp_tools_list(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get("/api/mcp/healthz")
    body = _json(response, "mcp_tools_list")
    tools = body.get("tools")
    _require(response.status_code == 200, f"mcp_tools_list: HTTP {response.status_code}: {body}")
    _require(isinstance(tools, list), "mcp_tools_list: tools is not a list")
    required = {"loopskill_search", "loopskill_install"}
    _require(required.issubset(set(tools)), f"mcp_tools_list: missing {sorted(required - set(tools))}")
    return {
        "ok": True,
        "steps": [
            f"GET /api/mcp/healthz -> name={body.get('name')}, tools={len(tools)}, "
            "required=loopskill_search,loopskill_install"
        ],
    }


def flow_fleet_skill_serve(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get("/fleet/skill")
    body = response.text
    _require(response.status_code == 200, f"fleet_skill_serve: HTTP {response.status_code}")
    _require(body.startswith("---\n"), "fleet_skill_serve: SKILL.md frontmatter missing")
    _require("fleet" in body.lower(), "fleet_skill_serve: fleet content missing")
    _require("loopskill" in body.lower(), "fleet_skill_serve: LoopSkill identity missing")
    return {
        "ok": True,
        "steps": [
            f"GET /fleet/skill -> content_type={response.headers.get('content-type')}, "
            f"bytes={len(response.content)}, frontmatter=true"
        ],
    }


def flow_wellknown_agent_json(ctx: VerifyContext) -> dict[str, Any]:
    response = ctx.client.get("/.well-known/agent.json")
    body = _json(response, "wellknown_agent_json")
    registration = body.get("registration") or {}
    mcp = body.get("mcp") or {}
    canonical = registration.get("canonical_string")
    _require(response.status_code == 200, f"wellknown_agent_json: HTTP {response.status_code}: {body}")
    _require(
        canonical == "loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}",
        f"wellknown_agent_json: canonical_string={canonical!r}",
    )
    _require(
        urlparse(registration.get("endpoint") or "").path == "/api/agents/register", "bad registration path"
    )
    _require(urlparse(mcp.get("endpoint") or "").path == "/api/mcp/http", "bad MCP path")
    return {
        "ok": True,
        "steps": [
            "GET /.well-known/agent.json -> name=LoopSkill, canonical_version=v1, "
            "registration=/api/agents/register, mcp=/api/mcp/http"
        ],
    }


FLOW_RUNNERS: dict[str, FlowRunner] = {
    "health": flow_health,
    "skill_search": flow_skill_search,
    "skill_detail": flow_skill_detail,
    "skill_install_signed_url_roundtrip": flow_skill_install_signed_url_roundtrip,
    "bundle_wellknown_paid_stub_never_leaks_readme": flow_bundle_wellknown_paid_stub_never_leaks_readme,
    "agent_register_ed25519_roundtrip": flow_agent_register_ed25519_roundtrip,
    "checkout_rejects_api_key": flow_checkout_rejects_api_key,
    "mcp_tools_list": flow_mcp_tools_list,
    "fleet_skill_serve": flow_fleet_skill_serve,
    "wellknown_agent_json": flow_wellknown_agent_json,
}


def _result_is_ok(result: dict[str, Any]) -> None:
    _require(result.get("ok") is True, "flow result did not report ok=true")


INVARIANT_CHECKS: dict[str, tuple[InvariantCheck, ...]] = {
    flow_id: (_result_is_ok,) for flow_id in FLOW_RUNNERS
}


def _db_url(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def _configure_environment(db_path: Path) -> dict[str, str]:
    """Environment for the bootstrap SUBPROCESS only.

    Never mutates ``os.environ``: the in-process harness overrides
    ``config.settings`` directly (see ``VerifyContext``), and leaking
    ``WR_COOKIES_SECURE=false`` into the parent process makes any later
    ``Settings()`` construction under a non-sqlite ``DATABASE_URL`` refuse to
    boot — which is exactly what the Postgres CI lane does after this test.
    """
    env = os.environ.copy()
    env.update(_verify_env_values(db_path))
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def _seed_verify_rows(db_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import APIKey, Bundle, BundleSkill, Skill, SkillVersion, User

    engine = create_engine(_db_url(db_path), connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()
    try:
        user_id = UUID("00000000-0000-4000-8000-000000000101")
        free_id = UUID("00000000-0000-4000-8000-000000000201")
        paid_id = UUID("00000000-0000-4000-8000-000000000202")
        bundle_id = UUID("00000000-0000-4000-8000-000000000301")
        tarball_path = db_path.with_suffix(".verify-skill.tar.gz").resolve()
        tarball_path.write_bytes(TARBALL_BYTES)
        checksum = hashlib.sha256(TARBALL_BYTES).hexdigest()

        user = User(id=user_id, display_name="Verify CLI", email="verify-cli@example.invalid")
        key = APIKey(
            id=UUID("00000000-0000-4000-8000-000000000102"),
            user_id=user_id,
            key_prefix=VERIFY_API_KEY[:12],
            key_hash=hashlib.sha256(VERIFY_API_KEY.encode()).hexdigest(),
            name="verify-cli",
            is_test=True,
        )
        free = Skill(
            id=free_id,
            slug=VERIFY_SKILL_SLUG,
            title="Verify Git Skill",
            description="Deterministic local verification fixture",
            category="development",
            readme="# Verify Git Skill\n\nDeterministic verification body.",
            license="MIT",
            tier="free",
            is_public=True,
        )
        paid = Skill(
            id=paid_id,
            slug=VERIFY_PAID_SLUG,
            title="Verify Paid Secret Skill",
            description="Paid fixture whose body must remain private",
            category="development",
            readme=f"# Paid body\n\n{PAID_MARKER}",
            license="proprietary",
            tier="pro",
            is_public=True,
        )
        version = SkillVersion(
            id=UUID("00000000-0000-4000-8000-000000000203"),
            skill_id=free_id,
            semver="1.0.0",
            tarball_path=str(tarball_path),
            tarball_size_bytes=len(TARBALL_BYTES),
            checksum_sha256=checksum,
            changelog="Deterministic verify fixture",
        )
        bundle = Bundle(
            id=bundle_id,
            bundle_owner=user_id,
            name="Verify Agent Bundle",
            slug=VERIFY_BUNDLE_SLUG,
            description="Disposable verification bundle",
            visibility="public",
        )
        session.add_all([user, key, free, paid, version, bundle])
        session.flush()
        session.add_all(
            [
                BundleSkill(bundle_id=bundle_id, skill_id=free_id, source="custom-added", install_order=1),
                BundleSkill(bundle_id=bundle_id, skill_id=paid_id, source="custom-added", install_order=2),
            ]
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()


def seed_database(db_path: Path) -> dict[str, Any]:
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (db_path, db_path.with_suffix(".verify-skill.tar.gz")):
        candidate.unlink(missing_ok=True)
    env = _configure_environment(db_path)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise VerificationFailure(f"bootstrap failed with exit {completed.returncode}: {detail}")
    with _temporary_verify_env(db_path):
        _seed_verify_rows(db_path)
    return {
        "ok": True,
        "db": str(db_path),
        "api_key": VERIFY_API_KEY,
        "master_key": VERIFY_MASTER_KEY,
        "steps": [
            f"fresh SQLite -> {db_path}",
            "scripts/bootstrap.py -> schema migrated and starter catalog seeded",
            f"verify fixtures -> skill={VERIFY_SKILL_SLUG}, bundle={VERIFY_BUNDLE_SLUG}",
        ],
    }


def _load_feature_map() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "feature-map.yaml").read_text(encoding="utf-8"))
    _require(isinstance(data, dict) and data.get("version") == 1, "feature-map.yaml: version must be 1")
    _require(isinstance(data.get("features"), dict), "feature-map.yaml: features must be a mapping")
    return data


def run_flows(db_path: Path, flow_ids: list[str], *, check: bool = False) -> list[dict[str, Any]]:
    _require(db_path.is_file(), f"database does not exist: {db_path}; run seed first")
    ctx = VerifyContext(db_path.resolve())
    results: list[dict[str, Any]] = []
    try:
        for flow_id in flow_ids:
            runner = FLOW_RUNNERS.get(flow_id)
            _require(runner is not None, f"unknown flow: {flow_id}")
            result = runner(ctx)
            if check:
                for invariant in INVARIANT_CHECKS[flow_id]:
                    invariant(result)
            results.append({"flow": flow_id, **result})
    finally:
        ctx.close()
    return results


def check_all(db_path: Path) -> list[dict[str, Any]]:
    feature_map = _load_feature_map()
    mapped = {feature["verify"] for feature in feature_map["features"].values()}
    _require(
        mapped == set(FLOW_RUNNERS),
        f"feature-map/CLI parity mismatch: map={sorted(mapped)}, cli={sorted(FLOW_RUNNERS)}",
    )
    for feature_id, feature in feature_map["features"].items():
        flows = feature.get("flows")
        _require(isinstance(flows, list) and flows, f"{feature_id}: flows missing")
        for flow in flows:
            invariants = flow.get("invariants")
            _require(isinstance(invariants, list) and invariants, f"{feature_id}: invariants missing")
            _require(
                all(isinstance(item, str) and item.strip() for item in invariants),
                f"{feature_id}: blank invariant",
            )
    return run_flows(db_path, list(FLOW_RUNNERS), check=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="disposable SQLite path (default: ./verify.db)"
    )
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON result")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="recreate and seed the disposable database")
    run = subparsers.add_parser("run", help="run one flow or all flows")
    run.add_argument("flow", choices=[*FLOW_RUNNERS, "all"])
    subparsers.add_parser("check", help="validate map parity and all seeded-state invariants")
    subparsers.add_parser("list", help="list stable flow ids")
    return parser


def _print_text(payload: dict[str, Any]) -> None:
    if payload.get("command") == "list":
        for flow_id in payload["flows"]:
            print(flow_id)
        return
    for result in payload.get("results", []):
        label = result.get("flow", payload.get("command", "verify"))
        for step in result.get("steps", []):
            print(f"[{label}] {step}")
        if "api_key" in result:
            print(f"API_KEY={result['api_key']}")
            print(f"MASTER_KEY={result['master_key']}")
        print(f"[{label}] PASS")
    if payload.get("ok") is False:
        print(f"[verify] FAIL: {payload['error']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            payload = {"ok": True, "command": "list", "flows": list(FLOW_RUNNERS)}
        elif args.command == "seed":
            seeded = seed_database(args.db)
            payload = {"ok": True, "command": "seed", "results": [seeded]}
        elif args.command == "run":
            flow_ids = list(FLOW_RUNNERS) if args.flow == "all" else [args.flow]
            payload = {"ok": True, "command": "run", "results": run_flows(args.db, flow_ids)}
        else:
            payload = {"ok": True, "command": "check", "results": check_all(args.db)}
    except VerificationFailure as exc:
        payload = {"ok": False, "command": args.command, "error": str(exc), "results": []}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            _print_text(payload)
        return 1

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _print_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
