"""§7.2 Security Scanner for recipes skill tarballs.

Implements scan_tarball() per CONTRACT_security_scan.md.
Stdlib + re only — no third-party scanner dependencies.

10 pattern classes:
  1  destructive         — rm -rf /, fork bombs, mkfs, dd to raw device
  2  pipe_to_shell       — curl/wget | bash
  3  eval_remote         — eval of curl output or base64-encoded payload
  4  base64_long         — >100-char base64 blob inside scripts/ only
  5  hex_encoded_shell   — 10+ consecutive \\xNN sequences
  6  credential_harvest  — reads ~/.ssh, ~/.aws/credentials, keychain, etc.
  7  prompt_injection    — LLM jailbreak phrases (case-insensitive)
  8  creds_in_files      — real-shaped API keys/tokens
  9  requiredenv_mismatch— STRIPE_*/OPENAI_*/etc. declared by unrelated skill
  10 path_escape         — path traversal or writes to /etc, /var, /usr, ~/.ssh
"""

from __future__ import annotations

import io
import os
import re
import tarfile
from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Data model (wire format defined in CONTRACT_security_scan.md)
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    pattern_class: str
    severity: Literal["critical", "high", "medium", "low"]
    file_path: str
    line_no: int | None      # 1-indexed; None for binary/whole-file findings
    snippet: str             # max 200 chars
    rationale: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB per-file limit

# Binary extensions to skip entirely
_BINARY_EXT_RE = re.compile(
    r'\.(png|jpg|gif|pdf|zip|tar\.gz|tar|bin)$', re.IGNORECASE
)

# Markdown code-fence detector (to suppress base64_long inside fenced blocks)
_CODE_FENCE_RE = re.compile(r'^\s*```')


# ---------------------------------------------------------------------------
# Pattern 1 — destructive
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = [
    # Contract regex uses \b but / is non-word so we use a lookahead that
    # matches end-of-token (space, end-of-line, non-word, or bare slash).
    re.compile(r'rm\s+-rf\s+(/|~|\$HOME)(?=\s|$|[^a-zA-Z0-9_.\-])'),
    re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),
    re.compile(r'mkfs\.[a-z0-9]+\s+/dev/'),
    re.compile(r'\bdd\s+.*of=/dev/(sd[a-z]|nvme|hd)'),
]

# ---------------------------------------------------------------------------
# Pattern 2 — pipe_to_shell
# ---------------------------------------------------------------------------

_PIPE_TO_SHELL_PATTERNS = [
    re.compile(r'\bcurl\s+[^|]*\|\s*(bash|sh|zsh|fish)\b'),
    re.compile(r'\bwget\s+[^|]*\|\s*(bash|sh|zsh|fish)\b'),
]

# ---------------------------------------------------------------------------
# Pattern 3 — eval_remote
# ---------------------------------------------------------------------------

_EVAL_REMOTE_PATTERNS = [
    re.compile(r'\beval\s*\(?\s*\$\s*\(\s*curl'),
    re.compile(r'\beval\s*\(\s*(?:atob|base64)'),
    re.compile(r'\bexec\s*\(\s*(?:atob|base64)'),
]

# ---------------------------------------------------------------------------
# Pattern 4 — base64_long  (scripts/ only, not references/, not inside fences)
# ---------------------------------------------------------------------------

_BASE64_LONG_RE = re.compile(r'[A-Za-z0-9+/]{100,}={0,2}')


def _in_scripts_dir(path: str) -> bool:
    """Return True if the tarball path is inside a scripts/ directory."""
    parts = path.replace('\\', '/').split('/')
    return 'scripts' in parts[:-1]  # 'scripts' must be a directory component


def _in_references_dir(path: str) -> bool:
    parts = path.replace('\\', '/').split('/')
    return 'references' in parts[:-1]


# ---------------------------------------------------------------------------
# Pattern 5 — hex_encoded_shell
# ---------------------------------------------------------------------------

