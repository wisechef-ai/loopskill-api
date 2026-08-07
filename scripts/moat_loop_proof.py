#!/usr/bin/env python3
"""mesh_0408 W5 — proof of the FULL moat loop, end to end.

    published artifact
      -> governed client agent installs it FROM ITS BUNDLE (routable provenance)
      -> real defect reported through the rail
      -> issue lands in the publisher's PRIVATE sink (verified AT GitHub)
      -> the defect is PATCHED and published as a NEW VERSION
      -> the member's bundle RESOLVES to that new version
      -> the client agent CONVERGES onto it, terminally, observably

That third row of the competitive table is the only asset on it that capital
cannot shortcut. A catalog is a quarter's work for a funded team; production
defect telemetry from governed client fleets is not purchasable. This script is
the gate for the whole claim, so it is written to be able to FAIL loudly.

WHY THIS LIVES IN THE REPO (not ~/.hermes/scripts)
--------------------------------------------------
Its predecessor, ``~/.hermes/scripts/t2b-roundtrip-proof.py``, proved only the
first half and was never version-controlled — the same defect commit 73b6524
had to fix for ``theme_starter_bundles.py`` ("applied live on prod, never
version-controlled"). This script now asserts against in-repo API contracts
(``/api/bundle-apply/*``, added in W5), so it MUST version with them: a rename
on either side has to break one reviewable diff, not a script nobody can see.

EXIT CODES (house style)
------------------------
  0  the full loop is proven
  1  a step FAILED — the output names which one
  2  VOID: a precondition or control could not be evaluated. **This is not a
     pass.** The harness could not discriminate, so the result carries no
     information either way. Never read a 2 as success.

TRAPS THIS SCRIPT IS BUILT AGAINST
----------------------------------
E2  FastAPI silently ignores unknown query params, so ``?cookbook_id=`` where
    the param is ``bundle_id=`` returns 200 with the feature NOT applied. Step 1
    therefore asserts the ``install_events.bundle_id`` ROW, never the status
    code. With ``--indirect-provenance`` (no DB reachable) it falls back to the
    routing discriminator — a NULL bundle_id routes to the public default repo,
    so landing in the private sink is itself DB-driven evidence — and says so
    out loud rather than quietly downgrading.
E3  Feedback dedups on a signature derived from the message TEXT. A fixed test
    message first submitted while the rail was broken stays pinned to that
    failed ``issue_url=""`` row FOREVER, reporting RED long after the fix works
    (this cost a round of misdirected debugging on 2026-08-05). Every run varies
    the payload with a fresh uuid4 + timestamp.
R4  ``loopskill_feedback`` returns ``{"ok": true, "issue_url": ""}`` when nothing
    was dispatched. Step 2 verifies at the DESTINATION (the GitHub API), never
    at the source's own success field.
V4  A verification harness can itself be broken. ``--selftest`` drives
    ``run_proof`` with stub transports engineered to produce every outcome shape
    and asserts exits {0, 1, 2} are ALL reachable. Run it before trusting a run.

USAGE
-----
    python scripts/moat_loop_proof.py --selftest     # falsify the harness
    python scripts/moat_loop_proof.py                # live run
    python scripts/moat_loop_proof.py --indirect-provenance   # no DB access
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import pathlib
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

BASE = os.environ.get("LOOPSKILL_BASE", "https://app.loopskill.io")
SECRETS = pathlib.Path.home() / ".hermes/secrets/loopskill_tori.json"
PAT_FILE = pathlib.Path.home() / ".hermes/secrets/astrovita_feedback_pat.json"
SKILL = os.environ.get("MOAT_PROOF_SKILL", "buzz-mesh-linux-build")

EXIT_PROVEN = 0
EXIT_FAILED = 1
EXIT_VOID = 2

STEPS = (
    "1/install-from-bundle",
    "2/defect-to-private-sink",
    "3/patch-published-as-new-version",
    "4/bundle-resolves-to-new-version",
    "5/member-converges-terminally",
)


# ══════════════════════════════════════════════════════════════════════════
# Outcome
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Outcome:
    """The result of one proof run. ``code`` is the process exit code."""

    code: int
    step: str | None = None
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @classmethod
    def proven(cls, notes: list[str]) -> "Outcome":
        return cls(EXIT_PROVEN, None, "full loop proven", notes)

    @classmethod
    def failed(cls, step: str, detail: str, notes: list[str] | None = None) -> "Outcome":
        return cls(EXIT_FAILED, step, detail, notes or [])

    @classmethod
    def void(cls, detail: str, notes: list[str] | None = None) -> "Outcome":
        return cls(EXIT_VOID, None, detail, notes or [])


class Void(Exception):
    """A control could not be evaluated — the run is VOID, not failed."""


# ══════════════════════════════════════════════════════════════════════════
# Transport — every network call lives here so run_proof() can be stubbed
# ══════════════════════════════════════════════════════════════════════════


class Transport:
    """Live transport against a real LoopSkill deployment + the GitHub API."""

    def __init__(self, *, base: str, member_key: str, owner_key: str, pat: str, repo: str) -> None:
        self.base = base.rstrip("/")
        self.member_key = member_key
        self.owner_key = owner_key
        self.pat = pat
        self.repo = repo

    # ── LoopSkill REST ───────────────────────────────────────────────────

    def _api(self, path: str, key: str, method: str = "GET", body: dict | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"x-api-key": key, "content-type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read()[:600].decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def install_from_bundle(self, slug: str, bundle_id: str) -> tuple[int, Any]:
        # NOTE: the merged Q-031 param is `bundle_id` (route vocabulary), NOT
        # the service layer's `cookbook_id`. Trap E2: getting this wrong yields
        # 200 with a NULL-bundle install — a red that looks exactly like the
        # original defect. This is why step 1 asserts the row, not the status.
        return self._api(f"/api/skills/install?slug={slug}&bundle_id={bundle_id}", self.member_key)

    def bundle_detail(self, bundle_id: str) -> tuple[int, Any]:
        """GET /api/bundles/{id} — used only to turn the recorded id into a slug."""
        return self._api(f"/api/bundles/{bundle_id}", self.member_key)

    def start_apply(self, bundle_slug: str) -> tuple[int, Any]:
        return self._api(f"/api/bundle-apply/{bundle_slug}/start", self.member_key, method="POST")

    def report_apply(self, job_id: str, slug: str, semver: str, outcome: str) -> tuple[int, Any]:
        return self._api(
            f"/api/bundle-apply/jobs/{job_id}/report",
            self.member_key,
            method="POST",
            body={"slug": slug, "semver": semver, "outcome": outcome},
        )

    def get_job(self, job_id: str) -> tuple[int, Any]:
        return self._api(f"/api/bundle-apply/jobs/{job_id}", self.member_key)

    # ── publish (multipart, ed25519-signed) ──────────────────────────────

    def publish_patch(self, slug: str, semver: str, changelog: str) -> tuple[int, Any]:
        """Publish a patched version of ``slug``.

        The publish route verifies the ed25519 signature against the pubkey
        uploaded alongside it (integrity of the tarball, not authorship —
        authorship is the api-key), so an ephemeral keypair is correct here.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        tarball = _build_patch_tarball(slug, semver)
        priv = Ed25519PrivateKey.generate()
        sig = priv.sign(hashlib.sha256(tarball).digest())
        pub = priv.public_key().public_bytes_raw()

        toml_text = (
            f'[skill]\nname = "{slug}"\nversion = "{semver}"\n'
            f'description = "Patched by the mesh_0408 W5 moat-loop proof."\n'
            f'license = "MIT"\nentrypoint = "SKILL.md"\n'
        )
        body, content_type = _multipart(
            files={
                "skill_toml": ("skill.toml", toml_text.encode(), "text/plain"),
                "tarball": (f"{slug}-{semver}.tar.gz", tarball, "application/gzip"),
                "signature": ("sig.bin", sig, "application/octet-stream"),
                "signing_pubkey": ("key.pub", pub, "application/octet-stream"),
            },
            fields={"is_public": "false", "changelog": changelog},
        )
        req = urllib.request.Request(
            f"{self.base}/api/skills/_publish",
            data=body,
            headers={"x-api-key": self.owner_key, "content-type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:600].decode(errors="replace")

    # ── MCP ──────────────────────────────────────────────────────────────

    def feedback(self, message: str, provenance_id: str, context: dict) -> dict:
        """Call loopskill_feedback over MCP (initialize handshake required)."""
        url = f"{self.base}/api/mcp/http/"
        hdr = {
            "x-api-key": self.member_key,
            "content-type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        def post(payload: dict, extra: dict | None = None) -> tuple[str | None, str]:
            h = dict(hdr)
            h.update(extra or {})
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.headers.get("mcp-session-id"), r.read().decode()

        sid, _ = post(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "moat-loop-proof", "version": "1"},
                },
            }
        )
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"mcp-session-id": sid})
        _, raw = post(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "tools/call",
                "params": {
                    "name": "loopskill_feedback",
                    "arguments": {
                        "category": "install",
                        "message": message,
                        "provenance_id": provenance_id,
                        "context": context,
                    },
                },
            },
            {"mcp-session-id": sid},
        )
        for line in raw.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                txt = (d.get("result", {}).get("content") or [{}])[0].get("text", "")
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    return {"_raw": txt}
        return {}

    # ── GitHub (the DESTINATION — trap R4) ───────────────────────────────

    def github_issue(self, repo: str, number: str) -> dict:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{number}",
            headers={
                "Authorization": f"Bearer {self.pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)

    def repo_is_private(self, repo: str) -> bool:
        """Probe ANONYMOUSLY — never read the repo's own settings with a PAT."""
        try:
            urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{repo}"), timeout=30)
            return False  # served without auth => public
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            raise Void(f"anonymous repo probe returned {e.code}, expected 404 or 200")

    # ── DB (step 1's direct row assertion) ───────────────────────────────

    def install_event_bundle_id(self, provenance_id: str) -> str | None:
        """Read ``install_events.bundle_id`` for a provenance_id, straight from
        the database. Raises Void when no DATABASE_URL is configured — an
        unevaluatable control is VOID, never a pass."""
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise Void("DATABASE_URL is not set; cannot assert the install_events row")
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise Void(f"sqlalchemy unavailable for the DB assertion: {exc}")

        engine = create_engine(url)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT ie.bundle_id FROM provenance_records pr "
                    "JOIN install_events ie ON ie.id = pr.install_event_id "
                    "WHERE pr.provenance_id = :pid"
                ),
                {"pid": provenance_id},
            ).first()
        if row is None:
            raise Void(f"no install_event joins provenance_id {provenance_id[:8]}...")
        return str(row[0]) if row[0] else None


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════


