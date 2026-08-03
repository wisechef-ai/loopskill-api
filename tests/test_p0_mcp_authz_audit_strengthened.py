"""P0 (converge_0208) — RED-proof for the strengthened MCP authz audit.

The audit that shipped before this phase accepted the literal comment
``# Public-scope MCP tool:`` as sufficient proof of safety, with no check
that the claim was actually true. ``app/mcp/tools/list_cookbook.py`` carried
that exact comment ("caller's own bundle; list_cookbook filters by caller's
user_id from auth context") while returning ANY bundle by raw UUID with zero
filtering when ``cookbook_id`` was supplied — the audit passed on a tool with
no authz at all (see test_p0_mcp_tenant_leak.py for the exploit).

These tests drive ``tests._mcp_authz_audit.audit_tool_file`` directly against
SYNTHETIC tool sources (not real repo files) to prove the audit rule itself
catches the exact gate-gaming shape that shipped, and does not regress to
accepting it again. This is the "temporarily break a tool and confirm the
audit catches it" proof the P0 task spec asks for, without mutating real
files on disk during a test run.
"""

from __future__ import annotations

from tests._mcp_authz_audit import audit_tool_file

_LEAKY_LIST_COOKBOOK_SHAPE = '''
"""loopskill_list_bundle — list a caller's cookbook."""

from app.models import Bundle


def loopskill_list_bundle(db, cookbook_id=None):
    # Public-scope MCP tool: caller's own bundle; list_cookbook filters by caller's user_id from auth context.
    if cookbook_id:
        return db.query(Bundle).filter(Bundle.id == cookbook_id).first()
    return None
'''

_GENUINELY_PUBLIC_SHAPE = '''
"""loopskill_search_widgets — search the public widget catalog."""

from app.models import Skill


def loopskill_search_widgets(db, query=None):
    # Public-scope MCP tool: public catalog only; is_public filter applied internally.
    return db.query(Skill).filter(Skill.is_public.is_(True)).all()
'''

_REAL_GATE_SHAPE = '''
"""loopskill_read_bundle — read a bundle the caller owns."""

from app import authz
from app.models import Bundle


def loopskill_read_bundle(db, ctx, bundle_id):
    cb = db.query(Bundle).filter(Bundle.id == bundle_id).first()
    if cb is None or not authz.can_read_cookbook(ctx, cb, allow_org_read=True):
        return {"error": "not_found"}
    return cb
'''

_FALSE_COMMENT_TEXT_SHAPE = '''
"""loopskill_configure_something — configure a bundle setting."""

from app.models import Bundle


def loopskill_configure_something(db, cookbook_id, value):
    # Public-scope MCP tool: bundle setting configuration.
    # (inline tier check); no authz.can_* used because the relevant check is elsewhere
    cb = db.query(Bundle).filter(Bundle.id == cookbook_id).first()
    cb.value = value
    db.commit()
    return {"ok": True}
'''


class TestAuditCatchesTheShippedGateGamingPattern:
    """RED-proof: feed the audit rule the EXACT shape of the bug that shipped."""

    def test_red_catches_list_cookbook_shaped_leak(self):
        """The precise shape that shipped: marker comment + Bundle query + no
        real gate. The audit MUST flag this — this is the bug we just fixed."""
        reason = audit_tool_file("hypothetical_leaky_tool.py", _LEAKY_LIST_COOKBOOK_SHAPE)
        assert reason is not None, (
            "AUDIT REGRESSION: a tool with the list_cookbook.py-shaped bug "
            "(marker comment + Bundle query + zero real gate) was NOT flagged. "
            "This is exactly the gate-gaming pattern that shipped the P0 leak."
        )
        assert "Bundle" in reason

    def test_red_catches_comment_text_that_only_mentions_authz(self):
        """The naive substring check ('authz.can_' in source) is fooled by a
        comment merely mentioning authz.can_* in prose (this is the EXACT
        shape of app/mcp/tools/configure_feedback.py's docstring, which is
        legitimately safe there only because of an inline gate the synthetic
        shape below deliberately omits). The strengthened audit must not be
        fooled by prose alone."""
        reason = audit_tool_file("hypothetical_configure_tool.py", _FALSE_COMMENT_TEXT_SHAPE)
        assert reason is not None, (
            "AUDIT REGRESSION: a tool whose only 'authz' mention is prose "
            "inside a comment (no real call, no real inline gate, no "
            "allowlist entry) was NOT flagged."
        )

    def test_genuinely_public_tool_still_passes(self):
        """Sanity: a tool that never touches an owner-column model keeps the
        original, simpler bar (comment OR call)."""
        reason = audit_tool_file("hypothetical_public_tool.py", _GENUINELY_PUBLIC_SHAPE)
        assert reason is None, f"False positive on genuinely public tool: {reason!r}"

    def test_tool_with_real_authz_call_passes(self):
        """Sanity: a tool with a real authz.can_*( call always passes,
        comment or not."""
        reason = audit_tool_file("hypothetical_gated_tool.py", _REAL_GATE_SHAPE)
        assert reason is None, f"False positive on a genuinely gated tool: {reason!r}"

    def test_tool_with_neither_call_nor_comment_still_fails(self):
        """Sanity: the original bar (comment OR call) still applies for
        tools that don't touch an owner-column model."""
        source = '"""some tool with no gate and no comment."""\ndef f(db):\n    return 1\n'
        reason = audit_tool_file("hypothetical_bare_tool.py", source)
        assert reason is not None
        assert "missing authz.can_* call AND missing" in reason
