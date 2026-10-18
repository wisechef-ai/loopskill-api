"""bundles0811 P1 (F1/F2) — the auth-free copy-pasteable bundle installer.

The cold-path trace (2026-08-11) found the ONLY "take it" affordance on a
public bundle page was ``clone_line`` — an MCP call
(``loopskill_bundle_install from "bundle://..."``) that 401s for every
anonymous caller because ``POST /api/mcp/http/`` requires an API key,
keyed or not. Meanwhile the anonymous path that genuinely works
(``GET /api/skills/install?slug=``, and its bulk form
``GET /api/bundles/public/{slug}/.well-known/skills/index.json`` — see
``bundle_wellknown_routes.py``) was never surfaced as a paste-and-run line.

This module serves ONE static shell script, mirroring the precedent in
``skill_serve_routes.py`` (``GET /skill`` serves the canonical SKILL.md as
plain text, no redirect, no auth). The script walks the SAME well-known
index the portal's public bundle page already calls, downloading every
FREE-tier skill's SKILL.md with zero credentials. Locked (paid-tier) members
are reported by name with a pointer to the authenticated MCP line — never
silently skipped without explanation, and never blocked from installing the
free members alongside them.

Verified end-to-end 2026-08-11 (pasted in the PR body):
    curl -fsSL https://app.loopskill.io/api/bundles/install.sh | bash -s -- loopskill-essentials
  -> installed 53 skill(s) to ~/.claude/skills (all free-tier, zero auth)
    curl -fsSL https://app.loopskill.io/api/bundles/install.sh | bash -s -- dev-agent-essentials
  -> installed 0 skill(s); reports the 3 pro-tier members + the MCP line to
     unlock them (honest failure, not a silent no-op)

fi_first_impression_api (2026-08-19): a live audit found the "reports the
pro-tier members" claim above was true only for a MIXED bundle. An ALL-Pro
bundle (dev-agent-essentials, research-and-report — every member locked)
printed a bare "installed 0 skill(s)" with the locked-members footer, which
reads exactly like the script ran correctly and the bundle is just empty —
no visitor would infer "this bundle is entirely paid" from that line. Fixed:
when the free-installable count is 0, the warning
"This bundle requires Pro — 0 free skills will be installed. See
https://app.loopskill.io/pricing" is now the FIRST line printed (before the
"installing bundle ..." progress line, before mkdir), and the script exits
4 (distinct from the pre-existing usage/missing-python3 codes 2/3) so an
automated caller can branch on "this bundle needs a key" without parsing
stdout.

qa1018 C18 (2026-10-18): persona QA found ``seo-and-search-growth``
"installed 8 skill(s)" when 3 of the 8 files were the locked stub served on a
failed origin fetch and 1 was an upstream "has moved" signpost, with no
checksum check at all. The script now decides per member from the server's
``X-LoopSkill-Installable`` / ``X-LoopSkill-Skip-Reason`` headers (falling back
to the stub's own text for an older server), verifies ``sha256`` (the index
value when present, else the ``X-LoopSkill-SHA256`` header), writes a file
only after it passes, retries 429/5xx with backoff, and ends with
"installed N skill(s) ...; skipped M" plus one line per skipped member and its
reason. Exit 5 = members were requested but none installed (exit 4, the
all-paid case, is unchanged; an empty bundle exits 0).

Dual-mounted at /api/bundles/install.sh (primary) and
/api/cookbooks/install.sh (compat-alias) — mirrors every other bundle
surface in this repo. Public (no auth) — same allowlist reasoning as /skill:
a visitor must be able to fetch the installer BEFORE they have a key.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["bundles"])

# Templated with {api_base} at serve time so a self-hosted deploy's script
# points at itself, never a hardcoded app.loopskill.io. Kept as a single
# static asset (not per-request generation) so it's trivially diffable and
# a human/agent can `curl` it to read exactly what it will run before piping
# it to bash — no surprises, no dynamic per-caller content.
_INSTALL_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# LoopSkill bundle installer — auth-free for every free-tier skill in the
# bundle. Members that are not installable (paid-tier, origin unavailable,
# moved upstream, failed checksum) are reported by name, never silently
# dropped and never written to disk.
#
# Usage:
#   curl -fsSL {api_base}/api/bundles/install.sh | bash -s -- <bundle-slug>
#
# Env overrides:
#   LOOPSKILL_API_BASE    — API origin (default: {api_base})
#   LOOPSKILL_INSTALL_DIR — install target (default: ~/.claude/skills)
#   LOOPSKILL_RETRY_SLEEP — base seconds between retries on 429/5xx (default 15)
#
# Exit codes: 0 = at least one skill installed (or the bundle is empty);
# 2 = usage; 3 = python3 missing; 4 = every member is paid-tier;
# 5 = members were requested but none could be installed; 6 = index fetch failed.
set -euo pipefail

SLUG="${{1:-${{LOOPSKILL_BUNDLE:-}}}}"
if [ -z "$SLUG" ]; then
  echo "usage: install.sh <bundle-slug>  (or set LOOPSKILL_BUNDLE)" >&2
  exit 2
fi

API_BASE="${{LOOPSKILL_API_BASE:-{api_base}}}"
DEST="${{LOOPSKILL_INSTALL_DIR:-$HOME/.claude/skills}}"
INDEX_URL="$API_BASE/api/bundles/public/$SLUG/.well-known/skills/index.json"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (or install manually: $INDEX_URL)" >&2
  exit 3
fi

PYTMP="$(mktemp)"
trap 'rm -f "$PYTMP"' EXIT
cat > "$PYTMP" <<'PYEOF'
import hashlib, json, os, sys, time, urllib.error, urllib.request

api_base, slug, dest, index_url = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
base = f"{{api_base}}/api/bundles/public/{{slug}}/.well-known/skills"
retry_base = float(os.environ.get("LOOPSKILL_RETRY_SLEEP", "15"))
# Text the server puts in a placeholder SKILL.md (locked stub / origin down).
# Fallback only: a current server labels every response via X-LoopSkill-* headers.
STUB_MARKERS = (
    ("are not served over the public bundle surface", "locked"),
    ("This file is a placeholder, not the skill.", "unavailable"),
)


def fetch(url):
    # GET with retry on 429/5xx (honours Retry-After). Returns (body, headers).
    delays = [retry_base, retry_base * 2, retry_base * 4]
    while True:
        try:
            req = urllib.request.Request(url, headers={{"User-Agent": "loopskill-install.sh"}})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or not delays:
                raise
            wait = delays.pop(0)
            ra = (e.headers or {{}}).get("Retry-After", "")
            if ra.isdigit():
                wait = max(wait, float(ra))
            time.sleep(wait)


try:
    raw, _ = fetch(index_url)
    data = json.loads(raw)
except Exception as e:  # noqa: BLE001
    print(f"could not fetch the bundle index for '{{slug}}' ({{e}}): {{index_url}}", file=sys.stderr)
    sys.exit(6)

skills = [s for s in data.get("skills", []) if s.get("name")]
free = [s for s in skills if not s.get("locked") and s.get("skip_reason") != "locked"]
locked = [s.get("name") for s in skills if s.get("locked") or s.get("skip_reason") == "locked"]

# fi_first_impression_api: an audit against dev-agent-essentials and
# research-and-report (2026-08-19) found this script silently printed
# "installed 0 skill(s)" when EVERY member of a bundle is Pro-locked —
# a cold-path visitor pasting the install line got a bare success-shaped
# summary with zero indication anything was withheld. The check now runs
# FIRST, before any progress output, so the warning is unmissable and the
# script exits with a distinct code (4) an automated caller can branch on.
if skills and not free:
    print("This bundle requires Pro — 0 free skills will be installed. See https://app.loopskill.io/pricing")
    sys.exit(4)

if not skills:
    print(f"LoopSkill: bundle '{{slug}}' has no skills to install.")
    sys.exit(0)

print(f"LoopSkill: installing bundle '{{slug}}' -> {{dest}}")

installed = []
skipped = [(name, "locked") for name in locked]
for s in free:
    name = s["name"]
    # bundles_0811: `name` is the WIRE KEY used to fetch; `dir_name` is the
    # filesystem-safe form to mkdir. Federated members are slugged
    # `ext:<source>:<slug>` and a colon is illegal in a Windows path, so
    # writing `name` verbatim produced an unusable directory for 114 of 172
    # public-bundle members. Falls back to `name` for an older API build.
    dir_name = s.get("dir_name") or name
    try:
        body, headers = fetch(f"{{base}}/{{name}}/SKILL.md")
    except Exception as e:  # noqa: BLE001
        skipped.append((name, f"download failed ({{e}})"))
        continue
    # qa1018 C18: the server labels what it served. A placeholder (locked
    # stub, origin unavailable, upstream "has moved" signpost) is reported,
    # never written to disk as if it were the skill.
    reason = None
    if (headers.get("X-LoopSkill-Installable") or "").lower() == "false":
        reason = headers.get("X-LoopSkill-Skip-Reason") or "unavailable"
    else:
        head = body[:4096].decode("utf-8", "replace")
        reason = next((r for marker, r in STUB_MARKERS if marker in head), None)
    if reason is None:
        digest = hashlib.sha256(body).hexdigest()
        want = s.get("sha256") or headers.get("X-LoopSkill-SHA256")
        if want and want.lower() != digest:
            reason = "checksum mismatch"
    if reason is not None:
        skipped.append((name, reason))
        continue
    outdir = os.path.join(dest, dir_name)
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, ".SKILL.md.part")
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, os.path.join(outdir, "SKILL.md"))
    installed.append(name)

print(f"installed {{len(installed)}} skill(s) to {{dest}}; skipped {{len(skipped)}}")
for name, reason in skipped:
    print(f"  skipped: {{name}} — {{reason}}")
if locked:
    print(f"{{len(locked)}} skill(s) require a LoopSkill key (pro tier): {{', '.join(locked)}}")
    print(f"Get one free at {{api_base}}/pricing, then run:")
    print(f'  loopskill_bundle_install from "bundle://{{slug}}"  (via MCP)')
if not installed:
    sys.exit(5)
PYEOF

python3 "$PYTMP" "$API_BASE" "$SLUG" "$DEST" "$INDEX_URL"
"""