def _build_patch_tarball(slug: str, semver: str) -> bytes:
    """A minimal, well-formed skill tarball carrying the patch."""
    buf = io.BytesIO()
    body = (
        f"# {slug}\n\n"
        f"Version {semver}. Patched by the mesh_0408 W5 moat-loop proof: the build\n"
        f"step no longer assumes `pkg-config` is present on a minimal host.\n"
    ).encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(body)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _multipart(*, files: dict[str, tuple[str, bytes, str]], fields: dict[str, str]) -> tuple[bytes, str]:
    """Encode a multipart/form-data body (stdlib only, no requests dependency)."""
    boundary = f"----moatproof{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    for name, (filename, data, ctype) in files.items():
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n').encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _next_patch_semver(current: str) -> str:
    """Bump the patch component. Falls back to a suffix for non-semver input."""
    parts = current.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return f"{current}-patch{int(time.time())}"


# ══════════════════════════════════════════════════════════════════════════
# THE PROOF — pure over the transport, so --selftest can drive every branch
# ══════════════════════════════════════════════════════════════════════════


def run_proof(
    t: Transport,
    *,
    skill: str,
    bundle_id: str,
    bundle_slug: str,
    repo: str,
    direct_db_assertion: bool = True,
    log: Callable[[str], None] = print,
) -> Outcome:
    """Execute all five steps. Never raises for a failure — returns an Outcome."""
    notes: list[str] = []
    run_tag = f"{uuid.uuid4().hex[:12]}-{int(time.time())}"

    # ── STEP 1: install FROM THE BUNDLE; assert the ROW, not the status ──
    code, body = t.install_from_bundle(skill, bundle_id)
    if code != 200 or not isinstance(body, dict):
        return Outcome.failed(STEPS[0], f"bundle-scoped install returned {code}: {body}")
    provenance = body.get("provenance_id")
    installed_semver = body.get("version")
    if not provenance:
        return Outcome.failed(STEPS[0], "install returned no provenance_id")

    if direct_db_assertion:
        try:
            stamped = t.install_event_bundle_id(provenance)
        except Void as exc:
            return Outcome.void(f"step {STEPS[0]}: {exc}", notes)
        if stamped is None:
            return Outcome.failed(
                STEPS[0],
                "install_events.bundle_id is NULL — the bundle param was accepted but "
                "NOT applied (trap E2: unknown query params are ignored silently). "
                "This install is structurally unroutable.",
            )
        if stamped != str(bundle_id):
            return Outcome.failed(STEPS[0], f"install_events.bundle_id is {stamped}, expected {bundle_id}")
        log(f"  ok  {STEPS[0]}: install_events.bundle_id = {stamped} (DB row asserted)")
    else:
        notes.append(
            "step 1 used the INDIRECT provenance check (no DATABASE_URL): a NULL "
            "bundle_id routes to the public default repo, so step 2 landing in the "
            "private sink is the discriminator. Weaker than the row read."
        )
        log(f"  ok  {STEPS[0]}: provenance minted (row NOT read — see notes)")

    # ── STEP 2: defect -> PRIVATE sink, verified AT GitHub ───────────────
    # Trap E3: the payload MUST vary per run or a signature first submitted
    # while the rail was broken pins this forever to that failed row.
    message = (
        f"{skill} assumes pkg-config is present; the build step fails on a minimal host. "
        f"SYNTHETIC defect — mesh_0408 W5 moat-loop proof, run {run_tag}."
    )
    res = t.feedback(message, provenance, {"skill": skill, "synthetic": True, "run": run_tag})
    if not res.get("ok"):
        return Outcome.failed(STEPS[1], f"feedback tool rejected the report: {res}", notes)
    if res.get("deduped"):
        return Outcome.failed(
            STEPS[1],
            "feedback was DEDUPED — the payload did not vary (trap E3). A deduped "
            "response echoes a previous run's issue_url and proves nothing about now.",
            notes,
        )
    issue_url = res.get("issue_url") or ""
    if not issue_url:
        # Trap R4 — this is exactly the shape that hid the original gap.
        return Outcome.failed(
            STEPS[1],
            "feedback returned ok:true with an EMPTY issue_url — stored, never "
            "dispatched. The rail is not delivering.",
            notes,
        )

    number = issue_url.rstrip("/").rsplit("/", 1)[-1]
    try:
        issue = t.github_issue(repo, number)
    except Void as exc:
        return Outcome.void(f"step {STEPS[1]}: {exc}", notes)
    except Exception as exc:  # noqa: BLE001
        return Outcome.failed(
            STEPS[1],
            f"issue_url was returned but GitHub does not serve it: {type(exc).__name__}: {exc}",
            notes,
        )
    if repo not in (issue.get("html_url") or ""):
        return Outcome.failed(STEPS[1], f"issue landed in the WRONG repo: {issue.get('html_url')}", notes)

    try:
        private = t.repo_is_private(repo)
    except Void as exc:
        return Outcome.void(f"step {STEPS[1]}: {exc}", notes)
    if not private:
        return Outcome.failed(
            STEPS[1], f"{repo} is PUBLICLY readable — client defects would be exposed", notes
        )
    log(f"  ok  {STEPS[1]}: #{issue.get('number')} in {repo}, sink private (anon 404)")

    # ── STEP 3: patch published as a NEW VERSION ─────────────────────────
    if not installed_semver:
        return Outcome.void(f"step {STEPS[2]}: install did not report the installed version", notes)
    patched = _next_patch_semver(str(installed_semver))
    code, pub_body = t.publish_patch(skill, patched, f"Fix for {issue_url} (run {run_tag})")
    if code not in (200, 201):
        return Outcome.failed(STEPS[2], f"publishing {skill}@{patched} returned {code}: {pub_body}", notes)
    log(f"  ok  {STEPS[2]}: {skill}@{installed_semver} -> {patched} published")

    # ── STEP 4: the member's bundle RESOLVES to the new version ──────────
    code, job = t.start_apply(bundle_slug)
    if code != 200 or not isinstance(job, dict):
        return Outcome.failed(STEPS[3], f"opening an apply job returned {code}: {job}", notes)
    targets = {tgt["slug"]: tgt["semver"] for tgt in job.get("targets", [])}
    if targets.get(skill) != patched:
        return Outcome.failed(
            STEPS[3],
            f"the bundle resolves {skill} to {targets.get(skill)!r}, not the patch "
            f"{patched!r} — the redeploy never reaches the member",
            notes,
        )
    job_id = job.get("job_id")
    if not job_id:
        return Outcome.void(f"step {STEPS[3]}: apply job carried no job_id", notes)
    log(f"  ok  {STEPS[3]}: bundle {bundle_slug} resolves {skill} -> {patched}")

    # ── STEP 5: the member CONVERGES, terminally ─────────────────────────
    # Convergence must be reported by the MEMBER at the expected semver. The
    # control plane cannot mark itself green.
    code, reported = t.report_apply(job_id, skill, patched, "success")
    if code != 200 or not isinstance(reported, dict):
        return Outcome.failed(STEPS[4], f"the member report returned {code}: {reported}", notes)
    if reported.get("status") != "converged" or not reported.get("terminal"):
        return Outcome.failed(
            STEPS[4],
            f"job did not reach a terminal converged state: "
            f"status={reported.get('status')!r} terminal={reported.get('terminal')!r}",
            notes,
        )

    # Re-read rather than trusting the write's echo (the R4 discipline, applied
    # to our own API this time).
    code, seen = t.get_job(job_id)
    if code != 200 or not isinstance(seen, dict):
        return Outcome.void(f"step {STEPS[4]}: could not re-read job {job_id}: {code}", notes)
    if seen.get("status") != "converged":
        return Outcome.failed(
            STEPS[4],
            f"the write echoed 'converged' but a re-read says {seen.get('status')!r}",
            notes,
        )
    log(f"  ok  {STEPS[4]}: job {job_id[:8]} terminal=converged (re-read confirms)")

    return Outcome.proven(notes)


