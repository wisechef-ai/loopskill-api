"""Connector publish-time validation — loopskill_activate_0701 Phase B.

The whole point of a Connector is to ship a config TEMPLATE that the agent
resolves against its OWN environment. That contract is only trustworthy if the
server enforces three things at publish time:

  1. **Structural**: ``config_template`` is a JSON object whose required keys
     depend on ``connector_type`` (stdio→command, http/sse→url).
  2. **Secret discipline (§0.5)**: only ``${VAR}`` env refs are allowed where
     sensitive values go. Literal secrets (real-shaped API keys, Bearer tokens,
     long base64ish strings, ``/home/<user>`` paths) are HARD-REJECTED. The
     published artifact must be grep-provable clean.
  3. **required_env consistency**: every var the template references as
     ``${VAR}`` need not be in required_env (optional vars are allowed), but
     every var LISTED in required_env MUST appear as a ``${VAR}`` ref somewhere
     in the template — otherwise the apply gate would block on a var the
     template never uses.

The secret-lint patterns reuse the existing skill-publish needles
(``app.security_scan`` Pattern 8 ``creds_in_files`` and
``app.skill_quality_gate`` leak patterns) so the rule surface stays consistent
across artifact types. Pure function — no I/O, no DB. Safe to call from the
HTTP route, the MCP tool, and tests.
"""

from __future__ import annotations

import re
from typing import Any

# ── connector_type → required config_template keys ─────────────────────────

_TYPE_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "stdio": ("command",),
    "http": ("url",),
    "sse": ("url",),
}

# ── ${VAR} reference detection ─────────────────────────────────────────────

# Matches ${VAR_NAME} — uppercase ASCII + underscore, 1–63 chars (env-var shape).
_VAR_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]{0,62})\}")

# ── Literal-secret needles (reuse the skill-publish lint surface) ──────────
# These are the same shape as app.security_scan._CREDS_IN_FILES_RE (Pattern 8)
# and the Bearer/Authorization leak class. Keeping them aligned means a new
# secret shape discovered in skill-publish is also blocked here.

# Real-shaped API keys: sk_live_, whsec_, ghp_, sk-, AIza, xoxb-, …
_LITERAL_SECRET_RE = re.compile(
    r"\b("
    r"sk_live_[A-Za-z0-9]{20,}"
    r"|whsec_[A-Za-z0-9]{20,}"
    r"|rk_live_[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|gho_[A-Za-z0-9]{30,}"
    r"|xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+"
    r"|AIza[A-Za-z0-9_\-]{35}"
    r"|sk-(?:proj-)?[A-Za-z0-9]{20,}"
    r"|rec_live_[A-Za-z0-9]{16,}"
    r")\b"
)

# Bearer <token> in any string slot — Authorization headers must be ${VAR}.
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-/+=]{8,}", re.IGNORECASE)

# 40+ char base64ish string that is NOT a ${VAR} ref — opaque blob = secret.
_BASE64ISH_RE = re.compile(r"(?<!\$\{)[A-Za-z0-9+/]{40,}={0,2}(?!\})")

# Absolute /home/<user> path — recon disclosure (mirrors skill_quality_gate).
_HOME_PATH_RE = re.compile(r"/home/[a-z][a-z0-9_-]+(?:/|$|\b)")

# Public-routable IPv4 (reuses the skill_quality_gate private/example filter).
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)


def _is_private_or_example_ip(ip: str) -> bool:
    """Same filter as app.skill_quality_gate._is_private_or_example_ip."""
    if ip in {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "127.0.0.1", "0.0.0.0"}:
        return True
    parts = [int(p) for p in ip.split(".")]
    if parts[0] == 10:
        return True
    if parts[0] == 127:
        return True
    if parts[0] == 192 and parts[1] == 168:
        return True
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return True
    if parts[0] == 169 and parts[1] == 254:
        return True
    if parts[0] >= 224:
        return True
    return False


class ConnectorValidationError(ValueError):
    """Raised when a connector config_template violates the publish contract."""


