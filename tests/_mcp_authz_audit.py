"""P0 (converge_0208) — strengthened MCP tool authz audit logic.

Extracted from ``tests/test_secfix_1905_b_mcp_authz.py::TestAuditPass`` so the
audit RULE can be unit-tested directly (RED-proofed against synthetic
"broken" tool sources) as well as run against the real files on disk.

Why this exists: the ORIGINAL audit accepted the literal comment
``# Public-scope MCP tool:`` as sufficient on its own, with no check that the
claim was true. ``app/mcp/tools/list_cookbook.py`` shipped with that exact
comment ("caller's own bundle; list_cookbook filters by caller's user_id from
auth context") while returning ANY bundle by raw UUID with zero filtering —
the audit passed on a tool with no authz. See the P0 task spec's "gate-gaming
pattern" note.

The strengthened rule: a tool file that queries an OWNER_COLUMN_MODEL (a
model with a per-user/tenant ownership column — Bundle, BundleSkill, Fleet,
...) may NOT rely on the marker comment alone. It must have either:
  (a) a real ``authz.can_*(`` call (not just the substring inside a comment
      — the naive substring check has already been fooled once, by
      app/mcp/tools/configure_feedback.py's docstring literally containing
      the words "no authz.can_* used"), or
  (b) an explicit entry in ``INLINE_OWNERSHIP_ALLOWLIST`` documenting the
      audited-safe inline ownership check it uses instead.

Files that never touch an owner-column model at all (genuinely public
catalog data, or write-only submissions that never read another caller's
row back) keep the original, simpler bar: comment OR real call.
"""

from __future__ import annotations

import re

# Models with a per-user/tenant ownership column. A tool file that queries
# one of these via ``db.query(<Model>`` is returning (or could return)
# private, owner-scoped rows — the marker comment alone is not enough.
OWNER_COLUMN_MODELS = frozenset(
    {
        "Bundle",
        "BundleSkill",
        "Fleet",
        "FleetMember",
        "FleetMemberLiveness",
        "FleetSubscription",
        "SkillFork",
        "ForkVersion",
        "APIKey",
        "CookbookShareToken",
        "ShareToken",
        "Personality",
        "CompositeLoop",
        "Loop",
        "Verifier",
        "LoopPlacement",
    }
)

# Tool modules manually audited (P0/converge_0208) to return ONLY genuinely
# public data, or to write without ever reading another caller's private row
# back — the marker comment is trusted at face value for these ONLY.
PUBLIC_ONLY_ALLOWLIST = frozenset(
    {
        "carousel_today.py",
        "doctor.py",
        "feedback.py",  # writes own FeedbackSubmission; provenance routing is server-derived, not caller-suppliable
        "publish_request.py",
        "recall.py",
        "recipify_request.py",
        "search.py",
        "seeker.py",
        "skill_error.py",
        "skill_patch.py",  # dedup hit shares a PR url by design across any caller with the same patch
        "subrecipe_resolve.py",
    }
)

# Tool modules that gate access WITHOUT a literal ``authz.can_*(`` call in
# THIS file — an inline ctx-equality check, a hard public-only filter before
# the row is ever returned, or delegation to another module's own real gate.
# Audited safe (P0/converge_0208) and named explicitly, with the reasoning,
# so a FUTURE change to one of these files (or a new file copying the
# pattern) isn't assumed safe without the same scrutiny — this is a bypass,
# not a rubber stamp, so keep entries narrow and specific.
INLINE_OWNERSHIP_ALLOWLIST = {
    "tailor.py": (
        "SkillFork rows are filtered by ctx.user_id equality "
        "(SkillFork.user_id == ctx.user_id); no authz.can_* predicate "
        "exists yet for fork ownership."
    ),
    "configure_feedback.py": (
        "Inline ownership gate: 'if cb.bundle_owner != user_uuid: return "
        "forbidden' before any bundle field is read or written."
    ),
    "bundle_stream.py": (
        "_resolve_public_cookbook hard-filters visibility == 'public' "
        "before ever returning a Bundle row (never leaks a private one "
        "regardless of caller); the compose-from-links write path stamps "
        "bundle_owner=ctx.user_id on the caller's own new bundle."
    ),
    "like.py": (
        "Delegates to app.library_service.set_liked_artifact(owner_id="
        "ctx.user_id, ...), which is the shared REST+MCP chokepoint and "
        "itself calls authz.can_read_skill before allowing a like."
    ),
}

_AUTHZ_CALL_RE = re.compile(r"authz\.can_\w+\(")
_QUERY_RE_TEMPLATE = r"db\.query\(\s*{model}\b"


def has_real_authz_call(source: str) -> bool:
    """True if ``authz.can_*(`` appears as an actual call, not just inside a comment."""
    for line in source.splitlines():
        code_part = line.split("#", 1)[0]  # drop trailing/whole-line comments
        if _AUTHZ_CALL_RE.search(code_part):
            return True
    return False


def referenced_owner_model(source: str) -> str | None:
    """Return the first OWNER_COLUMN_MODEL this source queries via db.query(...), or None."""
    for model in OWNER_COLUMN_MODELS:
        if re.search(_QUERY_RE_TEMPLATE.format(model=model), source):
            return model
    return None


def audit_tool_file(fname: str, source: str) -> str | None:
    """Return a failure reason, or None if ``fname``/``source`` passes the audit.

    ``fname`` is the bare filename (e.g. ``"list_cookbook.py"``); ``source``
    is the full file text.
    """
    if fname in INLINE_OWNERSHIP_ALLOWLIST:
        return None

    has_authz_call = has_real_authz_call(source)
    has_public_comment = "# Public-scope MCP tool:" in source

    if not has_authz_call and not has_public_comment:
        return "missing authz.can_* call AND missing '# Public-scope MCP tool:' comment"

    if has_authz_call:
        return None  # a real gate is always sufficient, comment or not

    # Only the comment is present. Strengthened check: is that actually enough?
    if fname in PUBLIC_ONLY_ALLOWLIST:
        return None

    owner_model = referenced_owner_model(source)
    if owner_model is None:
        return None  # no owner-scoped model touched; the comment is plausible

    return (
        f"queries {owner_model} (an owner-column model) and relies ONLY on "
        f"the '# Public-scope MCP tool:' comment — no real authz.can_* call, "
        f"and not in PUBLIC_ONLY_ALLOWLIST or INLINE_OWNERSHIP_ALLOWLIST. "
        f"The comment alone is not sufficient for tools returning "
        f"user-scoped rows."
    )
