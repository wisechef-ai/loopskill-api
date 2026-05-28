"""scripts/refresh_marketing_counts.py — keep config/recipes-marketing.yaml fresh.

Reads live counts from the DB and overwrites the ``counts`` block in
``config/recipes-marketing.yaml`` so the static-fallback path stays close
to live within 24h.

Run cadence (per quality_1705 Phase A6):
  - nightly via the existing ``recipes-publish-watchdog`` cron (every 4h)
  - on every catalog change (publish webhook calls this)
  - before any deploy

Idempotent: identical counts produce identical bytes (sorted keys, fixed
indent). When run with --check, prints the would-write diff and exits
nonzero if any diff exists — for the watchdog to flag staleness.

Per quality_1705 plan §3 Phase A step 6: the watchdog detects 6-day-stale
``last_refresh_at``; this script is what closes the loop. The watchdog
itself (in ``~/.hermes/scripts/recipes_publish_watchdog.py``) calls this
script after detecting drift > 1.

Deploy-persistence (2026-05-22): the deploy.yml workflow runs
``git reset --hard origin/main`` on every deploy, which wipes any
on-disk refresh that was never committed. Passing ``--auto-commit``
makes the refresh script ``git add + commit + push`` the updated yaml
back to origin/main, so the snapshot survives the next deploy.

The commit only fires when there is an actual textual diff vs HEAD —
running --auto-commit on an already-fresh yaml is a no-op. Commits are
attributed to ``wisechef-deploy <deploy@wisechef.ai>`` and tagged
``[skip ci]`` so they don't trigger redundant CI runs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "config" / "recipes-marketing.yaml"


def get_db_url() -> str:
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


def compute_counts() -> dict:
    from sqlalchemy import create_engine, text

    engine = create_engine(get_db_url(), future=True)
    with engine.connect() as conn:
        result = conn.execute(
            text("""
            SELECT
              SUM(CASE WHEN is_archived = false AND is_public = true THEN 1 ELSE 0 END) AS total,
              SUM(CASE WHEN is_archived = false AND is_public = true AND tier = 'free' THEN 1 ELSE 0 END) AS free,
              SUM(CASE WHEN is_archived = false AND is_public = true AND tier IN ('pro', 'cook') THEN 1 ELSE 0 END) AS pro,  -- cook = legacy alias
              SUM(CASE WHEN is_archived = false AND is_public = true AND tier IN ('pro_plus', 'operator','studio') THEN 1 ELSE 0 END) AS pro_plus_only  -- legacy aliases
            FROM skills
        """)
        ).first()
    return {
        "skills_total": int(result.total or 0),
        "free_skills": int(result.free or 0),
        "pro_skills": int(result.pro or 0),
        "pro_plus_exclusive_skills": int(result.pro_plus_only or 0),
    }


def update_yaml(counts: dict, mcp_tools: int = 6, rest_endpoints: int = 11) -> tuple[bool, str]:
    """Return (changed, new_yaml_text). Idempotent on the counts block.

    2026-05-19: switched from yaml.safe_dump(full doc) to surgical regex
    updates so comments and structural formatting are preserved. The previous
    impl stripped ~50 lines of SSOT contract documentation on every run,
    which is why no one committed the auto-refreshed output — the diff was
    too destructive.
    """
    import re
    import yaml

    raw = YAML_PATH.read_text()
    data = yaml.safe_load(raw)
    if "counts" not in data:
        # Bootstrap path — file has no counts block. Fall back to safe_dump
        # so we don't silently miss the first write.
        data["counts"] = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        data["counts"] = {
            "skills_total": counts["skills_total"],
            "free_skills": counts["free_skills"],
            "pro_skills": counts["pro_skills"],
            "pro_plus_exclusive_skills": counts["pro_plus_exclusive_skills"],
            "mcp_tools_count": mcp_tools,
            "rest_endpoint_count": rest_endpoints,
            "last_refresh_at": now,
        }
        return True, yaml.safe_dump(data, sort_keys=False, default_flow_style=False, indent=2)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    old_counts = dict(data["counts"])

    new_values = {
        "skills_total": str(counts["skills_total"]),
        "free_skills": str(counts["free_skills"]),
        "pro_skills": str(counts["pro_skills"]),
        "pro_plus_exclusive_skills": str(counts["pro_plus_exclusive_skills"]),
        # mcp_tools_count and rest_endpoint_count are not auto-refreshed —
        # they\'re manual SSOT and the watchdog tracks separate invariants.
        "last_refresh_at": f"'{now}'",
    }

    new_text = raw
    for key, val in new_values.items():
        # Match: leading whitespace, key, colon, current value, optional trailing comment
        pattern = re.compile(rf"^(\s+{re.escape(key)}:\s+)([^\s#]+)(\s*(?:#.*)?)$", re.MULTILINE)
        replaced, n = pattern.subn(rf"\g<1>{val}\g<3>", new_text, count=1)
        if n == 1:
            new_text = replaced
        # If n != 1 we silently skip — caller can detect via missing values in the diff

    numeric_changed = any(
        old_counts.get(k) != counts.get(k)
        for k in ["skills_total", "free_skills", "pro_skills", "pro_plus_exclusive_skills"]
    )
    return numeric_changed, new_text


def git_auto_commit(yaml_path: Path) -> tuple[bool, str]:
    """Commit yaml_path back to origin/main if there is a textual diff vs HEAD.

    Returns (committed, message). Idempotent: a no-op diff returns
    (False, "no diff vs HEAD") and exits without invoking git commit.

    Designed for the nightly cron + watchdog auto-heal path on
    wisechef-hq. Must NOT raise on transient git failures (e.g.
    network blip on push) — the cron should keep running and the
    watchdog will retry within 4h.

    Push collision handling: if ``git push`` fails because someone
    else advanced main between fetch and push, we ``git pull --rebase
    origin main`` and retry once. Two failures in a row → bail with a
    warning, leave the commit unpushed (next run will catch it).
    """
    repo_root = yaml_path.parent.parent
    rel_path = str(yaml_path.relative_to(repo_root))

    def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        # Rationale: git commands are bounded, stdout/stderr captured for logging.
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    # Short-circuit if there's no on-disk diff vs HEAD.
    diff = _git("diff", "--exit-code", "--quiet", "--", rel_path, check=False)
    if diff.returncode == 0:
        return False, "no diff vs HEAD"

    # Stage + commit with bot identity. -c flags scope identity to THIS
    # invocation so we don't pollute the host's global git config.
    _git("add", "--", rel_path)
    commit = _git(
        "-c",
        "user.name=wisechef-deploy",
        "-c",
        "user.email=deploy@wisechef.ai",
        "commit",
        "-m",
        "chore(marketing): refresh snapshot counts [skip ci] [auto]",
        "-m",
        "Auto-committed by scripts/refresh_marketing_counts.py --auto-commit.",
        check=False,
    )
    if commit.returncode != 0:
        return False, f"commit failed: {commit.stderr.strip() or commit.stdout.strip()}"

    # Push with one rebase-retry on non-fast-forward.
    push = _git("push", "origin", "HEAD:main", check=False)
    if push.returncode != 0:
        # Likely non-fast-forward — rebase and retry once.
        fetch = _git("fetch", "origin", "main", check=False)
        rebase = _git("pull", "--rebase", "origin", "main", check=False)
        if fetch.returncode != 0 or rebase.returncode != 0:
            return True, (
                f"committed locally but push+rebase failed: {(rebase.stderr or rebase.stdout).strip()[:200]}"
            )
        push = _git("push", "origin", "HEAD:main", check=False)
        if push.returncode != 0:
            return True, (
                f"committed locally but push retry failed: {(push.stderr or push.stdout).strip()[:200]}"
            )

    sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    return True, f"pushed {sha} to origin/main"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Print would-write diff; exit 1 if counts would change."
    )
    parser.add_argument("--commit", action="store_true", help="Write the updated yaml. Default is dry-run.")
    parser.add_argument(
        "--auto-commit",
        action="store_true",
        help="After --commit, git-commit + push the updated yaml to "
        "origin/main so it survives the next deploy reset.",
    )
    args = parser.parse_args()

    counts = compute_counts()
    changed, new_text = update_yaml(counts)
    print(f"Live counts: {counts}")
    print(f"Numeric change vs on-disk yaml: {changed}")

    if args.check:
        if changed:
            print("[CHECK] DIFF DETECTED — yaml is stale. Run --commit to refresh.")
            return 1
        print("[CHECK] yaml matches live counts.")
        return 0

    if args.commit:
        YAML_PATH.write_text(new_text)
        print(f"[COMMITTED] {YAML_PATH}")
        if args.auto_commit:
            try:
                pushed, msg = git_auto_commit(YAML_PATH)
                tag = "[GIT-PUSHED]" if pushed else "[GIT-SKIP]"
                print(f"{tag} {msg}")
            # Rationale: cron must not fail because of a transient git
            # error — the yaml on disk is still correct; the next run
            # will re-attempt the push.
            except Exception as exc:  # noqa: BLE001
                print(f"[GIT-ERROR] auto-commit raised but yaml is written: {exc}")
        return 0

    print()
    print(f"[DRY-RUN] Would rewrite {YAML_PATH}. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
