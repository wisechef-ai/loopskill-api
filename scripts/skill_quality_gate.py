#!/usr/bin/env python3
"""skill_quality_gate.py — Single canonical pre-publish gate for Recipes skills.

Combines THREE check sources into one tool:

  A. security_scan.py 10 patterns (malicious code, prompt injection, real creds)
  B. leak audit 8 categories (internal IPs, UUIDs, paths, hostnames, real-case forensics)
  C. clawdhub install-time grep patterns (defense-in-depth)
  D. generalization checks (personal names, hardcoded internal URLs, absolute home paths)

Usage:
    skill_quality_gate.py <path>              # scan a directory or .tar.gz
    skill_quality_gate.py <path> --json       # machine-readable
    skill_quality_gate.py <path> --strict     # fail on any finding (CI default)
    skill_quality_gate.py <path> --baseline   # snapshot current findings as ok-list
    skill_quality_gate.py <path> --org-allowlist wisechef-ai,recipes  # whitelist OSS repos

Exit codes:
    0 = clean (no findings, OR all findings within baseline allowlist)
    1 = warnings only (medium/low severity)
    2 = blocking (high severity, or strict mode with any finding)
    3 = usage error / scan failed

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

Severity = Literal["block", "warn", "info"]


@dataclass
class Finding:
    category: str             # short identifier, e.g. "internal_ip"
    severity: Severity
    file_path: str
    line_no: int | None
    snippet: str
    rationale: str
    source: str = ""          # which check source (sec_scan / leak / clawdhub / gen)

    def short(self) -> str:
        loc = f"{self.file_path}:{self.line_no}" if self.line_no else self.file_path
        return f"[{self.severity.upper():5}] {self.category:25} {loc}  {self.snippet[:80]}"


# ---------------------------------------------------------------------------
# Patterns — Source A: malicious / runtime threats (from security_scan.py)
# ---------------------------------------------------------------------------

_DESTRUCTIVE_RM_RE = re.compile(r"rm\s+-rf?\s+(/|~|\$HOME)(?=\s|$|[^a-zA-Z0-9_.\-/])")

# Safe deletion targets — `rm -rf` against these is benign cache cleanup, not a wipe.
# When the path after `rm -rf` matches any of these, suppress destructive_rm.
_SAFE_RM_TARGETS = re.compile(
    r"rm\s+-rf?\s+(?:"
    r"~/\.cache/|"               # User cache (HuggingFace, pip, npm, etc.)
    r"~/\.local/share/Trash|"    # XDG trash
    r"~/\.npm/|~/\.pnpm/|~/\.yarn/|"  # Package manager caches
    r"~/node_modules/|"          # node deps
    r"\$HOME/\.cache/|"
    r"/tmp/[a-zA-Z0-9_./\-]+|"   # /tmp scratch
    r"\./[a-zA-Z0-9_\-./]+|"     # ./relative paths in current dir
    r"\$\{?[A-Z_][A-Z0-9_]*\}?/"  # Env-var-rooted paths like $WORKDIR/
    r")"
)

# Known-good upstream installers — curl|bash from these orgs is documented practice.
# These appear in 90% of OSS install instructions; blocking them is a usability tax.
_TRUSTED_INSTALLER_DOMAINS = re.compile(
    r"https?://(?:"
    # raw.githubusercontent.com/<USER>/<REPO>/... — the user is the trust anchor
    r"raw\.githubusercontent\.com/(?:starship|rye-up|astral-sh|"
    r"ohmyzsh|nvm-sh|pyenv|rbenv|sdkman|"
    r"xdevplatform|denoland|bun-sh|oven-sh|cli|atuinsh|jorgebucaran|"
    r"helmfile|kubectl|kubernetes-sigs|tursodatabase|"
    r"wisechef-ai)/|"
    # github.com/<USER>/<REPO>/releases/download/.../installer.sh
    r"github\.com/(?:starship|astral-sh|denoland|bun-sh|oven-sh|"
    r"xdevplatform|wisechef-ai)/[\w\-]+/releases/|"
    # Direct installer subdomains
    r"sh\.rustup\.rs|"
    r"get\.docker\.com|get\.helm\.sh|get\.k3s\.io|get\.k0s\.sh|"
    r"install\.python-poetry\.org|"
    r"sh\.brew\.sh|cli\.github\.com|"
    r"deno\.land/install\.sh|bun\.sh/install"
    r")"
)

# Public DNS resolvers — these are documentation examples, not infra disclosure.
_PUBLIC_DNS_IPS = {
    "1.1.1.1", "1.0.0.1",         # Cloudflare
    "8.8.8.8", "8.8.4.4",         # Google
    "9.9.9.9", "149.112.112.112", # Quad9
    "208.67.222.222", "208.67.220.220",  # OpenDNS
    "76.76.2.0", "76.76.10.0",    # ControlD
}

# SSH paths considered legitimate user-isolated patterns (not credential harvest).
# Only block reads of authorized_keys / id_* / known_hosts which contain actual creds.
_SSH_HARVEST_RE = re.compile(
    r"~/\.ssh/(?:authorized_keys|id_[a-z0-9_]+(?:\.pub)?|known_hosts)\b"
)
# Old broad rule (any ~/.ssh/ access) is replaced with the narrow one above.

PATTERNS_MALICIOUS = [
    # destructive — uses dedicated handler, not flat list scan (so safe-target check works)
    ("destructive_forkbomb", "block",
     re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "Fork bomb"),
    ("destructive_mkfs", "block",
     re.compile(r"mkfs\.[a-z0-9]+\s+/dev/"),
     "Filesystem format on raw device"),
    ("destructive_dd", "block",
     re.compile(r"\bdd\s+.*of=/dev/(sd[a-z]|nvme|hd)"),
     "dd to raw block device"),
    # pipe-to-shell — handled with installer allowlist
    ("eval_curl", "block",
     re.compile(r"\beval\s*\(?\s*\$\s*\(\s*curl"),
     "eval of curl output"),
    ("eval_b64", "block",
     re.compile(r"\beval\s*\(\s*(?:atob|base64)"),
     "eval of base64-decoded payload"),
    ("exec_b64", "block",
     re.compile(r"\bexec\s*\(\s*(?:atob|base64)"),
     "exec of base64-decoded payload"),
    # hex obfuscation
    ("hex_obfuscation", "block",
     re.compile(r"(?:\\x[0-9a-fA-F]{2}){10,}"),
     "Hex-escaped payload (10+ bytes)"),
    # credential harvest — narrowed to actual cred files, not ~/.ssh/* generally
    ("cred_harvest_aws", "block",
     re.compile(r"~/\.aws/credentials"),
     "Reads ~/.aws/credentials"),
    ("cred_harvest_netrc", "block",
     re.compile(r"~/\.netrc\b"),
     "Reads ~/.netrc"),
    ("cred_harvest_gh", "block",
     re.compile(r"~/\.config/gh/"),
     "Reads ~/.config/gh"),
    ("cred_harvest_keychain", "block",
     re.compile(r"\b(security\s+find-(?:internet|generic)-password|keychain\s+(?:show|find))\b"),
     "macOS keychain access"),
    # prompt injection
    ("prompt_injection_ignore", "block",
     re.compile(r"ignore\s+(?:all\s+)?previous\s+(?:instructions|context)", re.I),
     "Prompt injection — ignore-previous"),
    ("prompt_injection_disregard", "block",
     re.compile(r"disregard\s+the\s+(?:system\s+)?prompt", re.I),
     "Prompt injection — disregard-system"),
    ("prompt_injection_role", "block",
     re.compile(r"you\s+are\s+now\s+(?:[A-Z]|a\s+different)", re.I),
     "Prompt injection — role swap"),
    ("prompt_injection_forget", "block",
     re.compile(r"forget\s+everything\s+(?:above|prior)", re.I),
     "Prompt injection — memory wipe"),
    # real credentials
    ("creds_stripe_live", "block",
     re.compile(r"\bsk_live_[A-Za-z0-9]{20,}"),
     "Live Stripe secret key"),
    ("creds_stripe_webhook", "block",
     re.compile(r"\bwhsec_[A-Za-z0-9]{20,}"),
     "Stripe webhook secret"),
    ("creds_stripe_restricted", "block",
     re.compile(r"\brk_live_[A-Za-z0-9]{20,}"),
     "Live Stripe restricted key"),
    ("creds_github_pat", "block",
     re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
     "GitHub personal access token"),
    ("creds_github_oauth", "block",
     re.compile(r"\bgho_[A-Za-z0-9]{30,}"),
     "GitHub OAuth token"),
    ("creds_slack_bot", "block",
     re.compile(r"\bxoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+"),
     "Slack bot token"),
    ("creds_google_api", "block",
     re.compile(r"\bAIza[A-Za-z0-9_\-]{35}"),
     "Google API key"),
    ("creds_openai", "block",
     re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
     "OpenAI/OpenAI-shaped key"),
    ("creds_anthropic", "block",
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
     "Anthropic API key"),
    ("creds_zai", "block",
     re.compile(r"\b[a-z0-9]{32}\.[A-Za-z0-9]{16}"),
     "Z.AI / GLM API key shape"),
    # path escape
    ("path_traversal", "warn",
     re.compile(r"\.\./\.\./"),
     "Path traversal"),
    ("path_escape_write", "block",
     re.compile(r"""(?:open|write_text|write_bytes)\s*\(\s*['"](?:/etc/|/var/|/usr/|~/\.ssh/)"""),
     "Write to sensitive system path"),
]