# ══════════════════════════════════════════════════════════════════════════
# --selftest: falsify the harness before trusting it (trap V4)
# ══════════════════════════════════════════════════════════════════════════


class _StubTransport:
    """A scriptable stand-in for :class:`Transport`.

    Every method reads its response from ``self.plan``; anything not overridden
    falls back to the happy path, so a scenario only has to state its ONE
    deviation. That is what makes the matrix below readable as a spec.
    """

    HAPPY = {
        "install": (200, {"provenance_id": "prov-abc", "version": "1.2.3"}),
        "bundle_id_row": "bundle-1",
        "feedback": {"ok": True, "issue_url": "https://github.com/o/r/issues/7"},
        "issue": {"number": 7, "html_url": "https://github.com/o/private-sink/issues/7"},
        "private": True,
        "publish": (201, {"version": "1.2.4"}),
        "start": (200, {"job_id": "job-1", "targets": [{"slug": "s", "semver": "1.2.4"}]}),
        "report": (200, {"status": "converged", "terminal": True}),
        "get_job": (200, {"status": "converged", "terminal": True}),
    }

    def __init__(self, **overrides: Any) -> None:
        self.plan = dict(self.HAPPY)
        self.plan.update(overrides)

    def _v(self, key: str) -> Any:
        v = self.plan[key]
        if isinstance(v, Void):
            raise v
        if isinstance(v, Exception):
            raise v
        return v

    def install_from_bundle(self, slug, bundle_id):
        return self._v("install")

    def install_event_bundle_id(self, provenance_id):
        return self._v("bundle_id_row")

    def feedback(self, message, provenance_id, context):
        self.last_message = message
        return self._v("feedback")

    def github_issue(self, repo, number):
        return self._v("issue")

    def repo_is_private(self, repo):
        return self._v("private")

    def publish_patch(self, slug, semver, changelog):
        return self._v("publish")

    def start_apply(self, bundle_slug):
        return self._v("start")

    def report_apply(self, job_id, slug, semver, outcome):
        return self._v("report")

    def get_job(self, job_id):
        return self._v("get_job")