def _walk_strings(obj: Any):
    """Yield every string value in a nested JSON object/list."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _find_literal_secrets(config_template: dict[str, Any]) -> list[str]:
    """Return a list of human-readable reasons for each literal-secret finding."""
    findings: list[str] = []
    for s in _walk_strings(config_template):
        # Skip ${VAR} refs entirely — they are the allowed form.
        if _VAR_REF_RE.fullmatch(s):
            continue
        if _LITERAL_SECRET_RE.search(s):
            findings.append("literal.secret: real-shaped API key/token in config_template (use ${VAR})")
            continue
        if _BEARER_RE.search(s):
            findings.append("literal.secret: Bearer <token> in config_template (use ${VAR})")
            continue
        if _HOME_PATH_RE.search(s):
            findings.append("leak: absolute /home/<user> path in config_template (use ~/ or $HOME)")
            continue
        # Long opaque base64ish blob, but NOT inside a ${VAR} ref and NOT a URL.
        # URLs (http://…) legitimately contain long tokens; skip if it looks like a URL.
        if not s.startswith(("http://", "https://", "npx", "node", "python", "uvx", "docker")):
            # Strip any ${VAR} refs from the string before testing, so a
            # template like "Bearer ${TOKEN}" doesn't false-positive.
            stripped = _VAR_REF_RE.sub("", s)
            m = _BASE64ISH_RE.search(stripped)
            if m:
                findings.append("literal.secret: 40+ char opaque blob in config_template (use ${VAR})")
                continue
            for ip_m in _IPV4_RE.finditer(stripped):
                if not _is_private_or_example_ip(ip_m.group()):
                    findings.append("leak: public IPv4 in config_template (use a hostname or ${VAR})")
                    break
    return findings


def _referenced_vars(config_template: dict[str, Any]) -> set[str]:
    """All ${VAR} names referenced anywhere in the template."""
    refs: set[str] = set()
    for s in _walk_strings(config_template):
        refs.update(_VAR_REF_RE.findall(s))
    return refs


def validate_connector_version(
    *,
    connector_type: str,
    config_template: dict[str, Any],
    required_env: list[str] | None,
) -> dict[str, Any]:
    """Validate + normalize a ConnectorVersion publish payload.

    Returns the cleaned dict {connector_type, config_template, required_env}.
    Raises ConnectorValidationError with an actionable message on any violation.
    Pure function — no I/O, no DB.
    """
    errors: list[str] = []

    # ── 1. connector_type is one of the known shapes ──
    if connector_type not in _TYPE_REQUIRED_KEYS:
        errors.append(f"connector_type must be one of {sorted(_TYPE_REQUIRED_KEYS)}, got {connector_type!r}")
        # Without a known type we can't check the required keys; bail with the
        # type error rather than cascading into confusing structural errors.
        if errors:
            raise ConnectorValidationError("; ".join(errors))

    # ── 2. config_template is a JSON object with the type-required keys ──
    if not isinstance(config_template, dict):
        errors.append("config_template must be a JSON object")
        raise ConnectorValidationError("; ".join(errors))

    required_keys = _TYPE_REQUIRED_KEYS[connector_type]
    for key in required_keys:
        val = config_template.get(key)
        if not val or not isinstance(val, (str, list, dict)):
            errors.append(
                f"config_template missing required field {key!r} for connector_type {connector_type!r}"
            )

    # ── 3. Secret lint — hard reject literal secrets; only ${VAR} refs allowed ──
    secret_findings = _find_literal_secrets(config_template)
    errors.extend(secret_findings)

    # ── 4. required_env consistency: each listed var must appear as ${VAR} ──
    required_env = list(required_env or [])
    refs = _referenced_vars(config_template)
    for var in required_env:
        if not isinstance(var, str) or not var:
            errors.append(f"required_env entry {var!r} must be a non-empty string")
            continue
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,62}", var):
            errors.append(f"required_env entry {var!r} must be an uppercase env-var name ([A-Z_][A-Z0-9_]*)")
            continue
        if var not in refs:
            errors.append(
                f"required_env entry {var!r} is not referenced as ${{{var}}} in config_template "
                "(every required_env var must appear as a ${VAR} ref)"
            )

    if errors:
        raise ConnectorValidationError("; ".join(errors))

    return {
        "connector_type": connector_type,
        "config_template": config_template,
        "required_env": required_env,
    }