# Patterns handled outside the flat list (need custom logic for safe-target / allowlist)
_PIPE_TO_SHELL_RE = re.compile(r"\b(?:curl|wget)\s+[^|]*\|\s*(?:bash|sh|zsh|fish)\b")

# ---------------------------------------------------------------------------
# Patterns — Source B: leak audit (internal info)
# ---------------------------------------------------------------------------

# IPv4 — but exclude obvious example ranges (0.0.0.0, 127.0.0.0/8, 10/8, 192.168/16, 172.16/12)
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)

def _is_private_or_example_ip(ip: str) -> bool:
    """RFC1918 + loopback + link-local + TEST-NET + 0.0.0.0 — these are OK in examples."""
    parts = [int(p) for p in ip.split(".")]
    if parts == [0, 0, 0, 0]:
        return True
    if parts[0] == 127:
        return True
    if parts[0] == 10:
        return True
    if parts[0] == 192 and parts[1] == 168:
        return True
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return True
    if parts[0] == 169 and parts[1] == 254:
        return True
    if parts[0] == 192 and parts[1] == 0 and parts[2] == 2:  # TEST-NET-1
        return True
    if parts[0] == 198 and parts[1] == 51 and parts[2] == 100:  # TEST-NET-2
        return True
    if parts[0] == 203 and parts[1] == 0 and parts[2] == 113:  # TEST-NET-3
        return True
    if parts[0] >= 224:  # multicast / reserved
        return True
    return False


