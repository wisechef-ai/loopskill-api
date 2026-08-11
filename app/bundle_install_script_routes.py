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

Dual-mounted at /api/bundles/install.sh (primary) and
/api/cookbooks/install.sh (compat-alias) — mirrors every other bundle
surface in this repo. Public (no auth) — same allowlist reasoning as /skill:
a visitor must be able to fetch the installer BEFORE they have a key.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["bundles"])

# Templated with {api_base} at serve time so a self-hosted deploy's script
# points at itself, never a hardcoded app.loopskill.io. Kept as a single
# static asset (not per-request generation) so it's trivially diffable and
# a human/agent can `curl` it to read exactly what it will run before piping
# it to bash — no surprises, no dynamic per-caller content.
_INSTALL_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# LoopSkill bundle installer — auth-free for every free-tier skill in the
# bundle. Paid-tier members are reported by name, never silently dropped.
#
# Usage:
#   curl -fsSL {api_base}/api/bundles/install.sh | bash -s -- <bundle-slug>
#
# Env overrides:
#   LOOPSKILL_API_BASE    — API origin (default: {api_base})
#   LOOPSKILL_INSTALL_DIR — install target (default: ~/.claude/skills)
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

echo "LoopSkill: installing bundle '$SLUG' -> $DEST"
mkdir -p "$DEST"

PYTMP="$(mktemp)"
trap 'rm -f "$PYTMP"' EXIT
cat > "$PYTMP" <<'PYEOF'
import json, os, sys, urllib.request

api_base, slug, dest, index_url = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
base = f"{{api_base}}/api/bundles/public/{{slug}}/.well-known/skills"
with urllib.request.urlopen(index_url) as r:
    data = json.load(r)
skills = data.get("skills", [])
installed, locked = [], []
for s in skills:
    name = s.get("name")
    if not name:
        continue
    if s.get("locked"):
        locked.append(name)
        continue
    # bundles_0811: `name` is the WIRE KEY used to fetch; `dir_name` is the
    # filesystem-safe form to mkdir. Federated members are slugged
    # `ext:<source>:<slug>` and a colon is illegal in a Windows path, so
    # writing `name` verbatim produced an unusable directory for 114 of 172
    # public-bundle members. Falls back to `name` for an older API build.
    dir_name = s.get("dir_name") or name
    outdir = os.path.join(dest, dir_name)
    os.makedirs(outdir, exist_ok=True)
    urllib.request.urlretrieve(f"{{base}}/{{name}}/SKILL.md", os.path.join(outdir, "SKILL.md"))
    installed.append(name)

print(f"installed {{len(installed)}} skill(s) to {{dest}}")
if locked:
    print(f"{{len(locked)}} skill(s) require a LoopSkill key (pro tier): {{', '.join(locked)}}")
    print(f"Get one free at {{api_base}}/pricing, then run:")
    print(f'  loopskill_bundle_install from "bundle://{{slug}}"  (via MCP)')
PYEOF

python3 "$PYTMP" "$API_BASE" "$SLUG" "$DEST" "$INDEX_URL"
"""


def render_install_script(api_base: str) -> str:
    """Render the installer with the caller's origin baked in.

    Extracted as a pure function so a test can assert the rendered script's
    exact shape without spinning up the ASGI app.
    """
    return _INSTALL_SCRIPT_TEMPLATE.format(api_base=api_base.rstrip("/"))


def _serve_install_script() -> PlainTextResponse:
    from app import config

    body = render_install_script(config.public_origin())
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


_h = APIRouter()


@_h.get("/install.sh", include_in_schema=False)
def bundle_install_script() -> PlainTextResponse:
    """Serve the auth-free bundle installer script. No auth (see module docstring)."""
    return _serve_install_script()


# Dual-mount: /api/bundles is primary, /api/cookbooks is the backward-compat
# alias every other bundle surface in this repo carries.
router.include_router(_h, prefix="/api/bundles")
router.include_router(_h, prefix="/api/cookbooks")  # compat-alias