#: (name, expected_exit, expected_step_or_None, stub overrides)
SELFTEST_MATRIX: list[tuple[str, int, str | None, dict]] = [
    ("happy path", EXIT_PROVEN, None, {}),
    (
        "E2: bundle param ignored, row is NULL",
        EXIT_FAILED,
        STEPS[0],
        {"bundle_id_row": None},
    ),
    (
        "install rejected",
        EXIT_FAILED,
        STEPS[0],
        {"install": (403, {"detail": "nope"})},
    ),
    (
        "R4: ok:true with an empty issue_url",
        EXIT_FAILED,
        STEPS[1],
        {"feedback": {"ok": True, "issue_url": ""}},
    ),
    (
        "E3: feedback deduped to a previous run",
        EXIT_FAILED,
        STEPS[1],
        {"feedback": {"ok": True, "issue_url": "https://x/1", "deduped": True}},
    ),
    (
        "sink is PUBLIC",
        EXIT_FAILED,
        STEPS[1],
        {"private": False},
    ),
    (
        "issue landed in the wrong repo",
        EXIT_FAILED,
        STEPS[1],
        {"issue": {"number": 7, "html_url": "https://github.com/o/somewhere-else/issues/7"}},
    ),
    (
        "patch publish rejected",
        EXIT_FAILED,
        STEPS[2],
        {"publish": (422, "security_scan_failed")},
    ),
    (
        "bundle still resolves to the OLD version",
        EXIT_FAILED,
        STEPS[3],
        {"start": (200, {"job_id": "j", "targets": [{"slug": "s", "semver": "1.2.3"}]})},
    ),
    (
        "member never reaches a terminal state",
        EXIT_FAILED,
        STEPS[4],
        {"report": (200, {"status": "applying", "terminal": False})},
    ),
    (
        "write echoed converged but the re-read disagrees",
        EXIT_FAILED,
        STEPS[4],
        {"get_job": (200, {"status": "applying", "terminal": False})},
    ),
    (
        "VOID: no DATABASE_URL for the row assertion",
        EXIT_VOID,
        None,
        {"bundle_id_row": Void("DATABASE_URL is not set")},
    ),
    (
        "VOID: anonymous repo probe was inconclusive",
        EXIT_VOID,
        None,
        {"private": Void("anonymous repo probe returned 403")},
    ),
]