PATTERNS_LEAK = [
    ("internal_uuid", "block",
     re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
     "UUID — likely internal agent/project/company ID. Use placeholder like 00000000-0000-0000-0000-000000000000"),
    ("discord_mention", "block",
     re.compile(r"<@\d{15,}>"),
     "Discord user mention with ID. Use <@YOUR_USER_ID>"),
    ("discord_channel_id", "block",
     re.compile(r"\b1488562[0-9]{12}|1485171[0-9]{12}|1469991[0-9]{12}|1469290[0-9]{12}\b"),
     "WiseChef Discord channel/server ID"),
    ("slack_webhook", "block",
     re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
     "Slack incoming webhook URL — credential-equivalent"),
    ("discord_webhook", "block",
     re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w\-]+"),
     "Discord webhook URL — credential-equivalent"),
    ("internal_hostname", "warn",
     re.compile(r"\b(?:wisechef-agents|wisechef-hq|adam-xps|rescue-medic|chef-vps|tori-host)\b"),
     "Internal hostname. Use a generic placeholder like <YOUR_HOST>"),
    ("ssh_user_combo", "block",
     re.compile(r"\b(?:ssh|scp|sftp)\s+(?:-[A-Za-z0-9]+\s+)*[a-z][a-z0-9_-]*@[a-z0-9][\w.\-]{2,}"),
     "SSH command with user@host — eliminates 50% of attacker recon"),
    ("real_case_forensics", "warn",
     re.compile(r"Real case\s+20[0-9]{2}-[01][0-9]-[0-3][0-9]", re.I),
     "Real-case forensics with date — embarrassing if customer reads"),
    ("ticket_reference", "warn",
     re.compile(r"\b(?:WIS|AP|CHEF|TORI|WISE)-\d{2,}\b"),
     "Internal ticket ID — generalize to <ticket-id> or remove"),
    ("personal_name", "warn",
     re.compile(r"\bAdam\s+Krawczyk\b|\bKrawczyk\b|\bkrawczyk@"),
     "Personal name. Use 'the developer' or remove."),
    ("personal_email_in_body", "warn",
     re.compile(r"\b[A-Za-z0-9._%+-]+@(?:wisechef\.ai|wisevision\.com|adamkrawczyk)"),
     "Personal/internal email"),
]