def render_install_script(api_base: str) -> str:
    """Render the installer with the caller's origin baked in.

    Extracted as a pure function so a test can assert the rendered script's
    exact shape without spinning up the ASGI app.
    """
    return _INSTALL_SCRIPT_TEMPLATE.format(api_base=api_base.rstrip("/"))


def _serve_install_script(request: Request) -> PlainTextResponse:
    from app import config

    # bhint-tel0824 (t_55a1a333): the install.sh pull IS the conversion event
    # for the bundle fast-path hint — the hint's install_all line is the only
    # surface that prints this URL verbatim. Server-side attribution only:
    # a converted_pull telemetry row is written iff this exact client_ip was
    # shown a hint for this slug within 7d (see bundle_hint_telemetry).
    # Anonymous organic pulls by never-hinted visitors record NOTHING.
    # Fail-quiet: a telemetry hiccup must never break serving the script.
    # Rationale: observability-only; the script itself has no auth to fail.
    try:
        from app.database import SessionLocal
        from app.services.bundle_hint_telemetry import maybe_record_hint_conversion

        slug = request.query_params.get("slug") or request.headers.get("x-loopskill-bundle-hint-slug")
        if slug:
            from app.utils.client_ip import _real_client_ip

            db = SessionLocal()
            try:
                maybe_record_hint_conversion(
                    db,
                    client_ip=_real_client_ip(request, config.settings.TRUSTED_PROXY_CIDRS),
                    bundle_slug=slug,
                )
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        pass

    body = render_install_script(config.public_origin())
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


_h = APIRouter()


@_h.get("/install.sh", include_in_schema=False)
def bundle_install_script(request: Request) -> PlainTextResponse:
    """Serve the auth-free bundle installer script. No auth (see module docstring)."""
    return _serve_install_script(request)


# Dual-mount: /api/bundles is primary, /api/cookbooks is the backward-compat
# alias every other bundle surface in this repo carries.
router.include_router(_h, prefix="/api/bundles")
router.include_router(_h, prefix="/api/cookbooks")  # compat-alias
