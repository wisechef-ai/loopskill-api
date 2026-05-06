#!/usr/bin/env python3
"""Pre-flight diagnostic for Phase H: Chef content pipeline failure triage.

Output: SPRINT_DOCS/CHEF_DIAGNOSIS.json
Exit 0 if all checks green, 1 if any infra issue detected.

READ-ONLY against wisechef-hq / wisechef-agents. No mutations.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "SPRINT_DOCS" / "CHEF_DIAGNOSIS.json"

CHECKS: list[dict] = []


def run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as e:
        return 127, "", f"binary not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def add(name: str, status: str, detail: str, raw: str = "") -> None:
    CHECKS.append(
        {
            "name": name,
            "status": status,  # "green" | "yellow" | "red" | "skip"
            "detail": detail,
            "raw": raw[:500] if raw else "",
        }
    )


def check_resend_quota() -> None:
    """grep journal on wisechef-hq for Resend 429s in the last 24h."""
    if not shutil.which("ssh"):
        add("resend_quota", "skip", "ssh not available", "")
        return
    rc, out, err = run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "BatchMode=yes",
            "wisechef-hq",
            "journalctl --since '24 hours ago' --no-pager 2>/dev/null | grep -ciE 'resend.*(429|quota|rate.?limit)' || echo 0",
        ],
        timeout=20,
    )
    if rc != 0:
        add("resend_quota", "skip", f"ssh wisechef-hq inaccessible (rc={rc})", err)
        return
    try:
        count = int(out.splitlines()[-1].strip())
    except (ValueError, IndexError):
        add("resend_quota", "skip", "could not parse count", out)
        return
    if count == 0:
        add("resend_quota", "green", "no Resend 429/quota errors in last 24h")
    elif count < 5:
        add("resend_quota", "yellow", f"{count} Resend rate-limit hits in 24h")
    else:
        add("resend_quota", "red", f"{count} Resend rate-limit hits in 24h — quota exhausted")


def check_portal_deploy() -> None:
    """curl Last-Modified on wisechef-portal-v3 production index."""
    if not shutil.which("curl"):
        add("portal_deploy", "skip", "curl not available")
        return
    rc, out, err = run(
        [
            "curl",
            "-sI",
            "--max-time",
            "10",
            "https://wisechef.tech/",
        ],
        timeout=15,
    )
    if rc != 0 or not out:
        add("portal_deploy", "skip", f"curl failed (rc={rc})", err)
        return
    last_mod = ""
    for line in out.splitlines():
        if line.lower().startswith("last-modified"):
            last_mod = line.split(":", 1)[1].strip()
            break
    if not last_mod:
        add("portal_deploy", "yellow", "no Last-Modified header", out)
        return
    add("portal_deploy", "green", f"portal Last-Modified: {last_mod}")


def check_og_image_route() -> None:
    """curl og:image generation route on wisechef portal."""
    if not shutil.which("curl"):
        add("og_image_route", "skip", "curl not available")
        return
    rc, out, err = run(
        [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "10",
            "https://wisechef.tech/api/og?title=diagnose",
        ],
        timeout=15,
    )
    if rc != 0:
        add("og_image_route", "skip", f"curl failed (rc={rc})", err)
        return
    code = out.strip()
    if code in ("200", "302"):
        add("og_image_route", "green", f"og:image route returns {code}")
    elif code in ("404", "405"):
        add("og_image_route", "yellow", f"og:image route returns {code} — route may not exist")
    else:
        add("og_image_route", "red", f"og:image route returns {code}")


def check_cloudflare_articles() -> None:
    """Skip — requires Cloudflare API token not in scope for this phase."""
    add("cloudflare_articles", "skip", "Cloudflare API token not provisioned in this phase")


def check_chef_ack_discord() -> None:
    """Look for Discord bot token in env; skip if absent."""
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        add("chef_ack_discord", "skip", "DISCORD_BOT_TOKEN not in env")
        return
    add("chef_ack_discord", "skip", "Discord ACK lookup deferred to manual triage")


def check_local_main_drift() -> None:
    """Compare local main HEAD vs origin/main — drift indicator."""
    rc, out, err = run(["git", "rev-parse", "origin/main"])
    if rc != 0:
        add("repo_drift", "skip", "could not resolve origin/main", err)
        return
    origin_sha = out
    rc, out, _ = run(["git", "rev-parse", "HEAD"])
    head_sha = out if rc == 0 else "unknown"
    add(
        "repo_drift",
        "green",
        f"branch head={head_sha[:8]}, origin/main={origin_sha[:8]}",
    )


def check_hcloud_credentials() -> None:
    """Confirm hcloud credentials are unavailable (per phase brief)."""
    token = os.environ.get("HCLOUD_TOKEN")
    cfg = Path.home() / ".hcloud" / "config.json"
    if token:
        add("hcloud_credentials", "yellow", "HCLOUD_TOKEN in env — phase brief said unavailable")
    elif cfg.exists():
        add("hcloud_credentials", "yellow", f"hcloud config present at {cfg}")
    else:
        add(
            "hcloud_credentials",
            "red",
            "HCLOUD_TOKEN absent + no ~/.hcloud config — provisioning blocked",
        )


def main() -> int:
    started = time.time()
    check_resend_quota()
    check_portal_deploy()
    check_og_image_route()
    check_cloudflare_articles()
    check_chef_ack_discord()
    check_local_main_drift()
    check_hcloud_credentials()

    red_count = sum(1 for c in CHECKS if c["status"] == "red")
    yellow_count = sum(1 for c in CHECKS if c["status"] == "yellow")
    green_count = sum(1 for c in CHECKS if c["status"] == "green")
    skip_count = sum(1 for c in CHECKS if c["status"] == "skip")

    # Decision: skill-solvable vs infra-level.
    # If hcloud is red AND any other red (resend/og), it is infra-level.
    # Pure hcloud-red alone still pushes us to scenario B since Phase H depends
    # on either fixing Chef content or shipping interplus-deploy-v1; without
    # provisioning we cannot fully exercise scenario A.
    decision = "skill-solvable"
    rationale = "no infra-level blockers detected"
    hcloud_red = any(c["name"] == "hcloud_credentials" and c["status"] == "red" for c in CHECKS)
    if hcloud_red:
        decision = "infra-level"
        rationale = (
            "HCLOUD_TOKEN unavailable — cannot provision wisechef-maestro CX23 box; "
            "scope shifts to canonical interplus-deploy-v1 sub-recipe + local smoke install only"
        )
    elif red_count > 0:
        decision = "infra-level"
        rationale = f"{red_count} infra-level red checks detected"

    summary = {
        "started_at": int(started),
        "duration_s": round(time.time() - started, 2),
        "totals": {
            "green": green_count,
            "yellow": yellow_count,
            "red": red_count,
            "skip": skip_count,
        },
        "decision": decision,
        "rationale": rationale,
        "checks": CHECKS,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if (red_count == 0 and decision == "skill-solvable") else 1


if __name__ == "__main__":
    sys.exit(main())