_HEX_ENCODED_RE = re.compile(r'(?:\\x[0-9a-fA-F]{2}){10,}')

# ---------------------------------------------------------------------------
# Pattern 6 — credential_harvest
# ---------------------------------------------------------------------------

_CRED_HARVEST_PATTERNS = [
    re.compile(r'~/\.ssh/(?!authorized_keys(?:\b|$))'),
    re.compile(r'~/\.aws/credentials'),
    re.compile(r'~/\.netrc\b'),
    re.compile(r'~/\.config/gh/'),
    re.compile(r'security\s+find-(?:internet|generic)-password'),
    re.compile(r'\bkeychain\s+(?:show|find)\b'),
]

# ---------------------------------------------------------------------------
# Pattern 7 — prompt_injection (case-insensitive)
# ---------------------------------------------------------------------------

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(?:all\s+)?previous\s+(?:instructions|context)', re.IGNORECASE),
    re.compile(r'disregard\s+the\s+(?:system\s+)?prompt', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+(?:[A-Z]|a\s+different)', re.IGNORECASE),
    re.compile(r'forget\s+everything\s+(?:above|prior)', re.IGNORECASE),
]

# F-API-12: negation lookbehind — preceding text ending with a negation word
_NEGATION_RE = re.compile(
    r"(?:do\s+not|don'?t|never|cannot|can'?t|won'?t|will\s+not)\s+(?:\S+\s+){0,3}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Pattern 8 — creds_in_files
# ---------------------------------------------------------------------------

_CREDS_IN_FILES_RE = re.compile(
    r'\b('
    r'sk_live_[A-Za-z0-9]{20,}'
    r'|whsec_[A-Za-z0-9]{20,}'
    r'|rk_live_[A-Za-z0-9]{20,}'
    r'|ghp_[A-Za-z0-9]{30,}'
    r'|gho_[A-Za-z0-9]{30,}'
    r'|xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+'
    r'|AIza[A-Za-z0-9_\-]{35}'
    r'|sk-(?:proj-)?[A-Za-z0-9]{20,}'
    r')\b'
)

# ---------------------------------------------------------------------------
# Pattern 9 — requiredenv_mismatch (logical check, not line-by-line)
# ---------------------------------------------------------------------------

# (pattern, set-of-categories-that-should-NOT-need-it)
_REQUIREDENV_RULES: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(r'^STRIPE_', re.IGNORECASE),    {'marketing', 'utility', 'search', 'translation', 'text'}),
    (re.compile(r'^OPENAI_', re.IGNORECASE),    {'billing', 'payment', 'stripe', 'accounting'}),
    (re.compile(r'^ANTHROPIC_', re.IGNORECASE), {'billing', 'payment', 'stripe', 'accounting'}),
    (re.compile(r'^GITHUB_', re.IGNORECASE),    {'marketing', 'billing', 'translation', 'payment'}),
]

# ---------------------------------------------------------------------------
# Pattern 10 — path_escape
# ---------------------------------------------------------------------------

