"""SSRF + dangerous-command guard for staged external connector candidates.

mesh_0408 T1-C. Every candidate pulled from an open MCP catalog by
``connector_taps.py`` is validated by this module BEFORE it is staged into
``ExternalConnector``. A candidate that fails is DROPPED, never inserted —
this is a pre-staging filter, not a per-row label, so a malicious row is
never even in the table for a later review step to accidentally promote.

REUSE, NOT REBUILD (T1-C deletion opener, plan §0.1 discipline): URL/host
safety does not reinvent IP-literal parsing. ``app.services.federation_fetch
.is_safe_url`` already resolves the hostname via ``socket.getaddrinfo`` and
checks every returned address against the cloud-metadata/private/link-local
block policy. That resolution step is what defeats BOTH classes this phase
must cover, for the same reason:

  * **Alternative IP encodings** (hex ``0xA9FEA9FE``, per-octet octal
    ``0251.0376.0251.0376``, single 32-bit decimal ``2852039166``, mixed
    ``169.0xFE.0251.254``) — a string blocklist for the literal
    ``"169.254.169.254"`` never sees these; the OS resolver's own
    ``inet_aton``-style parser normalizes ALL of them to the canonical IP,
    and ``getaddrinfo`` returns that canonical form, so the same block-list
    check running on the *resolved* address (not the raw host string) catches
    every encoding for free. Verified live in this module's test suite by
    calling ``socket.getaddrinfo`` on each encoding directly.
  * **DNS rebinding** — ``is_safe_url`` performs a FRESH resolution on every
    call; nothing is cached or memoized. A rebind attack relies on the
    validator trusting a stale/cached "safe" verdict while the actual use
    resolves differently. This guard never caches, so every validation call
    is an independent, current resolution — there is no cached verdict to
    exploit. (For an actual network *fetch*, ``federation_fetch.guarded_get``
    additionally re-validates every redirect hop — this module only needs
    the resolution-freshness property, since staged rows are never fetched
    or connected to automatically.)

Command safety is new here — ``federation_fetch`` only guards URLs. A
``stdio`` connector's ``command`` field is attacker-shaped free text from an
untrusted catalog; ``is_dangerous_command`` rejects the shapes an MCP-server
manifest has no legitimate reason to carry (destructive filesystem ops,
fork-bombs, pipe-to-shell downloads, raw shutdown/reboot).
"""

from __future__ import annotations

import re
from typing import Any

from app.services.federation_fetch import is_safe_url

# ── Dangerous command patterns ──────────────────────────────────────────────
# A legitimate MCP stdio server command is a runtime + package/script, e.g.
# "npx -y @scope/pkg", "uvx some-server", "docker run ...", "python -m pkg".
# None of that legitimately needs recursive deletion, fork bombs, raw device
# writes, or curl|bash-style remote execution. Deny-list is intentionally
# narrow and pattern-based (not a full shell-injection parser — out of scope
# for a staging filter whose rows can never install without promotion).
_DANGEROUS_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)\s"),  # rm -rf / rm -fr
    re.compile(r"\brm\s+.*\s/(\s|$)"),  # rm ... / (root-targeted delete)
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # classic fork bomb
    re.compile(r"\bmkfs\b|\bdd\s+if="),  # filesystem format / raw disk write
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),  # host power state
    re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)\b"),  # pipe-to-shell RCE
    re.compile(r">\s*/dev/(sd|nvme|hd)"),  # raw block device write
    re.compile(r"\bchmod\s+-R\s+777\s+/(\s|$)"),  # root world-writable
)


def is_dangerous_command(command: Any) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)`` for a candidate stdio ``command`` field.

    Accepts either a string ("rm -rf /") or a list (["rm", "-rf", "/"]) —
    MCP manifests use both shapes across sources. Non-string/list input is
    treated as safe-by-absence (nothing to flag); the structural validator
    (``connector_validation.py``) is responsible for shape errors.
    """
    if isinstance(command, list):
        text = " ".join(str(c) for c in command)
    elif isinstance(command, str):
        text = command
    else:
        return False, None
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(text):
            return True, f"ssrf_guard.dangerous_command: matches {pattern.pattern!r}"
    return False, None


def is_unsafe_url(url: Any) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)`` for a candidate URL field.

    Delegates to ``federation_fetch.is_safe_url`` (fresh DNS resolution every
    call, resolved-address block-list check — see module docstring for why
    that construction covers both alternative IP encodings and DNS rebinding
    without a separate implementation). Non-string input is safe-by-absence.
    """
    if not isinstance(url, str) or not url:
        return False, None
    if not is_safe_url(url):
        return True, f"ssrf_guard.unsafe_url: {url!r} resolves to a blocked address or scheme"
    return False, None


def validate_candidate_config(config_template: dict[str, Any] | None) -> list[str]:
    """Validate a raw candidate ``config_template`` before staging.

    Returns a list of block reasons (empty = safe to stage). Walks every
    ``command``/``url`` key at any nesting depth so a nested ``env``/``args``
    structure cannot smuggle a blocked value past a top-level-only check.
    """
    if not isinstance(config_template, dict):
        return []
    reasons: list[str] = []
    _walk(config_template, reasons)
    return reasons


def _walk(obj: Any, reasons: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l == "command":
                blocked, reason = is_dangerous_command(value)
                if blocked:
                    reasons.append(reason)  # type: ignore[arg-type]
            if key_l in ("url", "endpoint") and isinstance(value, str):
                blocked, reason = is_unsafe_url(value)
                if blocked:
                    reasons.append(reason)  # type: ignore[arg-type]
            _walk(value, reasons)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, reasons)
        # Also treat a bare list value under a "command" key at this level —
        # handled by the parent dict branch via is_dangerous_command(list).