# ---------------------------------------------------------------------------
# Patterns — Source C: clawdhub install-time greps (defense in depth)
# ---------------------------------------------------------------------------

PATTERNS_CLAWDHUB = [
    ("base64_decode_call", "warn",
     re.compile(r"\b(?:atob|btoa|base64\.b64decode|base64\.decode|base64 -d)\b"),
     "Base64 decode call — review for obfuscated payload"),
    ("string_fromcharcode", "warn",
     re.compile(r"String\.fromCharCode\s*\("),
     "String.fromCharCode — common obfuscation pattern"),
    ("subprocess_shell_true", "warn",
     re.compile(r"subprocess\.(?:call|run|Popen|check_output)\([^)]*shell\s*=\s*True"),
     "subprocess with shell=True — command injection risk if input is uncontrolled"),
    ("os_system", "warn",
     re.compile(r"\bos\.system\s*\("),
     "os.system — prefer subprocess.run with list args"),
    ("child_process_exec", "warn",
     re.compile(r"\b(?:child_process|exec|spawn|execSync)\s*\("),
     "Node child_process — review the command string"),
    ("env_keyword_grep", "info",
     re.compile(r"\b(?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY)\s*=\s*['\"][\w\-+/=]{16,}['\"]"),
     "Hardcoded credential-like assignment — must be env var reference, not literal"),
]

# ---------------------------------------------------------------------------
# Patterns — Source D: generalization (the customer-facing concern)
# ---------------------------------------------------------------------------

PATTERNS_GENERALIZATION = [
    ("absolute_home_path", "warn",
     re.compile(r"/home/[a-z][a-z0-9_-]+(?:/|$|\b)"),
     "Absolute /home/<user> path — use ~/ or $HOME"),
    ("absolute_user_root", "warn",
     re.compile(r"\b/root/[a-z]"),
     "Absolute /root path — agency users won't run as root"),
    ("hermes_path", "warn",
     re.compile(r"~?/\.hermes/|~?/clawd/|~?/\.openclaw/"),
     "Hermes/Clawd/OpenClaw internal path — generalize to ~/.<your-agent>/ or remove"),
    ("hetzner_internal", "block",
     re.compile(r"\b168\.119\.\d{1,3}\.\d{1,3}\b"),
     "Known Hetzner internal IP range"),
    ("recipes_internal_db", "warn",
     re.compile(r"wiserecipes|recipes_db|paperclip_db"),
     "Internal database/service name. Use generic descriptor."),
]


