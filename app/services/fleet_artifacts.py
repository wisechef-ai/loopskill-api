"""fleetos_1607 Phase 0 — fleet-artifact services.

Three pure, dependency-light services the Phase 0 hard gate exercises:

* ``manifest`` — canonical serialization of a LoopManifest so that
  export → validate → import round-trips BYTE-IDENTICALLY (the Tori-cron
  round-trip gate). Canonical = sorted keys, stable field order, no volatile
  fields (ids/timestamps) in the transport form.

* ``scripts_pack`` — the secret-scan publish gate. REUSES the shipped
  ``app.security_scan.scan_tarball`` scanner (the exact one the marketplace
  publish path uses) so a planted key ⇒ publish refused. No second scanner.

* ``host_profile`` — validate a loop's typed ``requires{}`` against a host's
  os / runtimes / packages, returning a per-requirement pass/fail report
  (loud, named failures — the bootstrap precondition in Phase C).

Zero new third-party deps: the runtime version comparator is a ~20-line
dotted-tuple compare, not a `packaging`/`semver` import (5-step: don't add a
dependency you can write in fifteen lines).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.security_scan import Finding, scan_tarball

# ── Manifest canonical serialization ─────────────────────────────────────────

# The declarative fields of a LoopManifest, in canonical order. Volatile columns
# (id, manifest_version bump, created_at, updated_at) are DELIBERATELY excluded
# from the transport form — two manifests that declare the same desired state
# must serialize identically regardless of when/where they were stored.
MANIFEST_TRANSPORT_FIELDS: tuple[str, ...] = (
    "loop_id",
    "enabled",
    "schedule",
    "tz",
    "concurrency_policy",
    "prompt",
    "skills",
    "model",
    "deliver",
    "requires",
    "secret_refs",
    "state_class",
    "state_locator",
    "timeout_seconds",
    "safety_class",
    "reserved",
)

_VALID_CONCURRENCY = {"forbid", "allow", "replace"}
_VALID_STATE_CLASS = {"stateless", "external", "local-resettable", "local-required"}
_VALID_SAFETY_CLASS = {"idempotent", "best-effort", "manual-only", "fenced"}

# A cron-5-field or "<N>m|h" / "every <N>h" shorthand — mirrors the loop-spec
# validator's accepted forms so the fleet manifest and the marketplace loop
# artifact never disagree on what a schedule is.
_SHORTHAND_RE = re.compile(r"^(every\s+)?\d+\s*[mh]$", re.IGNORECASE)
_CRON_RE = re.compile(r"^(\S+\s+){4}\S+$")

# Secret-interpolation lint: a prompt must reference secrets by ${NAME}, never
# embed a literal-looking credential. This is a cheap smell check on the prompt
# text at manifest-write time (the real scanner runs on scripts packs).
_LITERAL_SECRET_RE = re.compile(
    r"(?i)(sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,}|xox[baprs]-[a-z0-9-]{10,}"
    r"|aws_secret_access_key\s*=\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class ManifestValidationError(ValueError):
    """Raised when a LoopManifest transport payload is malformed."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def validate_manifest(payload: dict[str, Any]) -> None:
    """Validate a LoopManifest transport payload. Raises ManifestValidationError.

    Enforces the typed contracts (§0 #5/#6/#11): required fields present,
    enum-valued fields in range, schedule parseable, and the prompt free of
    literal-looking secrets (secret-interpolation lint — §0 Phase 0 step 2).
    """
    if not isinstance(payload, dict):
        raise ManifestValidationError("<root>", "must be a JSON object")

    loop_id = payload.get("loop_id")
    if not loop_id or not isinstance(loop_id, str):
        raise ManifestValidationError("loop_id", "required non-empty string")

    schedule = payload.get("schedule")
    if not isinstance(schedule, str) or not (
        _SHORTHAND_RE.match(schedule.strip()) or _CRON_RE.match(schedule.strip())
    ):
        raise ManifestValidationError("schedule", f"unparseable schedule: {schedule!r}")

    cp = payload.get("concurrency_policy", "forbid")
    if cp not in _VALID_CONCURRENCY:
        raise ManifestValidationError("concurrency_policy", f"must be one of {sorted(_VALID_CONCURRENCY)}")

    sc = payload.get("state_class", "stateless")
    if sc not in _VALID_STATE_CLASS:
        raise ManifestValidationError("state_class", f"must be one of {sorted(_VALID_STATE_CLASS)}")

    sfc = payload.get("safety_class", "best-effort")
    if sfc not in _VALID_SAFETY_CLASS:
        raise ManifestValidationError("safety_class", f"must be one of {sorted(_VALID_SAFETY_CLASS)}")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ManifestValidationError("prompt", "required non-empty string")
    if _LITERAL_SECRET_RE.search(prompt):
        raise ManifestValidationError(
            "prompt",
            "contains a literal-looking secret — reference secrets by ${NAME} + a secret_ref instead",
        )

    for coll, typ in (("skills", list), ("secret_refs", list), ("requires", dict), ("reserved", dict)):
        val = payload.get(coll)
        if val is not None and not isinstance(val, typ):
            raise ManifestValidationError(coll, f"must be a {typ.__name__} when present")