_PATH_ESCAPE_PATTERNS = [
    re.compile(r'\.\./\.\./'),                                          # ../..
    re.compile(r'os\.path\.join\([^,]*,[^,]*,[^)]*\.\.[^)]*\)'),       # os.path.join with ..
    # Write operations targeting sensitive paths
    re.compile(
        r'''(?:open|write_text|write_bytes)\s*\(\s*['"](?:/etc/|/var/|/usr/|~/\.ssh/)'''
    ),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _mk(
    pattern_class: str,
    severity: str,
    file_path: str,
    line_no: int | None,
    snippet: str,
    rationale: str,
) -> Finding:
    return Finding(
        pattern_class=pattern_class,
        severity=severity,  # type: ignore[arg-type]
        file_path=file_path,
        line_no=line_no,
        snippet=snippet[:200],
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_tarball(tarball_bytes: bytes, skill_section: dict) -> list[Finding]:
    """Scan a .tar.gz skill package for the 10 security patterns.

    Returns a list of Finding objects (empty = clean).
    The caller is responsible for deciding which severities block publishing.
    Pattern 9 (requiredenv_mismatch) is checked before the tarball walk.
    """
    findings: list[Finding] = []

    # Pattern 9 — logical check, no tarball walk required
    findings.extend(_check_requiredenv(skill_section))

    # Open tarball in memory; never write to disk
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError):
        # Can't decompress → nothing to scan; publisher's size/sig checks
        # already ran; return whatever findings we have (e.g. pattern 9).
        return findings

    with tf:
        for member in tf.getmembers():
            # ── Issue #10: Path-traversal gate (checked for ALL member types) ──
            name = member.name
            # Reject absolute paths
            if name.startswith("/"):
                findings.append(_mk(
                    "path_traversal", "critical", name, None,
                    name[:200],
                    "Tarball member has absolute path — path traversal risk",
                ))
                continue
            # Reject '..' components (parent traversal)
            if ".." in name.split("/"):
                findings.append(_mk(
                    "path_traversal", "critical", name, None,
                    name[:200],
                    "Tarball member contains '..' component — parent traversal risk",
                ))
                continue
            # Reject drive letters / NTFS alternate data streams
            if ":" in name:
                findings.append(_mk(
                    "path_traversal", "critical", name, None,
                    name[:200],
                    "Tarball member contains ':' — drive-letter or NTFS stream risk",
                ))
                continue
            # Reject symlinks whose target is absolute or contains '..'
            if member.issym():
                target = member.linkname or ""
                if target.startswith("/") or ".." in target.split("/"):
                    findings.append(_mk(
                        "path_traversal", "critical", name, None,
                        f"symlink -> {target[:200]}",
                        "Tarball symlink target escapes sandbox — path traversal risk",
                    ))
                continue  # skip further file-content scanning for symlinks
            # Reject names that normalise differently (catches ./a/../b)
            if os.path.normpath(name) != name:
                findings.append(_mk(
                    "path_traversal", "critical", name, None,
                    name[:200],
                    "Tarball member name normalises differently — path traversal risk",
                ))
                continue

            if not member.isfile():
                continue  # skip dirs, special files after path checks

            path = member.name

            # Skip binary-extension files
            if _BINARY_EXT_RE.search(path):
                continue

            # Oversize → low finding, skip pattern scan
            if member.size > MAX_FILE_BYTES:
                findings.append(_mk(
                    "oversize_file", "low", path, None,
                    f"file_size={member.size}",
                    "File exceeds 1 MB; skipped from pattern scan",
                ))
                continue

            # Extract
            try:
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                raw = fobj.read()
            except (tarfile.TarError, OSError):
                continue

            text = raw.decode("utf-8", errors="replace")
            findings.extend(_scan_file_lines(path, text.splitlines()))

    return findings


# ---------------------------------------------------------------------------
# Per-file scanner (patterns 1–8, 10)
# ---------------------------------------------------------------------------

def _scan_file_lines(path: str, lines: list[str]) -> list[Finding]:
    results: list[Finding] = []
    in_code_fence = False
    is_script = _in_scripts_dir(path)
    is_ref = _in_references_dir(path)

    for lineno, line in enumerate(lines, start=1):
        # Track markdown code-fence state (suppress base64_long inside fences)
        if _CODE_FENCE_RE.match(line):
            in_code_fence = not in_code_fence

        # ── 1. destructive ──────────────────────────────────────────────
        for pat in _DESTRUCTIVE_PATTERNS:
            if pat.search(line):
                results.append(_mk(
                    "destructive", "high", path, lineno,
                    line.strip()[:200],
                    "Filesystem destruction or fork-bomb pattern detected",
                ))
                break

        # ── 2. pipe_to_shell ────────────────────────────────────────────
        for pat in _PIPE_TO_SHELL_PATTERNS:
            if pat.search(line):
                results.append(_mk(
                    "pipe_to_shell", "high", path, lineno,
                    line.strip()[:200],
                    "Pipes remote URL content directly to a shell — remote code execution risk",
                ))
                break

        # ── 3. eval_remote ──────────────────────────────────────────────
        for pat in _EVAL_REMOTE_PATTERNS:
            if pat.search(line):
                results.append(_mk(
                    "eval_remote", "high", path, lineno,
                    line.strip()[:200],
                    "Eval of remote fetch result or base64-encoded payload",
                ))
                break

        # ── 4. base64_long (scripts/ only, not references/, not in code-fence)
        if is_script and not is_ref and not in_code_fence:
            m = _BASE64_LONG_RE.search(line)
            if m:
                results.append(_mk(
                    "base64_long", "medium", path, lineno,
                    m.group()[:200],
                    "Long base64 blob in script file — likely payload obfuscation",
                ))

        # ── 5. hex_encoded_shell ────────────────────────────────────────
        m = _HEX_ENCODED_RE.search(line)
        if m:
            results.append(_mk(
                "hex_encoded_shell", "high", path, lineno,
                m.group()[:200],
                "Ten or more consecutive hex-escaped bytes — obfuscated payload",
            ))

        # ── 6. credential_harvest ───────────────────────────────────────
        for pat in _CRED_HARVEST_PATTERNS:
            if pat.search(line):
                results.append(_mk(
                    "credential_harvest", "high", path, lineno,
                    line.strip()[:200],
                    "Accesses credential file or system keychain",
                ))
                break

        # ── 7. prompt_injection ─────────────────────────────────────────
        for pat in _PROMPT_INJECTION_PATTERNS:
            for m in pat.finditer(line):
                preceding = line[:m.start()]
                if _NEGATION_RE.search(preceding):
                    continue  # F-API-12: legitimate negation context, skip
                results.append(_mk(
                    "prompt_injection", "high", path, lineno,
                    line.strip()[:200],
                    "LLM prompt-injection payload detected",
                ))
                break  # one finding per line per pattern class is enough
            else:
                continue
            break

        # ── 8. creds_in_files ───────────────────────────────────────────
        m = _CREDS_IN_FILES_RE.search(line)
        if m:
            results.append(_mk(
                "creds_in_files", "high", path, lineno,
                m.group()[:200],
                "Real-shaped credential string found in shipped file",
            ))

        # ── 10. path_escape ─────────────────────────────────────────────
        for pat in _PATH_ESCAPE_PATTERNS:
            if pat.search(line):
                results.append(_mk(
                    "path_escape", "high", path, lineno,
                    line.strip()[:200],
                    "Path traversal or write to sensitive system path detected",
                ))
                break

    return results


# ---------------------------------------------------------------------------
# Pattern 9 — requiredenv_mismatch
# ---------------------------------------------------------------------------

def _check_requiredenv(skill_section: dict) -> list[Finding]:
    """Flag credential-type env vars declared by unrelated skill categories."""
    findings: list[Finding] = []

    # Support both requiredEnv and [skill.env] layouts
    required_env = skill_section.get("requiredEnv") or skill_section.get("env") or {}
    if not isinstance(required_env, dict):
        return findings

    category = (skill_section.get("category") or "").lower().strip()

    for env_key in required_env:
        for cred_pat, suspicious_categories in _REQUIREDENV_RULES:
            if cred_pat.match(str(env_key)):
                if category in suspicious_categories:
                    findings.append(_mk(
                        "requiredenv_mismatch", "medium", "skill.toml", None,
                        str(env_key)[:200],
                        f"Skill declares {env_key} but category '{category}' "
                        "has no obvious need for this credential type — possible credential bait",
                    ))
                    break

    return findings