# ---------------------------------------------------------------------------
# Per-line scanner
# ---------------------------------------------------------------------------

# File-extension allowlist — only scan text-ish files
_TEXT_EXT = {
    ".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".fish",
    ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx",
    ".yaml", ".yml", ".toml", ".json", ".env", ".ini", ".cfg",
    ".html", ".css", ".sql", ".rs", ".go", ".rb",
}
_BINARY_EXT = re.compile(r"\.(png|jpg|jpeg|gif|webp|pdf|zip|tar|tar\.gz|gz|bin|woff2?|ttf|otf|ico|mp[34]|wav|webm)$", re.I)
_SKIP_DIR_PARTS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", "venv", ".venv",
    "dist", "build", ".gitnexus", ".kuzu",
    # Internal sprint/dev docs that ship with repos but aren't part of the
    # publishable skill surface — these are allowed to mention internal hosts/paths.
    "SPRINT_DOCS", "sprint_docs", "internal_docs", ".claude", ".github",
    "tests", "test",  # tests can ship internal fixtures
}

# In --publish-mode, only files matching these patterns are scanned (the actual
# tarball contents per Anthropic Agent Skills standard §2.1).
PUBLISH_SCAN_PATHS = re.compile(
    r"^(SKILL\.md|README\.md|LICENSE|"
    r"scripts/[^/]+|references/[^/]+|assets/[^/]+|"
    r"skill\.toml|manifest\.toml)$"
)

# Files to ALWAYS scan even outside publish-mode (top-level docs that ship with the repo)
NEVER_SKIP_FILES = {"SKILL.md", "skill.toml", "manifest.toml"}

MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB hard cap per file


def _should_scan_file(path: str) -> bool:
    p = Path(path)
    if any(part in _SKIP_DIR_PARTS for part in p.parts):
        return False
    if _BINARY_EXT.search(path):
        return False
    # accept allowlisted extensions OR no extension (e.g. SKILL, README, LICENSE)
    if p.suffix.lower() in _TEXT_EXT or p.suffix == "":
        return True
    return False


