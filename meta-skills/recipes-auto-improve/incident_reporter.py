"""recipes-auto-improve — wraps skill invocations, posts sanitized
incident reports on failure. Stdlib only. Apache-2.0."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


_HOME_RE = re.compile(r"(/home/[^/\s]+|/Users/[^/\s]+)")
_API_KEY_RE = re.compile(r"rec_[0-9a-f]{16,}", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Remove $HOME paths and any rec_<hex> tokens from a string."""
    if not text:
        return text
    text = _HOME_RE.sub("/<HOME>", text)
    text = _API_KEY_RE.sub("rec_<REDACTED>", text)
    return text


def normalize_stack(tb_lines: list[str]) -> str:
    """Keep only the top-5 frames; drop file paths to module:lineno:func."""
    frames = []
    for line in tb_lines:
        line = line.strip()
        if line.startswith('File "'):
            # `File "/abs/path/mod.py", line 12, in fn`
            m = re.match(r'File "([^"]+)", line (\d+), in (\S+)', line)
            if m:
                mod = os.path.basename(m.group(1))
                frames.append(f"{mod}:{m.group(2)}:{m.group(3)}")
        elif frames and not line.startswith("File"):
            # The source line for the most recent frame; keep first 80 chars.
            frames[-1] += " | " + line[:80]
    return "\n".join(frames[:5])


def signature(normalized_stack: str) -> str:
    return hashlib.sha256(normalized_stack.encode("utf-8")).hexdigest()


def env_fingerprint(skill_version: str | None = None) -> dict[str, Any]:
    fp: dict[str, Any] = {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "py": platform.python_version(),
        "ram_gb": _ram_gb(),
        "cuda": _cuda(),
    }
    if skill_version:
        fp["skill_version"] = skill_version
    return fp


def _ram_gb() -> int | None:
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page = os.sysconf("SC_PAGE_SIZE")
            return max(1, (pages * page) // (1024**3))
    except (OSError, ValueError):
        pass
    return None


def _cuda() -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def build_report(
    *,
    skill_id: str,
    skill_version: str | None,
    command: list[str] | str,
    exit_code: int,
    tb_lines: list[str],
    agent_fp_anon: str,
) -> dict[str, Any]:
    norm = normalize_stack(tb_lines)
    sig = signature(norm)
    cmd_str = " ".join(command) if isinstance(command, list) else command
    return {
        "skill_id": skill_id,
        "error_signature": sig,
        "env_fingerprint": env_fingerprint(skill_version),
        "agent_fp_anon": agent_fp_anon,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "command": _scrub(cmd_str)[:512],
        "exit_code": exit_code,
        "stack_trace_top": _scrub(norm)[:2048],
    }


def post_report(
    report: dict[str, Any],
    *,
    api_url: str,
    api_key: str,
    timeout_s: float = 5.0,
) -> bool:
    body = json.dumps(report).encode("utf-8")
    req = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/api/feedback/incident",
        data=body,
        method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def wrap_invocation(
    *,
    skill_id: str,
    skill_version: str | None,
    command: list[str],
    agent_fp_anon: str,
    api_url: str,
    api_key: str,
) -> int:
    """Run `command`. On failure, build & POST a report. Return exit code."""
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode != 0:
            tb_lines = (proc.stderr or "").splitlines()[-30:]
            report = build_report(
                skill_id=skill_id, skill_version=skill_version,
                command=command, exit_code=proc.returncode,
                tb_lines=tb_lines, agent_fp_anon=agent_fp_anon,
            )
            try:
                post_report(report, api_url=api_url, api_key=api_key)
            except Exception:
                pass
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    except Exception:
        tb_lines = traceback.format_exc().splitlines()
        report = build_report(
            skill_id=skill_id, skill_version=skill_version,
            command=command, exit_code=1,
            tb_lines=tb_lines, agent_fp_anon=agent_fp_anon,
        )
        try:
            post_report(report, api_url=api_url, api_key=api_key)
        except Exception:
            pass
        return 1


def run_cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recipes-auto-improve")
    p.add_argument("--skill-id", required=True)
    p.add_argument("--skill-version", default=None)
    p.add_argument("--api-url", default=os.environ.get(
        "RECIPES_API_URL", "https://recipes.wisechef.ai"))
    p.add_argument("--api-key", default=os.environ.get("RECIPES_API_KEY"))
    p.add_argument("--agent-fp", default=os.environ.get("RECIPES_AGENT_FP"))
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    if not args.api_key or not args.agent_fp or not args.rest:
        sys.stderr.write("usage: --skill-id UUID -- <cmd>; needs RECIPES_API_KEY+AGENT_FP\n")
        return 2
    cmd = args.rest[1:] if args.rest[0] == "--" else args.rest
    return wrap_invocation(
        skill_id=args.skill_id, skill_version=args.skill_version,
        command=cmd, agent_fp_anon=args.agent_fp,
        api_url=args.api_url, api_key=args.api_key,
    )