def manifest_to_transport(obj: Any) -> dict[str, Any]:
    """Project a LoopManifest ORM row (or dict) into its canonical transport dict.

    Fills declared defaults for optional collection fields so an exported row and
    a hand-authored manifest with the same desired state compare equal.
    """

    def _get(k: str) -> Any:
        return obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)

    # Declared defaults so a minimal authored manifest and a full DB row with the
    # same desired state converge to identical canonical bytes.
    _DEFAULTS: dict[str, Any] = {
        "enabled": True,
        "tz": "UTC",
        "concurrency_policy": "forbid",
        "state_class": "stateless",
        "safety_class": "best-effort",
        "skills": [],
        "secret_refs": [],
        "requires": {},
        "reserved": {},
    }

    out: dict[str, Any] = {}
    for field in MANIFEST_TRANSPORT_FIELDS:
        val = _get(field)
        if val is None and field in _DEFAULTS:
            val = _DEFAULTS[field]
        out[field] = val
    return out


def canonical_manifest_json(obj: Any) -> str:
    """Serialize a LoopManifest to a canonical JSON string (stable, sorted).

    Two manifests with identical desired state ⇒ identical bytes. This is the
    round-trip contract: ``canonical_manifest_json(import(canonical_manifest_json(x)))``
    equals ``canonical_manifest_json(x)``.
    """
    transport = manifest_to_transport(obj)
    return json.dumps(transport, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_manifest_json(text: str) -> dict[str, Any]:
    """Parse + validate a canonical manifest JSON string into a transport dict."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ManifestValidationError("<root>", f"invalid JSON: {exc}") from exc
    validate_manifest(payload)
    return manifest_to_transport(payload)


# ── Scripts-pack secret-scan gate ────────────────────────────────────────────

# Severities that BLOCK a scripts-pack from being stored. Matches the publish
# path's blocking policy — critical/high are hard blocks.
BLOCKING_SEVERITIES = frozenset({"critical", "high"})


@dataclass
class ScriptsPackScanResult:
    """Outcome of scanning a scripts-pack tarball before storing it."""

    clean: bool
    findings: list[Finding]

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING_SEVERITIES]


def scan_scripts_pack(tarball_bytes: bytes) -> ScriptsPackScanResult:
    """Scan a scripts-pack tarball for planted secrets / traversal, REUSING the
    shipped ``security_scan.scan_tarball`` scanner.

    A pack is ``clean`` (installable, ``secret_scan_clean=True``) only when it
    has ZERO critical/high findings. The publish path must refuse to store a
    pack whose result is not clean — this is the RED-proof gate (planted key ⇒
    refused). ``skill_section={}`` because the requiredenv logical check
    (pattern 9) does not apply to scripts packs.
    """
    findings = scan_tarball(tarball_bytes, {})
    blocking = [f for f in findings if f.severity in BLOCKING_SEVERITIES]
    return ScriptsPackScanResult(clean=not blocking, findings=findings)


# ── Host-profile compatibility validation ────────────────────────────────────


@dataclass
class RequirementCheck:
    """One typed-requirement check outcome (loud + named)."""

    kind: str  # 'os' | 'arch' | 'runtime' | 'package'
    requirement: str
    satisfied: bool
    detail: str


@dataclass
class HostProfileReport:
    """The result of validating a loop's requires{} against a host profile."""

    ok: bool
    checks: list[RequirementCheck]

    @property
    def unmet(self) -> list[RequirementCheck]:
        return [c for c in self.checks if not c.satisfied]


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into an int tuple; non-numeric parts → 0."""
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", str(v)):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def _cmp_versions(a: str, b: str) -> int:
    """Return -1/0/1 for version a vs b using zero-padded dotted-tuple compare."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    width = max(len(ta), len(tb))
    ta += (0,) * (width - len(ta))
    tb += (0,) * (width - len(tb))
    return (ta > tb) - (ta < tb)


# specifier → predicate(installed, required)
_SPECIFIERS: dict[str, Any] = {
    ">=": lambda i, r: _cmp_versions(i, r) >= 0,
    "<=": lambda i, r: _cmp_versions(i, r) <= 0,
    "==": lambda i, r: _cmp_versions(i, r) == 0,
    ">": lambda i, r: _cmp_versions(i, r) > 0,
    "<": lambda i, r: _cmp_versions(i, r) < 0,
}


def _satisfies_spec(installed: str, spec: str) -> bool:
    """Check an installed version against a specifier like '>=3.11' or '3.10'."""
    spec = str(spec).strip()
    for op_str in (">=", "<=", "==", ">", "<"):
        if spec.startswith(op_str):
            return bool(_SPECIFIERS[op_str](installed, spec[len(op_str) :].strip()))
    # Bare version → treat as ">=" (a floor), the common "python: 3.11" intent.
    return _cmp_versions(installed, spec) >= 0


def validate_host_profile(requires: dict[str, Any], profile: dict[str, Any]) -> HostProfileReport:
    """Validate a loop's typed ``requires{}`` against a host ``profile``.

    ``requires`` shape (all keys optional):
        {"os": ["linux"], "arch": ["x86_64"],
         "runtime": {"python": ">=3.11"}, "packages": ["git", "ripgrep"]}
    ``profile`` shape:
        {"os": {"os": "linux", "arch": "x86_64"},
         "runtimes": {"python": "3.11.9"}, "packages": ["git", "curl"]}

    Returns a per-requirement report. ``ok`` is True iff every check passed.
    """
    checks: list[RequirementCheck] = []
    prof_os = profile.get("os") or {}
    host_os = str(prof_os.get("os", "")).lower()
    host_arch = str(prof_os.get("arch", "")).lower()
    runtimes = profile.get("runtimes") or {}
    packages = {str(p).lower() for p in (profile.get("packages") or [])}

    req_os = requires.get("os")
    if req_os:
        wanted = [str(o).lower() for o in (req_os if isinstance(req_os, list) else [req_os])]
        ok = host_os in wanted
        checks.append(RequirementCheck("os", f"os in {wanted}", ok, f"host os={host_os or '?'}"))

    req_arch = requires.get("arch")
    if req_arch:
        wanted = [str(a).lower() for a in (req_arch if isinstance(req_arch, list) else [req_arch])]
        ok = host_arch in wanted
        checks.append(RequirementCheck("arch", f"arch in {wanted}", ok, f"host arch={host_arch or '?'}"))

    req_runtime = requires.get("runtime") or {}
    if isinstance(req_runtime, dict):
        for name, spec in req_runtime.items():
            installed = runtimes.get(name)
            if installed is None:
                checks.append(
                    RequirementCheck("runtime", f"{name}{spec}", False, f"{name} not present on host")
                )
            else:
                ok = _satisfies_spec(str(installed), str(spec))
                checks.append(RequirementCheck("runtime", f"{name}{spec}", ok, f"host {name}={installed}"))

    for pkg in requires.get("packages") or []:
        ok = str(pkg).lower() in packages
        checks.append(RequirementCheck("package", str(pkg), ok, "present" if ok else "MISSING"))

    return HostProfileReport(ok=all(c.satisfied for c in checks), checks=checks)