def _scan_text(file_path: str, content: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = content.splitlines()

    # In-fence tracking — suppress some patterns inside markdown code-fences
    in_fence = False
    fence_re = re.compile(r"^\s*```")

    # Negation context: "No `curl | bash`...", "do NOT pipe", "never eval", etc.
    # When the pattern appears AFTER one of these, suppress as guidance-not-action.
    negation_re = re.compile(
        r"\b(?:no(?:t|ne)?|don'?t|never|avoid|forbid(?:den)?|prohibit(?:ed)?|"
        r"without|reject|disallow(?:ed)?|must\s+not|should\s+not|do\s+not)\b"
        r"[^.;!?]{0,80}$",
        re.I,
    )
    # Categories that should respect negation context (guidance docs talk ABOUT these)
    NEGATION_AWARE = {
        "pipe_to_shell", "pipe_to_shell_curl", "pipe_to_shell_wget",
        "eval_curl", "eval_b64", "exec_b64",
        "destructive_rm", "destructive_forkbomb",
        "prompt_injection_ignore", "prompt_injection_disregard",
        "prompt_injection_role", "prompt_injection_forget",
    }

    def _is_negated(line: str, match_start: int) -> bool:
        return bool(negation_re.search(line[:match_start]))

    for lineno, line in enumerate(lines, start=1):
        if fence_re.match(line):
            in_fence = not in_fence

        # ── Custom handler 1: destructive_rm with safe-target allowlist ──
        m = _DESTRUCTIVE_RM_RE.search(line)
        if m and not _is_negated(line, m.start()) and not _SAFE_RM_TARGETS.search(line):
            findings.append(Finding(
                "destructive_rm", "block", file_path, lineno,
                line.strip()[:200],
                "Filesystem destruction pattern",
                "malicious",
            ))

        # ── Custom handler 2: pipe-to-shell with installer allowlist ─────
        m = _PIPE_TO_SHELL_RE.search(line)
        if m and not _is_negated(line, m.start()):
            # Suppress if the URL in the curl/wget portion is a trusted installer.
            preceding = line[:m.start() + len(m.group(0))]
            if not _TRUSTED_INSTALLER_DOMAINS.search(preceding):
                findings.append(Finding(
                    "pipe_to_shell", "block", file_path, lineno,
                    line.strip()[:200],
                    "Remote curl/wget piped to shell — RCE risk. "
                    "If this is a trusted upstream installer, file an allowlist PR.",
                    "malicious",
                ))

        # ── Custom handler 3: SSH credential harvest (narrow) ────────────
        if _SSH_HARVEST_RE.search(line):
            findings.append(Finding(
                "cred_harvest_ssh", "block", file_path, lineno,
                line.strip()[:200],
                "Reads ~/.ssh credential file (authorized_keys / id_* / known_hosts)",
                "malicious",
            ))

        # Source A: malicious (rest of the patterns)
        for cat, sev, pat, rationale in PATTERNS_MALICIOUS:
            m = pat.search(line)
            if m:
                if cat in NEGATION_AWARE and _is_negated(line, m.start()):
                    continue
                findings.append(Finding(cat, sev, file_path, lineno,
                                        line.strip()[:200], rationale, "malicious"))

        # Source B: leak — IPv4 needs special handling (allow private + example IPs + public DNS)
        for m in _IPV4_RE.finditer(line):
            ip = m.group(0)
            if _is_private_or_example_ip(ip):
                continue
            if ip in _PUBLIC_DNS_IPS:
                continue  # Public DNS resolvers — documentation examples, not disclosure
            findings.append(Finding(
                "public_ipv4", "block", file_path, lineno,
                line.strip()[:200],
                f"Public IPv4 {ip} — recon disclosure. Use TEST-NET (192.0.2.x) or env var.",
                "leak"))
        for cat, sev, pat, rationale in PATTERNS_LEAK:
            if pat.search(line):
                findings.append(Finding(cat, sev, file_path, lineno,
                                        line.strip()[:200], rationale, "leak"))

        # Source C: clawdhub
        for cat, sev, pat, rationale in PATTERNS_CLAWDHUB:
            if pat.search(line):
                findings.append(Finding(cat, sev, file_path, lineno,
                                        line.strip()[:200], rationale, "clawdhub"))

        # Source D: generalization
        for cat, sev, pat, rationale in PATTERNS_GENERALIZATION:
            if pat.search(line):
                findings.append(Finding(cat, sev, file_path, lineno,
                                        line.strip()[:200], rationale, "general"))

    return findings


# ---------------------------------------------------------------------------
# Entry-point scanners — directory or tarball
# ---------------------------------------------------------------------------

def scan_directory(root: Path, publish_mode: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_PARTS]
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(root)
            rel_str = str(rel).replace("\\", "/")
            if publish_mode and not PUBLISH_SCAN_PATHS.match(rel_str):
                continue
            if not _should_scan_file(rel_str):
                continue
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    findings.append(Finding(
                        "oversize_file", "info", rel_str, None,
                        f"size={full.stat().st_size}",
                        "Skipped — file exceeds 1MB cap", "meta"))
                    continue
                content = full.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError) as exc:
                findings.append(Finding(
                    "read_error", "info", rel_str, None, str(exc)[:200],
                    "Could not read file", "meta"))
                continue
            findings.extend(_scan_text(rel_str, content))
    return findings