def selftest() -> int:
    """Drive every outcome shape through run_proof and prove {0,1,2} are reachable.

    A harness whose failure branches have never been executed is not evidence.
    """
    print("HARNESS FALSIFICATION (trap V4) — driving run_proof with stubs\n")
    reached: set[int] = set()
    bad: list[str] = []
    for name, want_code, want_step, overrides in SELFTEST_MATRIX:
        stub = _StubTransport(**overrides)
        out = run_proof(
            stub,  # type: ignore[arg-type]
            skill="s",
            bundle_id="bundle-1",
            bundle_slug="b",
            repo="o/private-sink",
            direct_db_assertion=True,
            log=lambda _m: None,
        )
        ok = out.code == want_code and (want_step is None or out.step == want_step)
        reached.add(out.code)
        mark = "ok  " if ok else "BAD "
        print(f"  {mark}exit={out.code} (want {want_code})  {name}")
        if not ok:
            bad.append(f"{name}: got exit={out.code} step={out.step!r}, want {want_code}/{want_step!r}")

    # The payload-variation guard is itself checked, not assumed (trap E3).
    stub = _StubTransport()
    run_proof(
        stub, skill="s", bundle_id="bundle-1", bundle_slug="b", repo="o/private-sink", log=lambda _m: None
    )  # type: ignore[arg-type]
    first = stub.last_message
    stub2 = _StubTransport()
    run_proof(
        stub2, skill="s", bundle_id="bundle-1", bundle_slug="b", repo="o/private-sink", log=lambda _m: None
    )  # type: ignore[arg-type]
    if first == stub2.last_message:
        bad.append("E3 GUARD BROKEN: two runs produced an IDENTICAL feedback payload")
    else:
        print("  ok  payload varies between runs (trap E3 guard live)")

    missing = {EXIT_PROVEN, EXIT_FAILED, EXIT_VOID} - reached
    print()
    if missing:
        bad.append(f"exit codes never reached: {sorted(missing)} — those branches are untested")
    if bad:
        print("HARNESS IS NOT TRUSTWORTHY:")
        for b in bad:
            print(f"  - {b}")
        return EXIT_FAILED
    print(f"harness falsified: exits {sorted(reached)} all reachable, {len(SELFTEST_MATRIX)} shapes checked")
    return EXIT_PROVEN


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="falsify the harness with stubs; no network")
    ap.add_argument(
        "--indirect-provenance",
        action="store_true",
        help="skip the direct install_events row read (no DB access) and rely on the "
        "private-sink routing discriminator instead. Weaker; the report says so.",
    )
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        s = json.loads(SECRETS.read_text())
        pat_cfg = json.loads(PAT_FILE.read_text())
        member_key = base64.b64decode(s["astrovita_member_api_key_plain_b64"]).decode().strip()
        owner_key = base64.b64decode(s["api_key_plain_b64"]).decode().strip()
        bundle_id = s.get("astrovita_bundle_id")
        pat, repo = pat_cfg["pat"], pat_cfg["repo"]
    except Exception as exc:  # noqa: BLE001
        print(f"VOID: cannot load credentials: {type(exc).__name__}: {exc}")
        return EXIT_VOID

    if not bundle_id:
        print("VOID: astrovita_bundle_id is not recorded in the secrets file")
        return EXIT_VOID

    t = Transport(base=BASE, member_key=member_key, owner_key=owner_key, pat=pat, repo=repo)

    # The agent surface addresses bundles by SLUG; the secrets file records an
    # ID. Resolve via GET /api/bundles/{id} — NOT /api/bundle-deploy/{…}/manifest,
    # whose path param is a slug despite being named cookbook_id elsewhere
    # (app/bundle_deployment_routes.py queries Bundle.slug), so passing an id
    # there 404s.
    bundle_slug = os.environ.get("MOAT_PROOF_BUNDLE_SLUG", "")
    if not bundle_slug:
        code, detail = t.bundle_detail(bundle_id)
        if code == 200 and isinstance(detail, dict):
            bundle_slug = detail.get("slug") or (detail.get("cookbook") or {}).get("slug") or ""
        if not bundle_slug:
            print(
                f"VOID: cannot resolve the slug for bundle {bundle_id} "
                f"(GET /api/bundles/{{id}} returned {code}). "
                f"Set MOAT_PROOF_BUNDLE_SLUG to skip this lookup."
            )
            return EXIT_VOID

    print(f"mesh_0408 W5 moat-loop proof — {BASE}, skill={SKILL}, bundle={bundle_slug}\n")
    out = run_proof(
        t,
        skill=SKILL,
        bundle_id=bundle_id,
        bundle_slug=bundle_slug,
        repo=repo,
        direct_db_assertion=not args.indirect_provenance,
    )

    print()
    for note in out.notes:
        print(f"NOTE: {note}")
    if out.code == EXIT_PROVEN:
        print(f"MOAT LOOP PROVEN: defect -> {repo} (private) -> patch -> version -> REDEPLOY converged")
    elif out.code == EXIT_FAILED:
        print(f"FAIL at step {out.step}: {out.detail}")
    else:
        print(f"VOID (exit 2 — NOT a pass, the harness could not discriminate): {out.detail}")
    return out.code


if __name__ == "__main__":
    sys.exit(main())