def scan_tarball(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                if not _should_scan_file(m.name):
                    continue
                if m.size > MAX_FILE_BYTES:
                    findings.append(Finding(
                        "oversize_file", "info", m.name, None,
                        f"size={m.size}", "Skipped — exceeds 1MB cap", "meta"))
                    continue
                try:
                    fobj = tf.extractfile(m)
                    if fobj is None:
                        continue
                    content = fobj.read().decode("utf-8", errors="replace")
                except (tarfile.TarError, OSError, UnicodeError):
                    continue
                findings.extend(_scan_text(m.name, content))
    except (tarfile.TarError, OSError) as exc:
        findings.append(Finding("tarball_error", "block", str(path), None,
                                str(exc)[:200], "Could not open tarball", "meta"))
    return findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize(findings: list[Finding]) -> dict:
    by_sev = {"block": 0, "warn": 0, "info": 0}
    by_source = {}
    by_cat = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_source[f.source] = by_source.get(f.source, 0) + 1
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
    return {
        "total": len(findings),
        "by_severity": by_sev,
        "by_source": by_source,
        "by_category": by_cat,
    }


def print_human(target: str, findings: list[Finding], summary: dict) -> None:
    print(f"\n══ Skill Quality Gate v{VERSION} ══")
    print(f"Target: {target}")
    print(f"Total findings: {summary['total']}")
    print(f"  block: {summary['by_severity']['block']}  "
          f"warn: {summary['by_severity']['warn']}  "
          f"info: {summary['by_severity']['info']}")
    if summary['total']:
        print(f"By source: {summary['by_source']}")
        print(f"\nFindings (showing first 60):")
        for f in findings[:60]:
            print(f"  {f.short()}")
            print(f"         → {f.rationale}")
        if len(findings) > 60:
            print(f"  … and {len(findings) - 60} more (run with --json for full list)")
    else:
        print("\n✅ Clean — no leakage, no malicious patterns, fully generalized.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Skill directory OR .tar.gz tarball")
    ap.add_argument("--json", action="store_true", help="JSON output (machine readable)")
    ap.add_argument("--strict", action="store_true",
                    help="Fail on ANY finding (block+warn+info). Default fails only on block.")
    ap.add_argument("--no-warn", action="store_true",
                    help="Suppress warn-level findings (only block-level cause failure)")
    ap.add_argument("--allow-categories", default="",
                    help="Comma-separated category names to ignore (e.g. real_case_forensics,ticket_reference)")
    ap.add_argument("--publish", action="store_true",
                    help="Publish-mode: only scan SKILL.md, README.md, LICENSE, scripts/, references/, assets/, skill.toml. "
                         "Use this in CI to validate exactly what would be packaged into the tarball.")
    ap.add_argument("--version", action="version", version=f"skill-quality-gate {VERSION}")
    args = ap.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"ERROR: not found: {target}", file=sys.stderr)
        return 3

    allowed = set(c.strip() for c in args.allow_categories.split(",") if c.strip())

    if target.is_dir():
        findings = scan_directory(target, publish_mode=args.publish)
    elif target.suffixes[-2:] in ([".tar", ".gz"], [".gz"]) or target.suffix == ".tgz":
        findings = scan_tarball(target)
    elif target.is_file():
        findings = _scan_text(target.name, target.read_text(encoding="utf-8", errors="replace"))
    else:
        print(f"ERROR: unsupported target: {target}", file=sys.stderr)
        return 3

    # Apply allow-list
    findings = [f for f in findings if f.category not in allowed]
    if args.no_warn:
        findings = [f for f in findings if f.severity == "block"]

    summary = summarize(findings)

    if args.json:
        print(json.dumps({
            "version": VERSION,
            "target": str(target),
            "summary": summary,
            "findings": [asdict(f) for f in findings],
        }, indent=2))
    else:
        print_human(str(target), findings, summary)

    # Exit code policy
    if summary["by_severity"]["block"] > 0:
        return 2
    if args.strict and summary["total"] > 0:
        return 2
    if summary["by_severity"]["warn"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
